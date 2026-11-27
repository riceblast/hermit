#!/usr/bin/env python3
"""Operator-started Stage 1 supplemental tests and the user's original BFS script.

Requires two already-running 16+32 GiB Servers. Stops on the first failure,
retaining processes, control fds, trace, swap and settings for diagnosis.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
import stage1_core_host as core

REPO = core.REPO
RS, HMT = core.RS, core.HMT
LIGRA = Path('/mnt/sdb2/yxr/K_R_benchmarks/ligra')
BFS = LIGRA / 'apps/BFS'
BFS_SCRIPT = LIGRA / 'scripts/cgroup_run_bfs.sh'
GRAPH = LIGRA / 'inputs/generated/rMat_64G.adj'
BIG_SWAP = '/mnt/sdb4/yxr/swapfile/swap_64g.img'
CG_PARENT = Path('/sys/fs/cgroup/memory')
BFS_CG = CG_PARENT / 'hermit-test'
HMT_CG = CG_PARENT / 'hermit-p12-extended'
TRACE_ROOT = Path('/sys/kernel/debug/tracing')
TRACE = TRACE_ROOT / 'instances/hermit_p12_extended'
PROBE = 'p12_stage1_extended/hermit_hit'
GROUP_SETTINGS = ('memory.memsw.limit_in_bytes', 'memory.limit_in_bytes', 'memory.swappiness')
RUN = None
OPEN_CONTROLS = set()
BFS_JOB = None
BFS_PID = None
PAUSED = []


def require(ok, message):
    core.require(ok, message)


def save(name, value):
    core.save(name, value)


def result_file(name, value):
    save(name, json.dumps(value, indent=2) + '\n')


def state(message):
    core.state(message)


def command(args, name, timeout=180):
    with (RUN / name).open('w') as output:
        subprocess.run(args, stdout=output, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def trace_command(text):
    # Text append-open seeks to EOF; tracefs seq_lseek rejects SEEK_END.
    # O_TRUNC would remove other users' probes. Use a raw append fd instead.
    fd = os.open(TRACE_ROOT / 'kprobe_events', os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
    try:
        payload = (text + '\n').encode()
        require(os.write(fd, payload) == len(payload), 'Short kprobe control write')
    finally:
        os.close(fd)


def bind(pid):
    fd = os.open(RS / 'identity', os.O_RDWR | os.O_CLOEXEC)
    OPEN_CONTROLS.add(fd)
    os.write(fd, ('bind ' + str(pid) + '\n').encode())
    response = core.fields(os.pread(fd, 512, 0).decode())
    require(response['attached'] == '1' and int(response['pid']) == pid, 'Incorrect bind response')
    core.CONTROL = fd
    return fd, int(response['binding_id'])


def close_control(fd):
    os.close(fd)
    OPEN_CONTROLS.remove(fd)
    if core.CONTROL == fd:
        core.CONTROL = None


def lookup(fd, offset):
    os.write(fd, ('lookup ' + str(offset) + '\n').encode())
    return core.fields(os.pread(fd, 512, 0).decode())


class Workload(core.Workload):
    def send(self, text):
        response = super().send(text)
        self.base = response.get('base', self.base)
        self.length = response.get('bytes', self.length)
        return response

    def execute_again(self):
        old_pid = self.pid
        self.process.stdin.write(b'exec\n')
        self.process.stdin.flush()
        response = self.receive()
        require(response.get('event') == 'ready' and response['pid'] == old_pid, 'Exec did not preserve PID')
        os.close(self.pm)
        self.pm = os.open('/proc/' + str(self.pid) + '/pagemap', os.O_RDONLY | os.O_CLOEXEC)
        self.base, self.length = response['base'], response['bytes']
        return response


def swapped_records(work):
    return {(entry & 31, (entry >> 5) & ((1 << 50) - 1)): work.base // 4096 + page
            for page, entry in enumerate(work.pages()) if core.swapped(entry)}


def check_detached(label):
    status = core.snapshot(label)
    require(status['bound'] == '0' and status['entries'] == '0' and status['aliases'] == '0',
            'Identity remained bound: ' + str(status))


def active_swap(path, label):
    require(not core.swaps() and not OPEN_CONTROLS, 'Swap/control still active before swapon')
    command(['swapon', str(path)], label + '.swapon.log')
    require([row[0] for row in core.swaps()] == [str(path)], 'Unexpected active swap')
    check_detached(label + '.start')


def stop_swap(path, label):
    require([row[0] for row in core.swaps()] == [str(path)], 'Unexpected swap before swapoff')
    command(['swapoff', str(path)], label + '.swapoff.log', timeout=300)
    require(not core.swaps(), 'Swap remained active')
    check_detached(label + '.stop')
    core.empty_mapping()


def test_reuse():
    state('REUSE: testing final release and actual type/offset reuse at different VAs')
    work = Workload('reuse', 32)
    fd, binding_id = bind(work.pid)
    seen, reused, reused_keys, history = {}, [], set(), []
    with (RUN / 'reuse.pairs.tsv').open('w') as output:
        output.write('type\toffset\told_vpn\tnew_vpn\taliases\n')
        for cycle in range(16):
            work.send('out all')
            core.await_swap(work, 0, work.length // 4096)
            audit = core.audit(work, 'reuse-' + str(cycle), True)
            current = swapped_records(work)
            for key, vpn in current.items():
                if key in seen and seen[key] != vpn:
                    response = lookup(fd, key[1])
                    require(response['found'] == '1' and int(response['vpn']) == vpn and response['aliases'] == '1',
                            'Reused offset retained old VPN: ' + str(response))
                    reused.append(dict(type=key[0], offset=key[1], old_vpn=seen[key], new_vpn=vpn))
                    reused_keys.add(key)
                    output.write(f'{key[0]}\t{key[1]}\t{seen[key]}\t{vpn}\t1\n')
            history.append(dict(cycle=cycle, base=work.base, audit=audit, reused=len(reused_keys)))
            seen.update(current)
            if len(reused_keys) >= 128:
                break
            work.send('remap')
        require(len(reused_keys) >= 128, 'Actual offset reuse not demonstrated; stop rather than claim coverage')
    work.finish()
    core.snapshot('reuse-exited')
    close_control(fd)
    check_detached('reuse-closed')
    result = dict(binding_id=binding_id, cycles=history, actual_reused_offsets=len(reused_keys),
                  reuse_pairs=len(reused), content_verified=True)
    result_file('reuse.results.json', result)
    return result


def test_hermit():
    state('HERMIT: 32 MiB anonymous pages in a 16 MiB memory cgroup; vaddr_swapout=Y')
    HMT_CG.mkdir()
    for name, value in zip(GROUP_SETTINGS, ('-1', str(16 * 1024**2), '100')):
        (HMT_CG / name).write_text(value + '\n')
    (HMT / 'vaddr_swapout').write_text('Y\n')
    require('RSWAP_STAGE1_CGROUP' not in os.environ, 'Unexpected workload cgroup override')
    os.environ['RSWAP_STAGE1_CGROUP'] = str(HMT_CG)
    try:
        work = Workload('hermit', 32)
    finally:
        del os.environ['RSWAP_STAGE1_CGROUP']
    fd, binding_id = bind(work.pid)
    TRACE.mkdir()
    (TRACE / 'tracing_on').write_text('0\n')
    (TRACE / 'buffer_size_kb').write_text('256\n')
    # Instances inherit global print options; force the format parsed below.
    for name, value in (('context-info', '1'), ('raw', '0'), ('hex', '0'), ('bin', '0')):
        (TRACE / 'options' / name).write_text(value + '\n')
    trace_command('p:' + PROBE + ' hermit_try_to_unmap_one')
    event = TRACE / ('events/' + PROBE)
    (event / 'filter').write_text('common_pid == ' + str(work.pid) + '\n')
    (event / 'enable').write_text('1\n')
    (TRACE / 'tracing_on').write_text('1\n')
    work.send('rewrite')
    core.await_swap(work, 0, work.length // 4096)
    audit = core.audit(work, 'hermit-pressure', True)
    work.send('verify')
    work.send('rewrite')
    core.await_swap(work, 0, work.length // 4096)
    second = core.audit(work, 'hermit-pressure-second', True)
    work.finish()
    (TRACE / 'tracing_on').write_text('0\n')
    trace = (TRACE / 'trace').read_text()
    save('hermit.function-trace', trace)
    save('hermit.kprobe-profile', (TRACE_ROOT / 'kprobe_profile').read_text())
    save('hermit.buffer-stats', ''.join(p.parent.name + '\n' + p.read_text()
                                      for p in sorted((TRACE / 'per_cpu').glob('cpu*/stats'))))
    hits = [line for line in trace.splitlines() if 'hermit_hit:' in line and re.search(r'-' + str(work.pid) + r'\s', line)]
    require(hits, 'No recorded Hermit function hit for the test PID')
    (event / 'enable').write_text('0\n')
    TRACE.rmdir()
    trace_command('-:' + PROBE)
    core.snapshot('hermit-exited')
    close_control(fd)
    check_detached('hermit-closed')
    require(not (HMT_CG / 'cgroup.procs').read_text().strip(), 'Hermit cgroup still contains tasks')
    HMT_CG.rmdir()
    (HMT / 'vaddr_swapout').write_text('N\n')
    result = dict(binding_id=binding_id, pid=work.pid, audits=[audit, second],
                  recorded_function_hits=len(hits), trace_purpose='PID-specific function-hit proof; not a complete call count',
                  verified_generations=2, memory_limit_bytes=16 * 1024**2)
    result_file('hermit.results.json', result)
    return result


def test_lifecycle(small_swap):
    state('LIFECYCLE: close/rebind, exec requires new mm binding, swapoff and old-fd ownership')
    work = Workload('lifecycle', 4)
    first, first_id = bind(work.pid)
    work.send('out all')
    core.await_swap(work, 0, work.length // 4096)
    core.audit(work, 'lifecycle-initial', True)
    close_control(first)
    check_detached('lifecycle-close')
    old, old_id = bind(work.pid)
    require(old_id > first_id, 'binding_id did not advance')
    core.audit(work, 'lifecycle-rebind', True)
    work.execute_again()
    require(core.fields(os.pread(old, 512, 0).decode())['attached'] == '1', 'Exec unexpectedly detached old mm')
    work.send('out all')
    core.await_swap(work, 0, work.length // 4096)
    core.audit(work, 'lifecycle-exec-old-mm', False)
    try:
        os.write(old, ('bind ' + str(work.pid) + '\n').encode())
    except OSError as exc:
        require(exc.errno == errno.ESTALE, 'Used fd rejected bind with unexpected errno')
    else:
        raise RuntimeError('Used fd incorrectly rebound across exec')
    close_control(old)
    current, current_id = bind(work.pid)
    core.audit(work, 'lifecycle-exec-new-mm', True)
    stop_swap(small_swap, 'lifecycle-first')
    require(core.fields(os.pread(current, 512, 0).decode())['attached'] == '0', 'Old fd attached after swapoff')
    command(['swapon', str(small_swap)], 'lifecycle-second.swapon.log')
    fresh, fresh_id = bind(work.pid)
    require(fresh_id > current_id, 'Fresh binding_id did not advance')
    close_control(current)
    status = core.snapshot('lifecycle-old-fd-closed')
    require(status['bound'] == '1' and int(status['binding_id']) == fresh_id, 'Old fd detached fresh binding')
    work.send('rewrite')
    work.send('out all')
    core.await_swap(work, 0, work.length // 4096)
    core.audit(work, 'lifecycle-fresh', True)
    work.finish()
    core.snapshot('lifecycle-exited')
    close_control(fresh)
    check_detached('lifecycle-final')
    result = dict(close_detaches=True, exec_stays_with_old_mm=True, used_fd_errno=errno.ESTALE,
                  new_mm_rebind=True, old_fd_did_not_detach_fresh_binding=True,
                  binding_ids=[first_id, old_id, current_id, fresh_id], content_verified=True)
    result_file('lifecycle.results.json', result)
    return result


def proc_identity(pid):
    text = Path('/proc/' + str(pid) + '/stat').read_text()
    rest = text[text.rfind(')') + 2:].split()
    return int(rest[1]), int(rest[19])


def pause_bfs(pid):
    global PAUSED
    chain, current = [], pid
    for _ in range(16):
        parent, start = proc_identity(current)
        chain.append((current, start))
        if current == BFS_JOB.pid:
            break
        current = parent
    require(chain[-1][0] == BFS_JOB.pid, 'BFS is not a descendant of the launched script')
    PAUSED = chain
    os.kill(pid, signal.SIGSTOP)
    deadline = time.monotonic() + 15
    while True:
        states = []
        for task in Path('/proc/' + str(pid) + '/task').iterdir():
            try:
                status = task.joinpath('status').read_text()
            except FileNotFoundError:
                continue
            states.append(re.search(r'^State:\s+(\S)', status, re.M)[1])
        if states and all(value in ('T', 't') for value in states):
            return
        require(time.monotonic() < deadline, 'BFS threads did not stop')
        time.sleep(0.02)


def resume_bfs():
    global PAUSED
    # sudo may stop its monitor when its command is stopped: resume the saved
    # ancestry within this launch, never the operator or another process group.
    for pid, start in PAUSED:
        try:
            require(proc_identity(pid)[1] == start, 'PID reused while BFS paused')
            os.kill(pid, signal.SIGCONT)
        except (FileNotFoundError, ProcessLookupError):
            continue
    PAUSED = []


def bfs_regions(pid):
    regions, region = [], None
    text = Path('/proc/' + str(pid) + '/smaps').read_text()
    for line in text.splitlines():
        if re.match(r'^[0-9a-f]+-[0-9a-f]+ ', line):
            words = line.split(maxsplit=5)
            start, end = (int(value, 16) for value in words[0].split('-'))
            region = dict(start=start, end=end, perms=words[1], inode=int(words[4]),
                          path=words[5] if len(words) == 6 else '', swap_kib=0, page_kib=0, huge_kib=0)
            regions.append(region)
        elif region and line.startswith('Swap:'):
            region['swap_kib'] = int(line.split()[1])
        elif region and line.startswith('KernelPageSize:'):
            region['page_kib'] = int(line.split()[1])
        elif region and line.startswith('AnonHugePages:'):
            region['huge_kib'] = int(line.split()[1])
    return [region for region in regions if region['inode'] == 0 and region['perms'].endswith('p')
            and region['path'] in ('', '[heap]', '[stack]') and region['swap_kib'] > 0
            and region['page_kib'] == 4 and region['huge_kib'] == 0]


def audit_bfs(pid, pm, checkpoint):
    pause_bfs(pid)
    started = time.monotonic()
    require(Path('/proc/' + str(pid) + '/exe').resolve() == BFS.resolve(), 'BFS executable changed')
    regions = bfs_regions(pid)
    require(regions, 'No eligible ordinary anonymous swap regions')
    randomizer = random.Random(1200 + checkpoint)
    candidates, checked, changed, seen, covered = 0, 0, 0, set(), set()
    with (RUN / ('bfs-checkpoint-' + str(checkpoint) + '.identity.tsv')).open('w') as output:
        output.write('pid\tva\tvpn\ttype\toffset\tfound\treturned_vpn\taliases\n')
        while candidates < 100000 and checked < 2048:
            candidates += 1
            region = randomizer.choices(regions, weights=[r['swap_kib'] for r in regions])[0]
            va = region['start'] + randomizer.randrange((region['end'] - region['start']) // 4096) * 4096
            if va in seen:
                continue
            seen.add(va)
            before = os.pread(pm, 8, va // 4096 * 8)
            require(len(before) == 8, 'Short BFS pagemap read')
            entry = struct.unpack('=Q', before)[0]
            if not core.swapped(entry):
                continue
            typ, offset = entry & 31, (entry >> 5) & ((1 << 50) - 1)
            answer = lookup(core.CONTROL, offset)
            if os.pread(pm, 8, va // 4096 * 8) != before:
                changed += 1
                continue
            require(answer['attached'] == '1' and answer['found'] == '1' and int(answer['type']) == typ
                    and int(answer['pid']) == pid and int(answer['vpn']) == va // 4096
                    and int(answer['va'], 16) == va and answer['aliases'] == '1',
                    'BFS identity mismatch: va=' + hex(va) + ' response=' + str(answer))
            output.write(f"{pid}\t{va:#x}\t{va // 4096}\t{typ}\t{offset}\t1\t{answer['vpn']}\t1\n")
            checked += 1
            covered.add(region['start'])
    require(checked >= 1024, 'Too few stable BFS swap entries sampled: ' + str(checked))
    core.snapshot('bfs-checkpoint-' + str(checkpoint))
    result = dict(checkpoint=checkpoint, checked=checked, changed=changed, candidates=candidates,
                  eligible_vmas=len(regions), sampled_vmas=len(covered), pause_seconds=time.monotonic() - started)
    result_file('bfs-checkpoint-' + str(checkpoint) + '.json', result)
    resume_bfs()
    state('BFS CHECKPOINT PASS: ' + json.dumps(result))
    return result


def bfs_sample(pid, peaks):
    status = core.snapshot('bfs-live')
    raw = {name: (RS / name).read_text() for name in ('stats', 'mapping_raw', 'identity_status')}
    identity = core.fields(raw['identity_status'])
    peaks['identity_entries'] = max(peaks.get('identity_entries', 0), int(identity['entries']))
    used = 0
    for line in raw['mapping_raw'].splitlines()[2:]:
        server, tier, cap, live, retired, free = line.split()
        live, retired = int(live), int(retired)
        used += live + retired
        key = server + '/' + tier
        peaks[key] = max(peaks.get(key, 0), (live + retired) * 4096)
    process = {}
    try:
        text = Path('/proc/' + str(pid) + '/status').read_text()
        process = {line.split(':', 1)[0]: line.split(':', 1)[1].strip()
                   for line in text.splitlines() if line.startswith(('VmRSS:', 'VmSwap:', 'Threads:'))}
        peaks['threads'] = max(peaks.get('threads', 0), int(process.get('Threads', '0')))
    except FileNotFoundError:
        pass
    raw.update(time_ns=time.monotonic_ns(), process=process,
               cgroup_usage=(BFS_CG / 'memory.usage_in_bytes').read_text().strip(),
               cgroup_stat=(BFS_CG / 'memory.stat').read_text(),
               oom_control=(BFS_CG / 'memory.oom_control').read_text())
    with (RUN / 'bfs.samples.jsonl').open('a') as output:
        output.write(json.dumps(raw) + '\n')
    return used * 4096, status


def test_bfs():
    global BFS_JOB, BFS_PID
    state('BFS: launching the original script; waiting for the BFS executable after exec')
    console = (RUN / 'bfs.console.log').open('w')
    BFS_JOB = subprocess.Popen(['bash', str(BFS_SCRIPT)], stdout=console, stderr=subprocess.STDOUT,
                               start_new_session=True)
    started = time.monotonic()
    pid = None
    deadline = started + 60
    while pid is None:
        require(BFS_JOB.poll() is None, 'BFS script exited before BFS exec; inspect bfs.console.log')
        if (BFS_CG / 'cgroup.procs').exists():
            for value in (BFS_CG / 'cgroup.procs').read_text().split():
                candidate = int(value)
                try:
                    if Path('/proc/' + str(candidate) + '/exe').resolve() == BFS.resolve():
                        pid = candidate
                        break
                except FileNotFoundError:
                    continue
        require(time.monotonic() < deadline, 'Timed out waiting for BFS exec')
        if pid is None:
            time.sleep(0.02)
    BFS_PID = pid
    pause_bfs(pid)
    require(Path('/proc/' + str(pid) + '/exe').resolve() == BFS.resolve(), 'Bound program would not be BFS')
    fd, binding_id = bind(pid)
    pm = os.open('/proc/' + str(pid) + '/pagemap', os.O_RDONLY | os.O_CLOEXEC)
    cmdline = Path('/proc/' + str(pid) + '/cmdline').read_bytes().split(b'\0')
    require([value.decode() for value in cmdline if value] == [str(BFS), '-rounds', '3', str(GRAPH)],
            'Unexpected BFS command line')
    actual_group = {name: (BFS_CG / name).read_text().strip() for name in GROUP_SETTINGS}
    require(int(actual_group['memory.limit_in_bytes']) == 24 * 1024**3
            and int(actual_group['memory.memsw.limit_in_bytes']) >= 1 << 60
            and actual_group['memory.swappiness'] == '100', 'Unexpected BFS memory controls')
    result_file('bfs.target.json', dict(pid=pid, binding_id=binding_id, argv=[value.decode() for value in cmdline if value],
                                     group=actual_group, script=str(BFS_SCRIPT)))
    core.snapshot('bfs-bound')
    resume_bfs()
    checkpoints, peaks, next_check, last_progress = [], {}, 0, 0
    while BFS_JOB.poll() is None:
        now = time.monotonic()
        if now - started > 900:
            pause_bfs(pid)
            raise RuntimeError('BFS exceeded 15-minute execution budget; preserving the stopped process')
        used, identity = bfs_sample(pid, peaks)
        require(identity['bound'] == '1' and int(identity['binding_id']) == binding_id, 'BFS binding changed')
        if used >= 2 * 1024**3 and len(checkpoints) < 3 and now >= next_check:
            checkpoints.append(audit_bfs(pid, pm, len(checkpoints)))
            next_check = time.monotonic() + 25
        if now - last_progress >= 30:
            state(f'BFS RUNNING: elapsed={now-started:.1f}s used={used/1024**3:.2f} GiB '
                  f'entries={identity["entries"]} checkpoints={len(checkpoints)}')
            last_progress = now
        time.sleep(1)
    code = BFS_JOB.wait(timeout=10)
    console.close()
    elapsed = time.monotonic() - started
    save('bfs.exit', str(code) + '\n')
    core.snapshot('bfs-exited-bound')
    require(code == 0, 'BFS script failed: exit=' + str(code))
    text = (RUN / 'bfs.console.log').read_text()
    require(len(re.findall(r'Running time', text)) == 3, 'Expected three BFS timed rounds')
    require(len(checkpoints) == 3, 'BFS did not provide three identity checkpoints')
    require(peaks.get('threads', 0) >= 2, 'Concurrent BFS threads were not observed')
    require(not (BFS_CG / 'cgroup.procs').read_text().strip(), 'BFS tasks remain after script exit')
    save('bfs.cgroup-after-exit.stat', (BFS_CG / 'memory.stat').read_text())
    final_stats = (RS / 'stats').read_text()
    save('bfs.final-stats', final_stats)
    initial = parse_io((RUN / 'bfs-before.stats').read_text())
    current = parse_io(final_stats)
    deltas = {key: current[key] - initial[key] for key in current}
    for server in ('0', '1'):
        for tier in ('dram', 'pmem'):
            require(deltas[(server, tier, 'write')] > 0 and
                    deltas[(server, tier, 'read_sync')] + deltas[(server, tier, 'read_async')] > 0,
                    'Missing actual BFS I/O for ' + server + '/' + tier)
    close_control(fd)
    os.close(pm)
    check_detached('bfs-fd-closed')
    result = dict(exit_code=code, timed_rounds=3, elapsed_seconds=elapsed, identity_checkpoints=checkpoints,
                  observed_peaks=peaks, io_delta=[dict(server=s, tier=t, op=op, pages=count)
                                                for (s, t, op), count in deltas.items()],
                  cgroup_empty_after_exit=True, identity_detached_after_close=True,
                  scope='integration and sampled identity; BFS does not export a correctness checksum; no performance A/B')
    result_file('bfs.results.json', result)
    state('BFS PASS: normal exit, three timed rounds and three identity checkpoints')
    return result


def parse_io(text):
    return {tuple(words[:3]): int(words[3]) for words in
            (line.split() for line in text.splitlines()[2:-1])}


def setting(path):
    value = Path(path).read_text().strip()
    return value.split('[')[1].split(']')[0] if path.endswith('/transparent_hugepage/enabled') else value


def preflight():
    require(os.geteuid() == 0, 'Operator must start this controller with sudo')
    require(not (RUN / 'started').exists(), 'Already started; do not retry this directory')
    require(os.uname().version == '#9 SMP Sat Nov 28 00:02:37 CST 2026', 'Expected #9 kernel')
    require(Path('/sys/devices/system/cpu/online').read_text().strip() == '0-47', 'Unexpected online CPU set')
    require(not Path('/sys/module/rswap_client').exists(), 'Client already loaded')
    swaps = core.swaps()
    require(len(swaps) == 1 and swaps[0][0] == '/swap.img' and int(swaps[0][3]) == 0,
            'Expected only unused original /swap.img')
    require(CG_PARENT.is_dir() and os.access(CG_PARENT, os.W_OK), 'Memory cgroup must be writable on the operator host')
    require(not HMT_CG.exists() and not TRACE.exists(), 'Test cgroup or trace instance already exists')
    require(PROBE not in (TRACE_ROOT / 'kprobe_events').read_text(), 'Test kprobe already exists')
    if BFS_CG.exists():
        require(not (BFS_CG / 'cgroup.procs').read_text().strip(), 'hermit-test already contains processes')
    for path, digest in json.loads((RUN / 'artifacts.json').read_text()).items():
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, 'Artifact changed: ' + path)
    config = (REPO / 'remoteswap/client/rswap_client.conf').read_text()
    for line in ('NR_SERVERS=2', 'POOL_GIB=96', 'SERVER0_IP="10.0.0.13"', 'SERVER1_IP="10.0.0.20"'):
        require(line in config.splitlines(), 'Unexpected dual-node Client configuration')
    require(Path(BIG_SWAP).stat().st_size == 64 * 1024**3, 'Unexpected large swap size')
    require(GRAPH.stat().st_size == 11783351220, 'Unexpected graph file size')
    require((RUN / 'swap-small.img').stat().st_size == 128 * 1024**2, 'Unexpected small swap size')
    require('OMP_NUM_THREADS=16 exec ' + str(BFS) + ' -rounds 3 ' + str(GRAPH) in BFS_SCRIPT.read_text(),
            'User BFS script configuration changed')
    result_file('preflight.json', dict(kernel=os.uname().version, cpu_online='0-47',
                                     original_swaps=swaps, cgroup_writable=True, graph_bytes=GRAPH.stat().st_size))


def execute():
    preflight()
    save('started', time.strftime('%Y-%m-%d %H:%M:%S %z') + '\n')
    command(['dmesg'], 'dmesg.before')
    save('client.conf.before', (REPO / 'remoteswap/client/rswap_client.conf').read_text())
    save('swaps.before', Path('/proc/swaps').read_text())
    paths = [str(HMT / name) for name in core.FLAGS]
    paths += ['/proc/sys/vm/page-cluster', '/sys/kernel/mm/swap/readahead_win',
              '/sys/kernel/mm/transparent_hugepage/enabled']
    original = {path: setting(path) for path in paths}
    result_file('settings.before.json', original)
    group_before = ({name: (BFS_CG / name).read_text().strip() for name in GROUP_SETTINGS}
                    if BFS_CG.exists() else None)
    result_file('bfs.cgroup.before.json', group_before)
    for name in core.FLAGS:
        (HMT / name).write_text('N\n')
    Path('/proc/sys/vm/page-cluster').write_text('0\n')
    Path('/sys/kernel/mm/swap/readahead_win').write_text('1\n')
    Path('/sys/kernel/mm/transparent_hugepage/enabled').write_text('never\n')
    command(['swaplabel', BIG_SWAP], 'large-swap.label')
    command(['swaplabel', str(RUN / 'swap-small.img')], 'small-swap.label')
    command(['swapoff', '/swap.img'], 'original-swapoff.log')
    command(['bash', str(REPO / 'remoteswap/client/manage_rswap_client.sh'), 'install'], 'client-install.log')
    require(Path('/sys/module/rswap_client/version').read_text().strip() == '1.17', 'Expected Client 1.17')
    core.empty_mapping()
    small = RUN / 'swap-small.img'
    active_swap(small, 'small')
    results = dict(version=1, kernel=os.uname().version, client='1.17', servers=['10.0.0.13', '10.0.0.20'])
    results['reuse'] = test_reuse()
    results['hermit'] = test_hermit()
    results['lifecycle'] = test_lifecycle(small)
    stop_swap(small, 'small-final')
    active_swap(BIG_SWAP, 'large')
    core.snapshot('bfs-before')
    results['bfs'] = test_bfs()
    finish_run(results, original, group_before)


def finish_run(results, original, group_before):
    small = RUN / 'swap-small.img'
    stop_swap(BIG_SWAP, 'large-final')
    require(not OPEN_CONTROLS, 'Control descriptors still open')
    command(['bash', str(REPO / 'remoteswap/client/manage_rswap_client.sh'), 'uninstall'], 'client-uninstall.log')
    require(not Path('/sys/module/rswap_client').exists(), 'Client still loaded')
    for path, value in original.items():
        Path(path).write_text(value + '\n')
        require(setting(path) == value, 'Setting restoration failed: ' + path)
    require(not (BFS_CG / 'cgroup.procs').read_text().strip(), 'BFS cgroup remains populated')
    if group_before is None:
        BFS_CG.rmdir()
    else:
        for name in ('memory.limit_in_bytes', 'memory.memsw.limit_in_bytes', 'memory.swappiness'):
            (BFS_CG / name).write_text(group_before[name] + '\n')
            require((BFS_CG / name).read_text().strip() == group_before[name], 'BFS cgroup setting not restored')
    require(not HMT_CG.exists() and not TRACE.exists() and PROBE not in (TRACE_ROOT / 'kprobe_events').read_text(),
            'Test tracing/cgroup resources remain')
    require((REPO / 'remoteswap/client/rswap_client.conf').read_text() == (RUN / 'client.conf.before').read_text(),
            'Client configuration changed')
    command(['swapon', '/swap.img'], 'original-swap-restore.log')
    require([row[0] for row in core.swaps()] == ['/swap.img'], 'Original swap restoration failed')
    command(['dmesg'], 'dmesg.final')
    before, after = (RUN / 'dmesg.before').read_text(), (RUN / 'dmesg.final').read_text()
    require(after.startswith(before), 'Kernel log baseline rotated; review manually')
    added = after[len(before):]
    save('dmesg.added', added)
    suspicious = [line for line in added.splitlines() if
                  re.search(r'BUG:|WARNING:|Oops:|Call Trace:|general protection fault|I/O error|\berror\b|\bfailed\b', line, re.I)]
    require(not suspicious, 'New suspicious kernel logs: ' + '\n'.join(suspicious))
    # Only remove the file created specifically for this run, after all swap is off.
    require(str(small) not in [row[0] for row in core.swaps()], 'Small test swap still active')
    small.unlink()
    results['cleanup'] = dict(identity_reset=True, four_slot_pools_empty=True, client_unloaded=True,
                              settings_restored=True, cgroup_restored_or_removed=True,
                              tracing_removed=True, original_swap_restored=True, small_swap_removed=True)
    results['elapsed_seconds'] = time.monotonic() - START
    results['not_covered'] = ['BFS algorithm correctness checksum', 'long-duration stress', 'capacity exhaustion',
                              'fault injection', 'performance A/B', 'new EWMA and placement']
    result_file('results.json', results)
    save('SUCCESS', 'Stage 1 supplemental and original-script BFS tests passed\n')
    state('COMPLETE: reuse, Hermit, lifecycle, BFS and restoration passed; Servers may exit after disconnect')


def process_gone(pid):
    try:
        text = Path('/proc/' + str(pid) + '/stat').read_text()
        return text[text.rfind(')') + 2:].split()[0] == 'Z'
    except FileNotFoundError:
        return True


def held_controller_identity(target, expected_arguments):
    parent, _target_start = proc_identity(target)
    _grandparent, parent_start = proc_identity(parent)
    arguments = Path('/proc/' + str(parent) + '/cmdline').read_bytes().split(b'\0')
    arguments = [argument.decode() for argument in arguments if argument]
    require(parent != os.getpid() and arguments[1:] == expected_arguments,
            'Held helper parent is not the expected failed controller')
    return parent, parent_start


def stop_expected_process(pid, expected_start):
    require(pid != os.getpid() and proc_identity(pid)[1] == expected_start,
            'Process identity changed before cleanup')
    os.kill(pid, signal.SIGTERM)


def recovery_waiters(expected_arguments):
    found = []
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal() or int(path.name) == os.getpid():
            continue
        try:
            arguments = [value.decode(errors='surrogateescape') for value in (path / 'cmdline').read_bytes().split(b'\0') if value]
            if arguments[1:] == expected_arguments:
                found.append((int(path.name), proc_identity(int(path.name))[1]))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    require(len(found) <= 1, 'Multiple failed recovery controllers found; inspect manually')
    return found


def record_failure():
    save('failure.txt', traceback.format_exc())
    if BFS_JOB is not None and BFS_JOB.poll() is None and BFS_PID is not None and not PAUSED:
        try:
            pause_bfs(BFS_PID)
        except Exception:
            save('failure-pause.txt', traceback.format_exc())
    state('FAILED: preserving processes/control fds/swap/settings. Stop here and ask Codex to inspect failure.txt.')


def recover_trace_open(after_controller_check=False):
    require(os.geteuid() == 0, 'Operator must launch recovery with sudo')
    failure = (RUN / 'failure.txt').read_text()
    marker = 'recovery-controller-check.started' if after_controller_check else 'recovery.started'
    archive_name = 'failed-controller-check' if after_controller_check else 'failed-trace-open'
    require(not (RUN / marker).exists(), 'This recovery already attempted; inspect rather than retry')
    if after_controller_check:
        require('RuntimeError: Controller PID reused before cleanup' in failure
                and (RUN / 'recovery.started').exists() and not (RUN / 'recovery-small.swapoff.log').exists(),
                'Not the diagnosed failure before the first cleanup signal')
        original_failure = (RUN / 'failed-trace-open/failure.txt').read_text()
    else:
        original_failure = failure
    require('kprobe_events' in original_failure and "open('a')" in original_failure and 'Errno 22' in original_failure,
            'This recovery only handles the diagnosed tracefs append-open failure')
    require(not (RUN / 'hermit.results.json').exists() and not (RUN / 'bfs.results.json').exists(),
            'Later stages already ran; this recovery does not apply')
    require(os.uname().version == '#9 SMP Sat Nov 28 00:02:37 CST 2026', 'Kernel changed')
    require(Path('/sys/module/rswap_client/version').read_text().strip() == '1.17', 'Unexpected loaded Client')
    for name, value in (('nr_servers', '2'), ('sip', '10.0.0.13'), ('sip2', '10.0.0.20'), ('rmsize', '96')):
        require(Path('/sys/module/rswap_client/parameters/' + name).read_text().strip() == value,
                'Loaded Client parameter mismatch: ' + name)
    manifest = 'controller-check-artifacts.json' if after_controller_check else 'recovery-artifacts.json'
    for path, digest in json.loads((RUN / manifest).read_text()).items():
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, 'Recovery artifact changed: ' + path)
    small = RUN / 'swap-small.img'
    require([row[0] for row in core.swaps()] == [str(small)], 'Unexpected swap; preserve and inspect')
    require(TRACE.exists() and HMT_CG.exists() and PROBE not in (TRACE_ROOT / 'kprobe_events').read_text(),
            'Unexpected trace/cgroup state for this failure')
    if BFS_CG.exists():
        require(not (BFS_CG / 'cgroup.procs').read_text().strip(), 'BFS cgroup contains an unexpected workload')
    target = json.loads((RUN / 'hermit.metadata.json').read_text())['pid']
    require(Path('/proc/' + str(target) + '/exe').resolve() == RUN / 'pattern', 'Held helper identity changed')
    expected = [str(Path(__file__).resolve()), str(RUN)]
    old_controller, controller_start = held_controller_identity(target, expected)
    waiters = recovery_waiters(expected + ['--recover-trace-open']) if after_controller_check else []
    reuse = json.loads((RUN / 'reuse.results.json').read_text())
    require(reuse['actual_reused_offsets'] >= 128 and reuse['content_verified'], 'Prior reuse stage did not pass')
    status = core.snapshot('recovery-before')
    require(status['bound'] == '1' and int(status['pid']) == target, 'Held identity target changed')
    original = json.loads((RUN / 'settings.before.json').read_text())
    group_before = json.loads((RUN / 'bfs.cgroup.before.json').read_text())
    require((REPO / 'remoteswap/client/rswap_client.conf').read_text() == (RUN / 'client.conf.before').read_text(),
            'Client configuration changed')
    command(['dmesg'], 'recovery.dmesg.before')
    archive = RUN / archive_name
    require(not archive.exists(), 'Failure archive already exists')
    shutil.copytree(RUN, archive, ignore=shutil.ignore_patterns('swap-small.img', 'failed-trace-open', 'failed-controller-check'))
    save(marker, time.strftime('%Y-%m-%d %H:%M:%S %z') + '\n')
    state('RECOVERY: stopping only the identified failed controller/helper; retaining both RDMA Sessions')
    stop_expected_process(old_controller, controller_start)
    for pid, start in waiters:
        if not process_gone(pid):
            stop_expected_process(pid, start)
    deadline = time.monotonic() + 10
    while not process_gone(old_controller) or not process_gone(target):
        require(time.monotonic() < deadline, 'Failed controller/helper did not exit; stop and inspect')
        time.sleep(0.05)
    stop_swap(small, 'recovery-small')
    (TRACE / 'tracing_on').write_text('0\n')
    TRACE.rmdir()
    require(not (HMT_CG / 'cgroup.procs').read_text().strip(), 'Held helper cgroup still populated')
    HMT_CG.rmdir()
    (HMT / 'vaddr_swapout').write_text('N\n')
    active_swap(small, 'recovery-small-fresh')
    results = dict(version=1, kernel=os.uname().version, client='1.17', servers=['10.0.0.13', '10.0.0.20'],
                   reuse=reuse, recovery=dict(reason='tracefs append-open SEEK_END EINVAL',
                                              failed_controller_pid=old_controller, stopped_helper_pid=target,
                                              stopped_failed_recovery_controllers=[pid for pid, _ in waiters],
                                              controller_start_check_corrected=after_controller_check,
                                              reused_prior_reuse_result=True, rdma_sessions_retained=True,
                                              elapsed_scope='recovery start through final cleanup'))
    results['hermit'] = test_hermit()
    results['lifecycle'] = test_lifecycle(small)
    stop_swap(small, 'small-final')
    active_swap(BIG_SWAP, 'large')
    core.snapshot('bfs-before')
    results['bfs'] = test_bfs()
    finish_run(results, original, group_before)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument('--recover-trace-open', action='store_true',
                          help='operator-approved recovery of the diagnosed tracefs append-open failure')
    recovery.add_argument('--recover-controller-check', action='store_true',
                          help='operator-approved recovery after the parent/child start-time check failed before cleanup')
    args = parser.parse_args()
    RUN = args.run_dir.resolve()
    require(RUN.is_dir(), 'Prepare the run directory before starting')
    core.RUN = RUN
    START = time.monotonic()
    try:
        if args.recover_controller_check:
            recover_trace_open(after_controller_check=True)
        elif args.recover_trace_open:
            recover_trace_open()
        else:
            execute()
    except Exception:
        record_failure()
        traceback.print_exc()
        while True:
            time.sleep(1)

#!/usr/bin/env python3
"""Operator-started dual-Server core + original BFS integration test.

Run --prepare unprivileged first; then the operator starts this with sudo.
A failure stops and retains the session, swap and live workloads for inspection.
No formatting, Server management, kernel changes or automatic retry.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
import stage1_core_host as core
import stage1_extended_host as previous
sys.path.insert(0, str(core.REPO / 'remoteswap/tools'))
from rswap_hotness_check import read_vpn_reports
from rswap_pool import parse_bundle, collect, validate_settled

REPO, RS, HMT = core.REPO, core.RS, core.HMT
RUN = None
JOBS = []
START = 0
SCALE = 1 << 32
OWNED_TARGETS = []
LAST_KERNEL_CHECK = 0


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def save(name, value):
    (RUN / name).write_text(value)


def result(name, value):
    save(name, json.dumps(value, indent=2) + '\n')


def state(text):
    save('state', text + '\n')
    print(text, flush=True)


def command(args, name, timeout=180):
    with (RUN / name).open('w') as log:
        subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def snapshot(label):
    core.RUN = RUN
    identity = core.snapshot(label)
    placement = core.fields((RS / 'placement_raw').read_text())
    require(placement['state'] == 'inactive', 'Legacy PFN placement unexpectedly activated')
    # Legacy unknown-policy reasons are separate from the new VPN heat table.
    parsed = parse_bundle(collect(RS))
    for reason in (2, 3, 4):  # no_snapshot, no_mean, pfn_miss
        require(parsed['policy']['counters'][('reason', reason)][0] == 0,
                'Legacy unknown-policy counter increased: reason=' + str(reason))
    check_kernel()
    return identity


def check_kernel():
    global LAST_KERNEL_CHECK
    now = time.monotonic()
    if now - LAST_KERNEL_CHECK < 10:
        return
    LAST_KERNEL_CHECK = now
    current = subprocess.check_output(['dmesg'], text=True, timeout=10)
    before = (RUN / 'dmesg.before').read_text()
    require(current.startswith(before), 'Kernel log baseline rotated; stop for manual review')
    added = current[len(before):]
    save('dmesg.live-added', added)
    require(not re.search(r'BUG:|WARNING:|Oops:|Call Trace:|general protection fault|I/O error|\berror\b|\bfailed\b',
                          added, re.I), 'Suspicious live kernel messages; inspect dmesg.live-added')


def child_state(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        return text[text.rfind(')') + 2:].split()[0]
    except FileNotFoundError:
        return None


def target_finished(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        # PF_EXITING is visible before exit_mm finishes and before state Z.
        return fields[0] in ('Z', 'X') or bool(int(fields[6]) & 4)
    except FileNotFoundError:
        return True


def process_identity(pid):
    text = Path(f'/proc/{pid}/stat').read_text()
    parts = text[text.rfind(')') + 2:].split()
    return int(parts[1]), int(parts[19])


def spawn(args, label, env=None, stdin=None):
    out = (RUN / (label + '.console.log')).open('w', buffering=1)
    err = (RUN / (label + '.stderr')).open('w', buffering=1)
    job = subprocess.Popen(args, stdin=stdin, stdout=out, stderr=err, env=env, start_new_session=True)
    JOBS.append(job)
    out.close()
    err.close()
    return job


class Workload:
    def __init__(self):
        self.log = (RUN / 'controlled.workload.log').open('w', buffering=1)
        self.err = (RUN / 'controlled.workload.stderr').open('w')
        self.job = subprocess.Popen([str(RUN / 'pattern')], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=self.err, bufsize=0, start_new_session=True)
        JOBS.append(self.job)
        self.meta = self.receive()
        require(self.meta['event'] == 'ready' and self.meta['pid'] == self.job.pid, 'Workload metadata mismatch')
        self.base, self.pages = self.meta['base'] // 4096, self.meta['pages']
        self.pm = os.open(f'/proc/{self.job.pid}/pagemap', os.O_RDONLY | os.O_CLOEXEC)
        result('controlled.target.json', self.meta)

    def receive(self):
        require(select.select([self.job.stdout], [], [], 30)[0], 'Workload response timeout')
        line = self.job.stdout.readline()
        require(line, 'Workload exited before response')
        self.log.write(line.decode())
        return json.loads(line)

    def send(self, text):
        self.job.stdin.write((text + '\n').encode())
        self.job.stdin.flush()
        response = self.receive()
        require(response == dict(event='done', command=text), 'Bad workload response: ' + str(response))

    def ptes(self):
        data = os.pread(self.pm, self.pages * 8, self.base * 8)
        require(len(data) == self.pages * 8, 'Short controlled pagemap read')
        return struct.unpack('=' + 'Q' * self.pages, data)

    def finish(self):
        self.send('finish')
        require(self.job.wait(timeout=30) == 0, 'Controlled memory content verification failed')
        os.close(self.pm)
        self.job.stdin.close()
        self.job.stdout.close()
        self.log.close()
        self.err.close()


class Sampler:
    def __init__(self, pid, label, work=None):
        self.label = label
        self.out = RUN / (label + '.heat')
        env = dict(os.environ)
        env.pop('RSWAP_STAGE2_WATCH', None)
        env.pop('RSWAP_STAGE2_RANGE', None)
        self.hot = self.cold = []
        if work:
            half = work.pages // 2
            self.hot = [work.base + half * i // 32 for i in range(32)]
            self.cold = [work.base + half + half * i // 32 for i in range(32)]
            vpns = self.hot + self.cold + [work.meta['reserved'] // 4096]
            watch = RUN / 'controlled.watch'
            watch.write_text(''.join(str(v) + '\n' for v in vpns))
            env['RSWAP_STAGE2_WATCH'] = str(watch)
            env['RSWAP_STAGE2_RANGE'] = f'{work.base}:{work.pages}'
        self.job = spawn([str(RUN / 'sampler-audit'), '--publish', '--a-bit-window', '5',
                          '--ewma-window', '4', '--out', str(self.out), '--pid', str(pid)], label, env=env)
        self.stdout = RUN / (label + '.console.log')
        self.stderr = RUN / (label + '.stderr')
        self.first = time.monotonic()

    def records(self, prefix):
        rows = []
        lines = self.stdout.read_text().splitlines()
        for index, line in enumerate(lines):
            if line.startswith(prefix + ' '):
                try:
                    rows.append(json.loads(line[len(prefix) + 1:]))
                except json.JSONDecodeError:
                    # A concurrent write can leave only the last line partial.
                    require(index == len(lines) - 1, 'Malformed audit output')
        return rows

    def check_alive(self):
        require(self.job.poll() is None, f'{self.label} sampler exited: {self.job.returncode}; inspect stderr')
        require(child_state(self.job.pid) not in ('T', 't'), f'{self.label} sampler stopped after audit failure')
        require('AUDIT_ERROR' not in self.stdout.read_text(), f'{self.label} audit failure')

    def wait_window(self, window, timeout=150):
        deadline = time.monotonic() + timeout
        while True:
            self.check_alive()
            audits = self.records('AUDIT')
            if self.out.is_dir():
                os.chmod(self.out, 0o755)  # Make evidence readable by the nonroot operator.
            if len(audits) > window and (self.out / 'summary.tsv').exists():
                lines = (self.out / 'summary.tsv').read_text().splitlines()
                if len(lines) >= window + 2:
                    self.validate()
                    return audits[window]
            require(time.monotonic() < deadline, f'{self.label} timed out before EWMA {window}')
            time.sleep(.1)

    def watched(self, window):
        return {r['vpn']: r for r in self.records('WATCH') if r['window'] == window}

    def validate(self, final=False):
        audits = self.records('AUDIT')
        rows, distribution = read_vpn_reports(self.out / 'summary.tsv', allow_pending=not final)
        require(len(rows) <= len(audits), 'Summary has no matching committed audit')
        for i, row in enumerate(rows):
            a = audits[i]
            require(a['window'] == i and a['unknown'] == 0 and a['checked_queries'] > 0,
                    'Missing snapshot, wrong window or unknown VPN')
            require(int(row['pages']) == a['pages'], 'Summary/Client pages mismatch')
            mean = a['sum_heat'] / SCALE / a['pages'] if a['pages'] else 0
            require(abs(float(row['mean_hotness']) - mean) <= .000050001, 'Summary/Client mean mismatch')
            counts = [int(cell.split('(')[0]) for cell in row['hot/cold'].split('/')]
            require(counts == [a['hot_pages'], a['cold_pages']], 'Summary/Client hot/cold mismatch')
            bins = distribution[i]
            for key, count in zip(list(bins)[4:], a['bins']):
                require(int(bins[key].split('(')[0]) == count, 'Distribution/wire heat-bin mismatch')
        text = (self.out / 'summary.tsv').read_text().lower()
        require('unknown' not in text and '\tnan' not in text and '\t-' not in text,
                'Unknown/invalid heat text in new summary')
        require({p.name for p in self.out.iterdir()} == {'summary.tsv', 'hot_distribute.tsv'},
                'Unexpected production sampler output files')
        return audits, rows

    def stop(self):
        self.check_alive()
        self.job.send_signal(signal.SIGTERM)
        code = self.job.wait(timeout=90)
        require(code == 143, f'{self.label} sampler did not stop normally: {code}')
        return self.finished()

    def finished(self):
        require(self.stderr.read_text() == '', f'{self.label} sampler reported an error: ' + self.stderr.read_text())
        require('LIFECYCLE closed_identity_rejected=1' in self.stdout.read_text(), 'Old binding did not become stale')
        audits, rows = self.validate(final=True)
        result(self.label + '.results.json', dict(snapshots=audits, summaries=rows, unknown=0,
                                               old_binding_rejected=True))
        require(core.fields((RS / 'identity_status').read_text())['bound'] == '0', 'Binding remained after sampler exit')
        return audits


def update(h, hits):
    return (16 * h + hits * SCALE + 10) // 20


def controlled():
    state('CONTROLLED: hot/cold, EWMA, swapped, empty PTE and refault; default 5s/4 rounds')
    work = Workload()
    sampler = Sampler(work.job.pid, 'controlled', work)
    baseline = sampler.wait_window(0)
    require(baseline['range_records'] == work.pages and baseline['range_pages'] == work.pages, 'Initial anonymous population mismatch')
    require(baseline['range_sum'] == 0, 'Initial heat must be zero')
    for window, expected in ((1, update(0, 4)), (2, update(update(0, 4), 4))):
        sampler.wait_window(window)
        watched = sampler.watched(window)
        require(all(v in watched and watched[v]['in_scope'] for v in sampler.hot + sampler.cold), 'Current target VPN missing from snapshot')
        hot_exact = sum(abs(watched[v]['heat'] - expected) <= 1 for v in sampler.hot)
        require(hot_exact >= 29, 'Hot pages did not follow EWMA: ' + str(hot_exact))
        require(all(watched[v]['heat'] == 0 for v in sampler.cold), 'Cold target pages unexpectedly became hot')
        state(f'CONTROLLED EWMA {window}: hot_exact={hot_exact}/32 cold_zero=32/32 unknown=0')
    before = work.ptes()
    work.send('out')
    deadline = time.monotonic() + 15
    while sum(core.swapped(e) for e in work.ptes()[:work.pages // 2]) < work.pages * .45:
        require(time.monotonic() < deadline, 'Insufficient actual controlled swap-out')
        time.sleep(.1)
    a3 = sampler.wait_window(3)
    w2, w3 = sampler.watched(2), sampler.watched(3)
    require(a3['range_pages'] == work.pages, 'Swapped target pages excluded from current statistics')
    for v in sampler.hot:
        require(w3[v]['in_scope'] == 1 and abs(w3[v]['heat'] - update(w2[v]['heat'], 0)) <= 1,
                'Swapped VPN history did not decay correctly')
    work.send('in')
    after = work.ptes()
    changes = core.pfn_changes(before, after)
    require(changes['changed'] > 0, 'No actual PFN change observed; cannot certify PFN-change history')
    sampler.wait_window(4)
    w4 = sampler.watched(4)
    for v in sampler.hot:
        require(w4[v]['in_scope'] == 1 and any(abs(w4[v]['heat'] - update(w3[v]['heat'], h)) <= 1 for h in range(5)),
                'Swap-in lost or corrupted VPN history')
    work.send('discard')
    require(all(not e & ((1 << 63) | (1 << 62)) for e in work.ptes()[:work.pages // 2]), 'DONTNEED did not empty target PTEs')
    a5 = sampler.wait_window(5)
    w5 = sampler.watched(5)
    require(a5['range_records'] == work.pages and a5['range_pages'] == work.pages // 2,
            'Empty target PTEs must retain history but not count toward pages')
    for v in sampler.hot:
        require(w5[v]['in_scope'] == 0 and abs(w5[v]['heat'] - update(w4[v]['heat'], 0)) <= 1,
                'DONTNEED lost history or failed to decay')
    work.send('refault')
    a6 = sampler.wait_window(6)
    w6 = sampler.watched(6)
    require(a6['range_records'] == work.pages and a6['range_pages'] == work.pages, 'Refault population not restored')
    for v in sampler.hot:
        require(w6[v]['in_scope'] == 1 and any(abs(w6[v]['heat'] - update(w5[v]['heat'], h)) <= 1 for h in range(5)),
                'Refault reset VPN history')
    sampler.stop()
    work.finish()
    snapshot('controlled-finished')
    result('controlled.checks.json', dict(pfn_changes=changes, pages=work.pages, heat_groups=6,
                                         content_swap_verified=True, content_dontneed_verified=True, unknown=0))
    state('CONTROLLED PASS: hot/cold, six EWMA groups, history, current population, contents and fd close')


def bfs():
    state('BFS: original script, actual BFS PID, 24 GiB cgroup; sampling outside the workload cgroup')
    before_io = previous.parse_io((RS / 'stats').read_text())
    job = spawn(['bash', str(previous.BFS_SCRIPT)], 'bfs')
    started = time.monotonic()
    pid = None
    while pid is None:
        require(job.poll() is None, 'Original BFS script exited before BFS exec')
        if (previous.BFS_CG / 'cgroup.procs').exists():
            for value in (previous.BFS_CG / 'cgroup.procs').read_text().split():
                candidate = int(value)
                try:
                    if Path(f'/proc/{candidate}/exe').resolve() == previous.BFS.resolve():
                        pid = candidate
                        break
                except FileNotFoundError:
                    continue
        require(time.monotonic() - started < 60, 'BFS exec timeout')
        if pid is None:
            time.sleep(.02)
    argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    require([a.decode() for a in argv if a] == [str(previous.BFS), '-rounds', '3', str(previous.GRAPH)], 'BFS argv changed')
    ancestor, start_token = process_identity(pid)
    seen = set()
    while ancestor != job.pid and ancestor > 1 and ancestor not in seen:
        seen.add(ancestor)
        ancestor = process_identity(ancestor)[0]
    require(ancestor == job.pid, 'BFS does not belong to this controller script')
    OWNED_TARGETS.append((pid, start_token))
    groups = {n: (previous.BFS_CG / n).read_text().strip() for n in previous.GROUP_SETTINGS}
    require(int(groups['memory.limit_in_bytes']) == 24 * 1024**3 and groups['memory.swappiness'] == '100'
            and int(groups['memory.memsw.limit_in_bytes']) >= 1 << 60, 'BFS cgroup changed')
    result('bfs.target.json', dict(pid=pid, argv=[a.decode() for a in argv if a], group=groups))
    sampler = Sampler(pid, 'bfs-sampler')
    next_progress = 0
    peak_slots = peak_sampler_rss = cpu_ticks = 0
    with (RUN / 'bfs.pool-samples.jsonl').open('w') as output:
        while job.poll() is None:
            elapsed = time.monotonic() - started
            require(elapsed < 720, 'BFS exceeded 12-minute budget; preserve rather than retry')
            if sampler.job.poll() is not None:
                require(sampler.job.returncode == 0 and target_finished(pid),
                        'BFS sampler exited while its target was still alive, or reported an error')
                require(job.wait(timeout=90) == 0, 'BFS wrapper failed after target exit')
                break
            require(child_state(sampler.job.pid) not in ('T', 't') and
                    'AUDIT_ERROR' not in sampler.stdout.read_text(), 'BFS sampler audit stopped')
            if sampler.out.exists():
                os.chmod(sampler.out, 0o755)
            raw = collect(RS)
            parsed = parse_bundle(raw)
            used = sum(p['used'] for p in parsed['pools'].values())
            peak_slots = max(peak_slots, used)
            try:
                status = Path(f'/proc/{sampler.job.pid}/status').read_text()
                stat = Path(f'/proc/{sampler.job.pid}/stat').read_text()
                times = stat[stat.rfind(')') + 2:].split()
                cpu_ticks = max(cpu_ticks, int(times[11]) + int(times[12]))
            except FileNotFoundError:
                require(sampler.job.poll() is not None, 'Sampler proc files disappeared unexpectedly')
                status = ''
            rss = re.search(r'^VmRSS:\s+(\d+) kB', status, re.M)
            if rss:
                peak_sampler_rss = max(peak_sampler_rss, int(rss[1]) * 1024)
            raw['sampler_resources'] = dict(peak_rss_bytes=peak_sampler_rss, cpu_ticks=cpu_ticks)
            output.write(json.dumps(raw) + '\n')
            output.flush()
            snapshot('bfs-live')
            if elapsed >= next_progress:
                state(f'BFS RUNNING {elapsed:.1f}s remote={used*4096/1024**3:.2f} GiB '
                      f'EWMA={len(sampler.records("AUDIT"))-1} sampler_RSS={peak_sampler_rss/1024**2:.1f} MiB')
                next_progress = elapsed + 30
            time.sleep(1)
    require(job.wait(timeout=10) == 0, 'BFS script failed')
    # The sampler should observe target exit and close its binding normally.
    code = sampler.job.wait(timeout=90)
    require(code == 0, f'BFS sampler failed during target exit: {code}')
    audits = sampler.finished()
    require(len(audits) >= 3, 'BFS completed fewer than two full EWMA updates; coverage insufficient')
    console = (RUN / 'bfs.console.log').read_text()
    require(len(re.findall(r'Running time', console)) == 3, 'BFS did not finish three timed rounds')
    require(not (previous.BFS_CG / 'cgroup.procs').read_text().strip(), 'BFS tasks remain')
    current_io = previous.parse_io((RS / 'stats').read_text())
    delta = {k: current_io[k] - before_io[k] for k in current_io}
    for server in ('0', '1'):
        for tier in ('dram', 'pmem'):
            require(delta[(server, tier, 'write')] > 0 and delta[(server, tier, 'read_sync')] +
                    delta[(server, tier, 'read_async')] > 0, 'BFS missing actual RDMA I/O: ' + server + '/' + tier)
    snapshot('bfs-finished')
    result('bfs.results.json', dict(elapsed_seconds=time.monotonic() - started, timed_rounds=3,
                                   ewma_updates=len(audits)-1, peak_remote_bytes=peak_slots*4096,
                                   peak_sampler_rss_bytes=peak_sampler_rss, unknown=0,
                                   sampled_sampler_cpu_seconds=cpu_ticks/os.sysconf('SC_CLK_TCK'),
                                   io_delta=[dict(server=s,tier=t,op=op,pages=n) for (s,t,op),n in delta.items()],
                                   scope='functional integration; no algorithm checksum or performance A/B'))
    state('BFS PASS: normal exit, three timed rounds, committed EWMA, no unknown or I/O errors')


def prepare():
    require(not RUN.exists(), 'Preparation directory must be new')
    RUN.mkdir(mode=0o755)
    for source, output, extra in [('stage2_pattern.c','pattern',['-pthread']),
                                  ('stage2_sampler_audit.c','sampler-audit',[])]:
        command(['gcc','-O2','-g','-std=gnu11','-Wall','-Wextra','-Werror',
                 str(REPO / 'remoteswap/tests' / source), *extra, '-o', str(RUN/output)], output+'.build.log')
    command(['make','-C',str(REPO/'remoteswap/tools')], 'tools.build.log')
    command(['make','-C',str(REPO/'remoteswap/client'), 'KDIR='+str(REPO/'linux-5.14-rc5'),'-j12'], 'client.build.log')
    files = [RUN/'pattern', RUN/'sampler-audit', Path(__file__), previous.BFS_SCRIPT,
             REPO/'remoteswap/client/rswap-client.ko', REPO/'remoteswap/client/rswap_client.conf']
    files += list((REPO/'remoteswap/tools').glob('rswap_hotness.[ch]'))
    files += [REPO/'remoteswap/tests/stage2_pattern.c', REPO/'remoteswap/tests/stage2_sampler_audit.c',
              REPO/'remoteswap/tests/stage1_core_host.py', REPO/'remoteswap/tests/stage1_extended_host.py',
              REPO/'remoteswap/tools/rswap_hotness_check.py', REPO/'remoteswap/tools/rswap_pool.py']
    result('artifacts.json', {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    state('PREPARED: binaries built; operator must start both Servers before sudo controller')


def execute():
    global START
    START = time.monotonic()
    require(os.geteuid() == 0, 'Operator must launch the controller with sudo')
    require(not (RUN/'started').exists(), 'Already started; never automatically retry a test directory')
    require(os.uname().version == '#9 SMP Sat Nov 28 00:02:37 CST 2026', 'Expected kernel #9')
    require(not Path('/sys/module/rswap_client').exists(), 'Client already loaded')
    swaps = core.swaps()
    require(len(swaps)==1 and swaps[0][0]=='/swap.img' and int(swaps[0][3])==0, 'Expected original unused /swap.img only')
    require(previous.GRAPH.stat().st_size==11783351220 and Path(previous.BIG_SWAP).stat().st_size==64*1024**3,
            'BFS graph or existing swap size changed')
    require(previous.CG_PARENT.is_dir(), 'Memory cgroup v1 required')
    for path, digest in json.loads((RUN/'artifacts.json').read_text()).items():
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest, 'Prepared artifact changed: '+path)
    if previous.BFS_CG.exists():
        require(not (previous.BFS_CG/'cgroup.procs').read_text().strip(), 'BFS cgroup already occupied')
    require(str(previous.BFS_CG) not in Path('/proc/self/cgroup').read_text(), 'Controller must be outside BFS cgroup')
    group_before = {n:(previous.BFS_CG/n).read_text().strip() for n in previous.GROUP_SETTINGS} if previous.BFS_CG.exists() else None
    result('bfs.cgroup.before.json', group_before)
    settings = {str(HMT/n):previous.setting(str(HMT/n)) for n in core.FLAGS}
    settings.update({p:previous.setting(p) for p in ['/proc/sys/vm/page-cluster',
                    '/sys/kernel/mm/swap/readahead_win','/sys/kernel/mm/transparent_hugepage/enabled']})
    result('settings.before.json',settings)
    command(['dmesg'],'dmesg.before')
    save('started',time.strftime('%Y-%m-%d %H:%M:%S %z')+'\n')
    for name in core.FLAGS:
        (HMT/name).write_text('N\n')
    Path('/proc/sys/vm/page-cluster').write_text('0\n')
    Path('/sys/kernel/mm/swap/readahead_win').write_text('1\n')
    Path('/sys/kernel/mm/transparent_hugepage/enabled').write_text('never\n')
    command(['swaplabel',previous.BIG_SWAP],'large-swap.label')
    command(['swapoff','/swap.img'],'original-swapoff.log')
    command(['bash',str(REPO/'remoteswap/client/manage_rswap_client.sh'),'install'],'client-install.log')
    require(Path('/sys/module/rswap_client/version').read_text().strip()=='1.18','Expected Client 1.18')
    core.empty_mapping()
    command(['swapon',previous.BIG_SWAP],'test-swapon.log')
    snapshot('baseline')
    controlled()
    bfs()
    snapshot('before-cleanup')
    validate_settled(parse_bundle(collect(RS))['policy'])
    command(['swapoff',previous.BIG_SWAP],'test-swapoff.log',timeout=300)
    snapshot('after-swapoff')
    core.empty_mapping()
    command(['bash',str(REPO/'remoteswap/client/manage_rswap_client.sh'),'uninstall'],'client-uninstall.log')
    for path,value in settings.items():
        Path(path).write_text(value+'\n')
        require(previous.setting(path)==value,'Setting restoration failed: '+path)
    require(not (previous.BFS_CG/'cgroup.procs').read_text().strip(),'BFS processes remain')
    if group_before is None:
        previous.BFS_CG.rmdir()
    else:
        for name in ('memory.limit_in_bytes','memory.memsw.limit_in_bytes','memory.swappiness'):
            (previous.BFS_CG/name).write_text(group_before[name]+'\n')
            require((previous.BFS_CG/name).read_text().strip()==group_before[name],'Cgroup setting not restored')
    command(['swapon','/swap.img'],'original-swap-restored.log')
    require([r[0] for r in core.swaps()]==['/swap.img'],'Original swap not restored')
    require(not Path('/sys/module/rswap_client').exists(),'Client still loaded')
    command(['dmesg'],'dmesg.final')
    before,after=(RUN/'dmesg.before').read_text(),(RUN/'dmesg.final').read_text()
    require(after.startswith(before),'Kernel baseline rotated; manual review required')
    added=after[len(before):]
    save('dmesg.added',added)
    require(not re.search(r'BUG:|WARNING:|Oops:|Call Trace:|general protection fault|I/O error|\berror\b|\bfailed\b',added,re.I),
            'Suspicious new kernel messages; inspect dmesg.added')
    result('results.json',dict(client='1.18',kernel=os.uname().version,unknown=0,
                              seconds=time.monotonic()-START,controlled=json.loads((RUN/'controlled.checks.json').read_text()),
                              bfs=json.loads((RUN/'bfs.results.json').read_text()),cleanup='restored'))
    save('SUCCESS','Stage 2 core, VPN unknown checks, original BFS and restoration passed\n')
    state('COMPLETE: all checks passed; Servers may exit after Client disconnect')


def pause_owned_jobs():
    for pid, start_token in OWNED_TARGETS:
        try:
            if process_identity(pid)[1] == start_token:
                os.kill(pid, signal.SIGSTOP)
        except (FileNotFoundError, ProcessLookupError):
            pass
    for job in JOBS:
        if job.poll() is None:
            try:
                os.killpg(job.pid,signal.SIGSTOP)
            except ProcessLookupError:
                pass


def main():
    global RUN
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--prepare',action='store_true')
    args=parser.parse_args()
    RUN=args.directory.resolve()
    if args.prepare:
        prepare()
        return
    require(RUN.is_dir(),'Run --prepare before sudo execution')
    try:
        execute()
    except Exception:
        save('failure.txt',traceback.format_exc())
        pause_owned_jobs()
        state('FAILED: paused owned workloads, retained RDMA/swap/settings. Stop and inspect failure.txt with Codex.')
        # Keep Python pipes and workload children alive, as explicitly requested.
        while True:
            time.sleep(30)


if __name__=='__main__':
    main()

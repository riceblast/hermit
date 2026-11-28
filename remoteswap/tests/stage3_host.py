#!/usr/bin/env python3
"""Operator-started Client 1.19 core/capacity/BFS acceptance on two Servers.

Prepare without sudo, then launch on the real host with sudo. No kernel install,
Server management, swap formatting, automatic retries or unsolicited BFS kill.
Failures pause only owned jobs and retain the environment for inspection.
"""
import argparse
from collections import Counter
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
import stage2_host as old
import stage1_core_host as core
import stage1_extended_host as previous
from rswap_pool import collect, parse_bundle, validate_settled
from rswap_trace import read_decisions
from rswap_analysis import check_store_results
from rswap_hotness_check import check_buffer_stats
from rswap_run import register
from stage3_bfs_report import EwmaReport, occupancy, WARMUP_GROUPS, ADAPTIVE_REVIEW_GROUPS

REPO, RS, HMT = core.REPO, core.RS, core.HMT
RUN = None
TRACE = previous.TRACE_ROOT / 'instances/hermit_p14'
LAYOUT = ((16, 32), (16, 32))
SCALE = 1 << 32
START = 0
OPEN_FDS = set()
TEST_GROUPS = []
BFS_INTERVAL = 10
BFS_EWMA_WINDOW = 2
require, fields = core.require, core.fields


def save(name, text):
    (RUN / name).write_text(text)


def result(name, value):
    save(name, json.dumps(value, indent=2) + '\n')


def state(text):
    save('state', text + '\n')
    print(text, flush=True)


def command(args, name, timeout=180):
    with (RUN / name).open('w') as log:
        subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def snapshot(label):
    core.snapshot(label)
    parsed = parse_bundle(collect(RS))
    for reason in (2, 3, 4):
        require(parsed['policy']['counters'][('reason', reason)][0] == 0,
                f'Unexpected unknown-policy decision, reason={reason}')
    old.check_kernel()
    return parsed


def settle():
    deadline = time.monotonic() + 60
    while True:
        try:
            validate_settled(parse_bundle(collect(RS))['policy'])
            return
        except ValueError:
            require(time.monotonic() < deadline, 'Store completion counters failed to settle')
            time.sleep(.1)


def empty():
    deadline = time.monotonic() + 60
    while True:
        try:
            core.empty_mapping()
            return
        except RuntimeError:
            require(time.monotonic() < deadline, 'Mappings remain after owned workload exit')
            time.sleep(.1)


def reset_test_swap(label):
    """Isolate completed stages; drain per-CPU free-slot caches via swapoff."""
    require(not OPEN_FDS,'A policy fd is still open; do not reset swap')
    require(all(job.poll() is not None for job in old.JOBS),'Owned workload still active; do not reset swap')
    require(fields((RS/'identity_status').read_text())['bound']=='0','Identity still bound; do not reset swap')
    require(fields((RS/'placement_raw').read_text())['state']=='inactive','Policy still active; do not reset swap')
    require([r[0] for r in core.swaps()]==[previous.BIG_SWAP],'Unexpected swap areas; do not reset')
    snapshot(label+'-before-drain')
    command(['swapoff',previous.BIG_SWAP],label+'.swapoff.log',timeout=300)
    require(not core.swaps(),'Swap remained active after drain')
    empty(); snapshot(label+'-drained')
    command(['swapon',previous.BIG_SWAP],label+'.swapon.log')
    empty(); snapshot(label+'-ready')
    state('SWAP RESET: '+label+'; free-slot caches drained; both RDMA Sessions retained')


def vpn_sample_filter(entry):
    declarations=re.findall(r'\bfield:([^;]+);',(entry/'format').read_text())
    names=set()
    for declaration in declarations:
        match=re.search(r'([A-Za-z_]\w*)(?:\[[^]]*\])?$',declaration.strip())
        if match: names.add(match.group(1))
    require({'slot','ret'}<=names,'Kernel vpn_decision schema lacks slot/ret fields')
    return 'slot < 64 && ret == 0\n'


def check_trace_schema():
    require((TRACE/'tracing_on').read_text().strip()=='0','Trace schema check requires tracing off')
    entry=TRACE/'events/rswap/vpn_decision'
    expression=vpn_sample_filter(entry)
    try:
        (entry/'filter').write_text(expression)
        result('trace-schema.json',dict(expression=expression.strip(),accepted=(entry/'filter').read_text(),
                                         format=(entry/'format').read_text()))
    finally:
        (entry/'filter').write_text('0\n')


class Trace:
    def __init__(self, label, sampled=False):
        self.label, self.sampled = label, sampled
        expression=vpn_sample_filter(TRACE/'events/rswap/vpn_decision') if sampled else None
        (TRACE / 'tracing_on').write_text('0\n')
        (TRACE / 'trace').write_text('')
        for event in ('vpn_decision', 'decision_raw', 'app_decision', 'decision', 'store_result'):
            entry = TRACE / 'events/rswap' / event
            (entry / 'enable').write_text('0\n')
            (entry / 'filter').write_text('0\n')
            if sampled and event != 'vpn_decision':
                continue
            if sampled:
                (entry / 'filter').write_text(expression)
            (entry / 'enable').write_text('1\n')
        (TRACE / 'tracing_on').write_text('1\n')

    def finish(self):
        settle()
        (TRACE / 'tracing_on').write_text('0\n')
        text = (TRACE / 'trace').read_text()
        save(self.label + '.trace', text)
        stats = '\n'.join(p.read_text() for p in sorted((TRACE / 'per_cpu').glob('cpu*/stats')))
        save(self.label + '.buffer-stats', stats)
        check_buffer_stats(RUN / (self.label + '.buffer-stats'))
        require('LOST' not in text, 'Trace reported event loss')
        return text


def validate_trace(label, text, binding=None):
    summary = Counter()
    rows = read_decisions(text.splitlines(), LAYOUT, summary)
    if binding is not None:
        rows = {seq: d for seq, d in rows.items() if d.get('binding') == binding}
    require(rows, label + ': no matching actual Store decisions')
    require(all(d.get('reason') not in (2, 3, 4) for d in rows.values()), 'Unknown target decision')
    # Other processes may legitimately Store during the same capture. Keep
    # their evidence in the file, but join only this selected allocation set.
    matching_results = []
    for line in text.splitlines():
        if 'store_result:' in line and int(fields(line.split('store_result:',1)[1])['alloc_seq']) in rows:
            matching_results.append(line)
    outcomes = check_store_results(matching_results, rows)
    require(not outcomes.get('post_error',0) and not outcomes.get('wc_error',0),
            label+': Store post/WC reported an error')
    result(label + '.decisions.json', dict(summary=dict(summary), matched=len(rows), outcomes=outcomes))
    return list(rows.values())


class Sampler(old.Sampler):
    def __init__(self, pid, label, window=4, work=None, interval=5):
        self.label, self.out = label, RUN / (label + '.heat')
        require(not self.out.exists(),'Sampler output must be new: '+str(self.out))
        env = dict(os.environ)
        env.pop('RSWAP_STAGE2_WATCH', None)
        env.pop('RSWAP_STAGE2_RANGE', None)
        self.hot, self.cold = [], []
        if work:
            half = work.pages // 2
            self.hot = [work.base + half*i//32 for i in range(32)]
            self.cold = [work.base + half + half*i//32 for i in range(32)]
            watch = RUN / (label + '.watch')
            watch.write_text(''.join(str(v)+'\n' for v in self.hot+self.cold+[work.meta['reserved']//4096]))
            env['RSWAP_STAGE2_WATCH'] = str(watch)
            env['RSWAP_STAGE2_RANGE'] = f'{work.base}:{work.pages}'
        self.job = old.spawn([str(RUN/'sampler-audit'), '--placement', '--a-bit-window', str(interval),
                              '--ewma-window', str(window), '--out', str(self.out), '--pid', str(pid)],
                             label, env=env)
        self.stdout, self.stderr = RUN/(label+'.console.log'), RUN/(label+'.stderr')
        self.first = time.monotonic()


class Pattern:
    def __init__(self, label, mib, group=None):
        self.label = label
        self.log = (RUN/(label+'.console.jsonl')).open('w', buffering=1)
        self.err = (RUN/(label+'.stderr')).open('w')
        def join():
            (group/'tasks').write_text(str(os.getpid()))
        self.job = subprocess.Popen([str(RUN/'capacity-pattern'),str(mib)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=self.err, bufsize=0,
                                    start_new_session=True, preexec_fn=join if group else None)
        old.JOBS.append(self.job)
        self.meta = self.receive()
        require(self.meta.get('event') == 'ready' and self.meta['pid'] == self.job.pid, 'Pattern metadata')
        self.base, self.pages = self.meta['base']//4096, self.meta['bytes']//4096
        self.pm = os.open(f'/proc/{self.job.pid}/pagemap', os.O_RDONLY|os.O_CLOEXEC)
        result(label+'.target.json', self.meta)

    def receive(self):
        require(select.select([self.job.stdout],[],[],240)[0], self.label+': response timeout')
        line = self.job.stdout.readline()
        require(line, self.label+': exited before response')
        self.log.write(line.decode())
        return json.loads(line)

    def send(self, text):
        self.job.stdin.write((text+'\n').encode()); self.job.stdin.flush()
        require(self.receive() == dict(event='done',command=text), self.label+': bad response')

    def finish(self):
        self.send('finish')
        require(self.job.wait(timeout=60) == 0, self.label+': content verification failed')
        self.job.stdin.close(); self.job.stdout.close(); self.log.close(); self.err.close()
        os.close(self.pm)


class Policy:
    """Explicit controlled snapshots for edge/capacity tests; not measured heat."""
    def __init__(self, pid):
        self.identity = os.open(RS/'identity', os.O_RDWR|os.O_CLOEXEC)
        OPEN_FDS.add(self.identity)
        os.write(self.identity, f'bind {pid}\n'.encode())
        self.binding = int(fields(os.pread(self.identity,512,0).decode())['binding_id'])
        self.heat = os.open(RS/'vpn_heat', os.O_RDWR|os.O_CLOEXEC)
        OPEN_FDS.add(self.heat)
        self.write(6)

    def write(self, op, window=0, count=0, pages=0, total=0, payload=b''):
        data = struct.pack('=II6Q',1,op,self.binding,window,count,pages,total,0)+payload
        require(os.write(self.heat,data) == len(data), 'Short VPN heat write')

    def publish(self, window, records=()):
        pages = sum(r[2] for r in records)
        total = sum(r[1] for r in records if r[2])
        self.write(2,window,len(records),pages,total)
        for i in range(0,len(records),4096):
            batch = records[i:i+4096]
            data = b''.join(struct.pack('=QQII',vpn,h,scope,0) for vpn,h,scope in batch)
            self.write(3,window,len(batch),payload=data)
        self.write(4,window)

    def close(self):
        for fd in (self.heat,self.identity):
            os.close(fd); OPEN_FDS.remove(fd)


def controlled():
    state('CORE: real 5s/4-round sampler; Warmup, fifth commit, hot/cold and contents')
    work = old.Workload()
    sampler = Sampler(work.job.pid,'controlled',work=work)
    sampler.wait_window(0)
    a1 = sampler.wait_window(1)
    watched = sampler.watched(1)
    require(all(v in watched for v in sampler.hot+sampler.cold), 'Known target history missing')
    require(sum(abs(watched[v]['heat']-old.update(0,4))<=1 for v in sampler.hot)>=29,
            'Hot EWMA does not match measured accesses')
    require(all(watched[v]['heat']==0 for v in sampler.cold), 'Cold EWMA not zero')
    trace = Trace('core-warmup')
    work.send('out'); work.send('in')
    rows = validate_trace('core-warmup', trace.finish(), a1['binding_id'])
    require(all(d['state']==1 and d['preferred_tier']==1 for d in rows), 'Warmup did not prefer PMem')
    for window in range(2,6):
        audit = sampler.wait_window(window)
        status = fields((RS/'placement_raw').read_text())
        require(status['state'] == ('adaptive' if window==5 else 'warmup'), 'Phase/commit mismatch')
        state(f'CORE EWMA {window}: phase={status["state"]} unknown=0')
    work.send('dirty')  # clean swap copies otherwise bypass a new Store
    trace = Trace('core-adaptive')
    work.send('out'); work.send('outcold'); work.send('in')
    rows = validate_trace('core-adaptive', trace.finish(), audit['binding_id'])
    require(any(d['reason']==5 for d in rows) and any(d['reason']==6 for d in rows),
            'Real sampler did not produce both hot and cold Store decisions')
    sampler.stop(); work.finish(); settle(); snapshot('core-finished')
    result('core.results.json',dict(real_sampler=True,default_parameters=True,unknown=0,
                                   warmup_decisions=True,hot_and_cold=True,contents=True))


def edges():
    state('EDGES: controlled snapshots for equality/zero, non-target Interleave, fixed priority and stop')
    group = previous.CG_PARENT/'hermit-p14-fixed'
    require(not group.exists(), 'Fixed-policy test cgroup already exists')
    group.mkdir(); TEST_GROUPS.append(group)
    (group/'memory.memsw.limit_in_bytes').write_text('-1\n')
    (group/'memory.limit_in_bytes').write_text('256M\n')
    (group/'memory.swappiness').write_text('100\n')
    work = Pattern('edges-target',4,group)
    policy = Policy(work.job.pid)
    for window in range(6):
        policy.publish(window,[(work.base+i,SCALE//2,1) for i in range(work.pages)])
    trace = Trace('edge-equality'); work.send('out all'); work.send('verify')
    rows = validate_trace('edge-equality',trace.finish(),policy.binding)
    require(all(d['reason']==5 and d['heat_q32']*d['pages']==d['sum_heat'] for d in rows), 'Equality was not hot')
    policy.publish(6,[(work.base+i,0,1) for i in range(work.pages)])
    trace = Trace('edge-zero'); work.send('out all'); work.send('verify')
    rows = validate_trace('edge-zero',trace.finish(),policy.binding)
    require(all(d['reason']==5 and d['heat_q32']==d['sum_heat']==0 for d in rows), 'All-zero was not hot')
    other = Pattern('non-target',4)
    trace = Trace('non-target'); other.send('out all'); other.send('verify')
    rows = validate_trace('non-target',trace.finish())
    require(all(d.get('aliases',0)==0 and d['state']==0 for d in rows), 'Non-target consumed target heat')
    other.finish()
    for name,tier in [('purePMem',1),('pureDRAM',0)]:
        fd = os.open(RS/'app_policy',os.O_RDWR|os.O_CLOEXEC); OPEN_FDS.add(fd)
        binding = register(fd,work.job.pid,name)
        trace = Trace('fixed-'+name); work.send('out all'); work.send('verify')
        text = trace.finish()
        rows = [fields(line.split('app_decision:',1)[1]) for line in text.splitlines() if 'app_decision:' in line]
        rows = [d for d in rows if int(d['binding'])==binding]
        require(rows and all(int(d['preferred_tier'])==tier and int(d['tier'])==tier and int(d['alloc_ret'])==0 for d in rows),
                'Fixed policy failed to override VPN heat: '+name)
        os.close(fd); OPEN_FDS.remove(fd)
    (RS/'placement_control').write_text('stop\n')
    trace = Trace('stopped'); work.send('out all'); work.send('verify')
    rows = validate_trace('stopped',trace.finish())
    require(all(d['state']==0 for d in rows), 'Stopped policy still affects new Stores')
    policy.close(); work.finish(); settle(); snapshot('edges-finished')
    result('edges.results.json',dict(equality_hot=True,zero_hot=True,fixed_priority=True,
                                    non_target_interleave=True,stop=True,contents=True))


def release_capacity_slot(work, rows, preferred):
    chosen=next((d for d in rows if d['tier']==0 and d['server']==(preferred^1)
                 and work.base<=d['vpn']<work.base+work.pages),None)
    require(chosen is not None,'No target Slot for same-tier capacity check')
    index=chosen['vpn']-work.base
    release=[d['vpn']-work.base for d in rows if d['tier']==1 and work.base<=d['vpn']<work.base+work.pages][:64]
    require(len(set(release))==64,'Insufficient distinct PMem pages to drain free-slot cache')
    os.sched_setaffinity(work.job.pid,{min(os.sched_getaffinity(work.job.pid))})
    work.send(f'drop {index}')
    # SWAP_BATCH=64 on this #9 kernel. The 65th free drains the first batch,
    # including the selected DRAM entry; merely waiting cannot flush it.
    for page in release: work.send(f'drop {page}')
    free=parse_bundle(collect(RS))['pools']
    require(free[preferred,'dram']['free']==0 and free[preferred^1,'dram']['free']==1,
            'Expected one released DRAM Slot on the alternate Server')
    return index,release


def capacity():
    state('CAPACITY: 40 GiB pageout in existing 96 GiB pool; fill 32 GiB DRAM then PMem')
    work = Pattern('capacity',40960)
    policy = Policy(work.job.pid)
    for w in range(6): policy.publish(w)  # missing target history=0, empty mean=0
    before = parse_bundle(collect(RS))['policy']['counters']
    trace = Trace('capacity-bulk',sampled=True)
    work.send('out all')
    text = trace.finish()
    rows = list(read_decisions(text.splitlines(),LAYOUT).values())
    rows = [d for d in rows if d.get('binding')==policy.binding]
    require(rows and all(d['reason']==5 for d in rows), 'Capacity workload was not logically hot')
    require(any(d['fallback']>=2 and d['tier']==1 for d in rows), 'No sampled cross-tier fallback')
    parsed = snapshot('capacity-full')
    for s in (0,1):
        p = parsed['pools'][s,'dram']
        require(p['free']==0 and p['used']==p['capacity'], f'DRAM {s} not full')
        require(parsed['pools'][s,'pmem']['used']>0, f'No fallback to PMem {s}')
    # Every target Store is hot, sharing one DRAM RR; free a Slot on the
    # nonpreferred Server to exercise same-tier fallback deterministically.
    after = parsed['policy']['counters']
    tickets = after[('reason',5)][0]-before[('reason',5)][0]
    preferred = tickets%2
    trace = Trace('capacity-same-tier')
    index,release=release_capacity_slot(work,rows,preferred)
    work.send(f'refault {index}'); work.send(f'out {index}')
    rows = validate_trace('capacity-same-tier',trace.finish(),policy.binding)
    require(any(d['fallback']==1 and d['tier']==0 for d in rows), 'No same-tier alternate Server fallback')
    for page in release: work.send(f'refault {page}')
    (RS/'placement_control').write_text('stop\n')
    work.send('verify')  # existing DRAM and PMem copies remain readable after stop
    policy.close(); work.finish(); settle(); snapshot('capacity-finished')
    result('capacity.results.json',dict(pool_gib=96,workload_gib=40,tier_fallback=True,
                                       same_tier_fallback=True,contents=True,loads_after_stop=True,
                                       full_pool_disk_fallback='local tests only; pool exceeds active swap size'))


def process_command():
    path = RUN/'bfs.command'
    if not path.exists(): return None
    action = path.read_text().strip()
    require(action in ('stop','continue'), 'bfs.command must be stop or continue')
    path.unlink()
    with (RUN/'bfs.commands.jsonl').open('a') as log:
        log.write(json.dumps(dict(action=action,mono_ns=time.monotonic_ns()))+'\n')
    return action


def pin_owned(pid, token, executable):
    fd = os.pidfd_open(pid)
    try:
        require(old.process_identity(pid)[1]==token, 'Owned PID changed; no signal sent')
        require(Path(f'/proc/{pid}/exe').resolve(strict=True)==executable.resolve(), 'Owned executable changed')
        return fd
    except FileNotFoundError as error:
        os.close(fd)
        raise ProcessLookupError(errno.ESRCH,'Owned process exited during identity check') from error
    except BaseException:
        os.close(fd)
        raise


def signal_owned(pid, token, executable, sig):
    fd=pin_owned(pid,token,executable)
    try:
        signal.pidfd_send_signal(fd,sig)
    finally:
        os.close(fd)


def signal_target(pid, token, sig):
    signal_owned(pid,token,previous.BFS,sig)


def wait_bfs_review(pid, token, sampler, sampler_token, report, reason):
    """Freeze only the verified BFS and sampler; no extra EWMA during review."""
    if old.target_finished(pid): return 'finished'
    handles=[]
    try:
        try:
            bfs_fd=pin_owned(pid,token,previous.BFS);handles.append(bfs_fd)
            sampler_fd=pin_owned(sampler.job.pid,sampler_token,RUN/'sampler-audit');handles.append(sampler_fd)
        except ProcessLookupError:
            require(old.target_finished(pid),'Owned process vanished before review')
            return 'finished'
        return review_pinned(pid,sampler,report,reason,bfs_fd,sampler_fd)
    finally:
        for fd in handles: os.close(fd)


def send_pinned(fd,sig):
    try:
        signal.pidfd_send_signal(fd,sig)
        return True
    except ProcessLookupError:
        return False  # A dead pidfd is never redirected to a reused PID.


def review_pinned(pid,sampler,report,reason,bfs_fd,sampler_fd):
    if not send_pinned(sampler_fd,signal.SIGSTOP):
        require(old.target_finished(pid),'BFS sampler exited before pause')
        return 'finished'
    if not send_pinned(bfs_fd,signal.SIGSTOP):
        send_pinned(sampler_fd,signal.SIGCONT)
        return 'finished'
    deadline=time.monotonic()+15
    while old.child_state(pid) not in ('T','t') or old.child_state(sampler.job.pid) not in ('T','t'):
        if old.target_finished(pid):
            send_pinned(sampler_fd,signal.SIGCONT)
            return 'finished'
        require(time.monotonic()<deadline,'Owned BFS/sampler did not pause; inspect process states')
        time.sleep(.05)
    paused_ns=time.monotonic_ns()
    current=snapshot('bfs-review')
    review=dict(reason=reason,pid=pid,paused=True,completed_windows=len(report.rows)-1,
                adaptive_ewma=max(0,len(report.rows)-1-WARMUP_GROUPS),
                latest_ewma=report.rows[-1] if report.rows else None,
                pool_at_pause=occupancy(current),unknown=0,
                records='bfs.ewma.tsv and bfs.ewma.jsonl')
    result('bfs.review.json',review)
    state('BFS PAUSED FOR REVIEW: '+json.dumps(review)+'; user must choose stop/continue')
    while True:
        action=process_command()
        if action: break
        if old.target_finished(pid):
            send_pinned(sampler_fd,signal.SIGCONT)
            return 'finished'
        old.check_kernel()
        time.sleep(1)
    if action=='stop': send_pinned(bfs_fd,signal.SIGTERM)
    send_pinned(bfs_fd,signal.SIGCONT)
    # On stop, let the sampler observe the target exit and close normally.
    send_pinned(sampler_fd,signal.SIGCONT)
    with (RUN/'bfs.pauses.jsonl').open('a') as log:
        log.write(json.dumps(dict(reason=reason,action=action,begin_ns=paused_ns,end_ns=time.monotonic_ns()))+'\n')
    return action


def bfs():
    state('BFS: original 6 GiB script, 10s/2-round sampler; pause after 5 Warmup + 10 Adaptive EWMA groups')
    before = snapshot('bfs-before')
    before_io = previous.parse_io((RS/'stats').read_text())
    trace = Trace('bfs-sampled',sampled=True)
    job = old.spawn(['bash',str(previous.BFS_SCRIPT)],'bfs')
    started_ns=time.monotonic_ns(); started=started_ns/1e9; pid=None
    report=EwmaReport(RUN,before,started_ns)
    while pid is None:
        require(job.poll() is None, 'BFS script exited before exec')
        if (previous.BFS_CG/'cgroup.procs').exists():
            for value in (previous.BFS_CG/'cgroup.procs').read_text().split():
                candidate=int(value)
                try:
                    if Path(f'/proc/{candidate}/exe').resolve()==previous.BFS.resolve(): pid=candidate; break
                except FileNotFoundError: pass
        require(time.monotonic()-started<60,'BFS exec timeout')
        if pid is None: time.sleep(.02)
    argv = [v.decode() for v in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if v]
    require(argv==[str(previous.BFS),'-rounds','3',str(previous.GRAPH)],'BFS argv changed')
    ancestor,token=old.process_identity(pid)
    seen=set()
    while ancestor!=job.pid and ancestor>1 and ancestor not in seen:
        seen.add(ancestor); ancestor=old.process_identity(ancestor)[0]
    require(ancestor==job.pid,'BFS not owned by this controller')
    old.OWNED_TARGETS.append((pid,token))
    groups={n:(previous.BFS_CG/n).read_text().strip() for n in previous.GROUP_SETTINGS}
    require(int(groups['memory.limit_in_bytes'])==6*1024**3 and groups['memory.swappiness']=='100'
            and int(groups['memory.memsw.limit_in_bytes'])>=1<<60,'BFS cgroup changed')
    result('bfs.target.json',dict(pid=pid,start_token=token,argv=argv,group=groups))
    sampler=Sampler(pid,'bfs-sampler',window=BFS_EWMA_WINDOW,interval=BFS_INTERVAL)
    sampler_token=old.process_identity(sampler.job.pid)[1]
    stopped=False; peak=0; reviews=0; next_progress=0; last_live=None
    checkpoint=WARMUP_GROUPS+ADAPTIVE_REVIEW_GROUPS
    last_advance=time.monotonic()
    base_counts=before['policy']['counters']
    def update_ewma():
        audits=sampler.records('AUDIT')
        if len(audits)<=len(report.rows): return False
        # Observe after the newest audit; preserve earlier matching samples
        # from the normal 1s collection history rather than relabel new data.
        report.observe(collect(RS))
        for audit in audits[len(report.rows):]:
            row=report.append(audit)
            usage=(f'DRAM={row["dram_gib"]:.2f}GiB PMem={row["pmem_gib"]:.2f}GiB '
                   f'ratio={row["ratio"]}') if row['occupancy_observed'] else 'capacity=unavailable'
            state(f'BFS EWMA {row["window"]}: phase={row["phase"]} adaptive_group={row["adaptive_ewma"]} '
                  f'mean={row["mean"]:.6f} hot={100*row["hot_fraction"]:.1f}% '
                  f'cold={100*row["cold_fraction"]:.1f}% bins={row["bins"]} {usage} unknown=0')
        return True
    with (RUN/'bfs.pool-samples.jsonl').open('w') as output:
        while job.poll() is None:
            now=time.monotonic()
            raw=collect(RS); parsed=report.observe(raw)
            output.write(json.dumps(raw)+'\n'); output.flush()
            peak=max(peak,sum(p['used'] for p in parsed['pools'].values())*4096)
            if not old.target_finished(pid):
                last_live=dict(mono_ns=raw['end_ns'],**occupancy(parsed))
            for r in (2,3,4): require(parsed['policy']['counters'][('reason',r)][0]==0,'BFS unknown decision')
            actual=sum(parsed['policy']['counters'][('reason',r)][0]-base_counts[('reason',r)][0] for r in (5,6))
            old.check_kernel()
            if sampler.out.exists(): os.chmod(sampler.out,0o755)
            if update_ewma(): last_advance=now
            if sampler.job.poll() is not None:
                require(sampler.job.returncode==0 and old.target_finished(pid),'BFS sampler exited unexpectedly')
                break
            require(old.child_state(sampler.job.pid) not in ('T','t') and
                    'AUDIT_ERROR' not in sampler.stdout.read_text(),'BFS audit failed/stopped')
            action=process_command()
            if action=='stop':
                try:
                    signal_target(pid,token,signal.SIGTERM); stopped=True
                except ProcessLookupError:
                    require(old.target_finished(pid),'BFS vanished before stop request')
                state('BFS STOP REQUESTED: terminating only the verified BFS target; preserving test evidence')
                break
            if report.ready_to_review(checkpoint) or now-last_advance>=900:
                reason='adaptive_10_ewma' if report.ready_to_review(checkpoint) else 'no_completed_ewma_for_15_minutes'
                action=wait_bfs_review(pid,token,sampler,sampler_token,report,reason)
                if action=='stop': stopped=True; break
                if action=='finished': break
                reviews+=1
                checkpoint=(report.rows[-1]['window'] if report.rows else 0)+ADAPTIVE_REVIEW_GROUPS
                last_advance=time.monotonic()
            if now>=next_progress:
                state(f'BFS RUNNING {now-started:.1f}s phase={parsed["state"]["state"]} '
                      f'window={parsed["state"].get("completed_windows","0")} '
                      f'adaptive_stores={actual} remote_peak={peak/1024**3:.2f}GiB unknown=0')
                next_progress=now+30
            time.sleep(1)
    code=job.wait(timeout=120)
    require(stopped or code==0,'BFS script failed')
    require(sampler.job.wait(timeout=120)==0,'BFS sampler failed on target exit')
    audits=sampler.finished()
    update_ewma()
    text=trace.finish()
    summary=Counter()
    decisions=read_decisions(text.splitlines(),LAYOUT,summary)
    binding=audits[0]['binding_id'] if audits else None
    samples=[d for d in decisions.values() if d.get('binding')==binding]
    warmup_sampled=any(d['state']==1 for d in samples)
    adaptive_sampled=any(d['state']==2 for d in samples)
    by_window={a['window']:a for a in audits}
    checked=0
    for d in samples:
        if not d['publish_ns'] or d['window'] not in by_window: continue
        a=by_window[d['window']]
        require((d['pages'],d['sum_heat'])==(a['pages'],a['sum_heat']),
                'BFS Store inputs differ from committed/audited snapshot')
        checked+=1
    require(checked>0,'No sampled BFS Store matched an audited publication')
    result('bfs-sampled.decisions.json',dict(summary=dict(summary),matched=len(samples),
                                            publication_checked=checked,scope='sampled Slots; not full allocator replay'))
    timed=len(re.findall(r'Running time',(RUN/'bfs.console.log').read_text()))
    require(not (previous.BFS_CG/'cgroup.procs').read_text().strip(),'BFS tasks remain')
    after=snapshot('bfs-finished'); settle()
    actual=sum(after['policy']['counters'][('reason',r)][0]-base_counts[('reason',r)][0] for r in (5,6))
    adaptive_covered=len(audits)>=6 and actual>0 and warmup_sampled and adaptive_sampled
    require(stopped or timed==3,'BFS did not finish three timed rounds')
    io=previous.parse_io((RS/'stats').read_text())
    delta={k:io[k]-before_io[k] for k in io}
    for s in ('0','1'):
        require(sum(v for (server,tier,op),v in delta.items() if server==s and op=='write')>0,
                'No BFS writes on Server '+s)
        require(sum(v for (server,tier,op),v in delta.items() if server==s and op in ('read_sync','read_async'))>0,
                'No BFS reads on Server '+s)
    result('bfs.results.json',dict(stopped_by_user=stopped,normal_complete=not stopped,timed_rounds=timed,
                                  elapsed_seconds=time.monotonic()-started,ewma_updates=len(audits)-1,
                                  adaptive_store_decisions=actual,completed_adaptive_ewma=max(0,len(audits)-1-WARMUP_GROUPS),
                                  a_bit_window=BFS_INTERVAL,ewma_window=BFS_EWMA_WINDOW,warmup_groups=WARMUP_GROUPS,
                                  review_count=reviews+int(stopped),last_live_occupancy=last_live,
                                  adaptive_coverage=adaptive_covered,warmup_sampled=warmup_sampled,
                                  adaptive_sampled=adaptive_sampled,
                                  per_ewma_records='bfs.ewma.tsv',unknown=0,
                                  peak_remote_bytes=peak,io_delta=[dict(server=s,tier=t,op=o,pages=n) for (s,t,o),n in delta.items()],
                                  scope='Functional acceptance; BFS provides no algorithm checksum; no performance A/B'))
    state('BFS '+('USER-STOPPED: real-load coverage retained' if stopped else
                 'PASS: normal exit and three timed rounds' if adaptive_covered else
                 'FINISHED: normal exit; Adaptive coverage insufficient, no automatic rerun'))



def prepare():
    require(not RUN.exists(),'Preparation directory must be new')
    RUN.mkdir(mode=0o755)
    for source,output,flags in [('stage2_pattern.c','pattern',['-pthread']),
                                ('stage2_sampler_audit.c','sampler-audit',[]),
                                ('stage3_capacity_pattern.c','capacity-pattern',[])]:
        command(['gcc','-O2','-g','-std=gnu11','-Wall','-Wextra','-Werror',
                 str(REPO/'remoteswap/tests'/source),*flags,'-o',str(RUN/output)],output+'.build.log')
    command(['make','-C',str(REPO/'remoteswap/tools')],'tools.build.log')
    command(['make','-C',str(REPO/'remoteswap/client'),'KDIR='+str(REPO/'linux-5.14-rc5'),'-j12'],'client.build.log')
    paths=[Path(__file__),previous.BFS_SCRIPT,previous.BFS,REPO/'remoteswap/client/rswap-client.ko',
           REPO/'remoteswap/client/rswap_client.conf',REPO/'remoteswap/client/manage_rswap_client.sh']
    paths += [RUN/name for name in ('pattern','sampler-audit','capacity-pattern')]
    paths += [REPO/'remoteswap/tests'/name for name in ('stage2_pattern.c','stage2_sampler_audit.c','stage3_bfs_report.py',
              'stage3_capacity_pattern.c','stage2_host.py','stage1_core_host.py','stage1_extended_host.py')]
    paths += list((REPO/'remoteswap/tools').glob('rswap_hotness.[ch]'))
    paths += [REPO/'remoteswap/tools'/name for name in ('rswap_pool.py','rswap_trace.py','rswap_analysis.py',
              'rswap_hotness_check.py','rswap_run.py','rswap_stats.py','rswap_threshold_check.py')]
    result('artifacts.json',{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
    state('PREPARED: Client 1.19; both Servers need 16GiB DRAM + 32GiB PMem on port 9400')


def execute():
    global START
    START=time.monotonic()
    require(os.geteuid()==0,'Operator must run this controller with sudo on Optane')
    require(not (RUN/'started').exists(),'Already started; do not retry this directory')
    require(os.uname().version=='#9 SMP Sat Nov 28 00:02:37 CST 2026','Expected kernel #9')
    require(Path('/sys/devices/system/cpu/online').read_text().strip()=='0-47','Expected 48 Client CPUs')
    require(not Path('/sys/module/rswap_client').exists(),'Client already loaded')
    swaps=core.swaps()
    require(not swaps or len(swaps)==1 and swaps[0][0]=='/swap.img' and int(swaps[0][3])==0,
            'Expected no swap, or only unused original /swap.img')
    require(not TRACE.exists(),'Private trace instance already exists')
    require(not (previous.CG_PARENT/'hermit-p14-fixed').exists(),'Fixed-policy test cgroup already exists')
    require(previous.CG_PARENT.is_dir(),'Memory cgroup v1 required')
    require(previous.GRAPH.stat().st_size==11783351220 and Path(previous.BIG_SWAP).stat().st_size==64*1024**3,
            'Expected existing graph and 64 GiB swap file')
    for path,digest in json.loads((RUN/'artifacts.json').read_text()).items():
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest,'Prepared artifact changed: '+path)
    config=(REPO/'remoteswap/client/rswap_client.conf').read_text()
    for line in ('NR_SERVERS=2','POOL_GIB=96','SERVER0_IP="10.0.0.13"','SERVER1_IP="10.0.0.20"'):
        require(line in config.splitlines(),'Unexpected Client configuration')
    group_before=None
    if previous.BFS_CG.exists():
        require(not (previous.BFS_CG/'cgroup.procs').read_text().strip(),'BFS cgroup already occupied')
        group_before={n:(previous.BFS_CG/n).read_text().strip() for n in previous.GROUP_SETTINGS}
    require(str(previous.BFS_CG) not in Path('/proc/self/cgroup').read_text(),'Controller must be outside BFS cgroup')
    settings={str(HMT/n):previous.setting(str(HMT/n)) for n in core.FLAGS}
    settings.update({p:previous.setting(p) for p in ('/proc/sys/vm/page-cluster','/sys/kernel/mm/swap/readahead_win',
                                                   '/sys/kernel/mm/transparent_hugepage/enabled')})
    result('settings.before.json',settings); result('bfs.cgroup.before.json',group_before)
    result('swaps.before.json',[dict(path=r[0],priority=int(r[4])) for r in swaps])
    command(['dmesg'],'dmesg.before'); command(['swaplabel',previous.BIG_SWAP],'large-swap.label')
    result('controller.json',dict(pid=os.getpid(),start_token=old.process_identity(os.getpid())[1]))
    save('started',time.strftime('%Y-%m-%d %H:%M:%S %z')+'\n')
    for name in core.FLAGS: (HMT/name).write_text('N\n')
    Path('/proc/sys/vm/page-cluster').write_text('0\n')
    Path('/sys/kernel/mm/swap/readahead_win').write_text('1\n')
    Path('/sys/kernel/mm/transparent_hugepage/enabled').write_text('never\n')
    if swaps: command(['swapoff','/swap.img'],'original-swapoff.log')
    command(['bash',str(REPO/'remoteswap/client/manage_rswap_client.sh'),'install'],'client-install.log')
    require(Path('/sys/module/rswap_client/version').read_text().strip()=='1.19','Expected Client 1.19')
    empty(); command(['swapon',previous.BIG_SWAP],'test-swapon.log')
    snapshot('baseline')
    TRACE.mkdir()
    (TRACE/'buffer_size_kb').write_text('16384\n')
    (TRACE/'trace_clock').write_text('global\n')
    check_trace_schema()
    run_tests(settings,group_before,swaps)


def fresh_outputs(directory, completed=()):
    for name in ('controlled.heat','bfs-sampler.heat','bfs.ewma.tsv','bfs.ewma.jsonl',
                 'core.results.json','edges.results.json','capacity.results.json','bfs.results.json'):
        if name in {p+'.results.json' for p in completed}: continue
        require(not (directory/name).exists(),'Test output already exists; use a fresh directory: '+str(directory/name))


def run_tests(settings,group_before,swaps,completed=()):
    fresh_outputs(RUN,completed)
    if 'core' not in completed:
        controlled(); reset_test_swap('after-core')
    if 'edges' not in completed:
        edges(); reset_test_swap('after-edges')
    capacity(); reset_test_swap('before-bfs')
    bfs()
    settle(); snapshot('before-cleanup')
    require(not OPEN_FDS,'Control fds still open')
    command(['swapoff',previous.BIG_SWAP],'test-swapoff.log',timeout=300)
    empty(); snapshot('after-swapoff')
    (TRACE/'events/enable').write_text('0\n'); TRACE.rmdir()
    command(['bash',str(REPO/'remoteswap/client/manage_rswap_client.sh'),'uninstall'],'client-uninstall.log')
    for path,value in settings.items():
        Path(path).write_text(value+'\n'); require(previous.setting(path)==value,'Setting not restored: '+path)
    for group in TEST_GROUPS:
        require(not (group/'cgroup.procs').read_text().strip(),'Owned test cgroup not empty'); group.rmdir()
    require(not (previous.BFS_CG/'cgroup.procs').read_text().strip(),'BFS cgroup not empty')
    if group_before is None: previous.BFS_CG.rmdir()
    else:
        (previous.BFS_CG/'memory.memsw.limit_in_bytes').write_text('-1\n')
        for name in ('memory.limit_in_bytes','memory.memsw.limit_in_bytes','memory.swappiness'):
            (previous.BFS_CG/name).write_text(group_before[name]+'\n')
            require((previous.BFS_CG/name).read_text().strip()==group_before[name],'BFS cgroup restoration failed')
    for row in swaps:
        args=['swapon']
        if int(row[4])>=0: args+=['--priority',row[4]]
        command(args+[row[0]],'original-swap-restored.log')
    require([r[0] for r in core.swaps()]==[r[0] for r in swaps],'Original swap state not restored')
    command(['dmesg'],'dmesg.final'); old.check_kernel()
    bfs_result=json.loads((RUN/'bfs.results.json').read_text())
    result('results.json',dict(client='1.19',seconds=time.monotonic()-START,unknown=0,cleanup='restored',bfs=bfs_result))
    if bfs_result['adaptive_coverage']:
        save('SUCCESS','Stage 3 core, boundary, fixed priority, capacity, real BFS coverage and restoration passed\n')
        state('COMPLETE: test environment restored; Servers may exit after Client disconnect')
    else:
        save('PARTIAL','Core/boundary/capacity passed; BFS ended before full Adaptive coverage; environment restored\n')
        state('COMPLETE WITH PARTIAL BFS COVERAGE: environment restored; report actual groups and occupancy')


def process_record(pid):
    try:
        proc=Path('/proc')/str(pid)
        text=(proc/'stat').read_text()
        values=text[text.rfind(')')+2:].split()
        argv=[v.decode() for v in (proc/'cmdline').read_bytes().split(b'\0') if v]
        return dict(pid=pid,ppid=int(values[1]),start_token=int(values[19]),state=values[0],argv=argv)
    except (FileNotFoundError,ProcessLookupError):
        return None


def recovery_process_plan(owner, records):
    """Only this exact failed controller and its two expected direct children."""
    controller=records.get(owner['pid'])
    require(controller is not None and controller['start_token']==owner['start_token'],
            'Failed controller exited or PID was reused; no signals sent')
    argv=controller['argv']
    require(len(argv)==3 and Path(argv[0]).name in ('python3','python3.12') and
            argv[1:]==[str(Path(__file__).resolve()),str(RUN)],'Unexpected failed controller command')
    expected={str(RUN/'pattern'),str(RUN/'sampler-audit')}
    children=[r for r in records.values() if r['ppid']==controller['pid'] and r['state'] not in ('Z','X')]
    require(len(children)==2 and {r['argv'][0] for r in children if r['argv']}==expected,
            'Unexpected failed-controller children; no signals sent')
    for child in children:
        if child['argv'][0]==str(RUN/'pattern'):
            require(child['argv']==[str(RUN/'pattern')],'Unexpected pattern arguments')
            require(child['pid']==json.loads((RUN/'controlled.target.json').read_text())['pid'],
                    'Controlled target PID changed')
        else:
            require(child['argv']==[str(RUN/'sampler-audit'),'--placement','--a-bit-window','5',
                    '--ewma-window','4','--out',str(RUN/'controlled.heat'),'--pid',
                    str(json.loads((RUN/'controlled.target.json').read_text())['pid'])],
                    'Unexpected sampler arguments')
        require(not any(r['ppid']==child['pid'] and r['state'] not in ('Z','X') for r in records.values()),
                'Unexpected grandchildren; no signals sent')
    return [controller]+children


def recover_core_store(slot_cache=False):
    """User-invoked recovery for this one diagnosed test coverage failure."""
    global START
    START=time.monotonic()
    require(os.geteuid()==0,'Operator must invoke recovery with sudo on Optane')
    marker='slot-cache-recovery.started' if slot_cache else 'recovery.started'
    manifest='slot-cache-artifacts.json' if slot_cache else 'recovery-artifacts.json'
    require(not (RUN/marker).exists(),'Recovery already attempted; inspect before any further action')
    if slot_cache:
        require('RuntimeError: Mappings remain after owned workload exit' in
                (RUN/'recovery.failure.txt').read_text(),'Unexpected second failure; do not reset swap')
    require('RuntimeError: Real sampler did not produce both hot and cold Store decisions' in
            (RUN/'failure.txt').read_text(),'Unexpected failure; this recovery does not apply')
    require(not (RUN/'bfs.target.json').exists() and not (RUN/'core.results.json').exists(),
            'Failure was not the identified core coverage check')
    for path,digest in json.loads((RUN/manifest).read_text()).items():
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest,'Recovery artifact changed: '+path)
    original=json.loads((RUN/'artifacts.json').read_text())
    for name in ('remoteswap/client/rswap-client.ko','remoteswap/client/rswap_client.conf'):
        path=REPO/name
        require(hashlib.sha256(path.read_bytes()).hexdigest()==original[str(path)],'Client module/config changed')
    require(Path('/sys/module/rswap_client/version').read_text().strip()=='1.19','Expected retained Client 1.19')
    live=fields((RS/'stats').read_text().splitlines()[0])
    baseline=fields((RUN/'baseline.stats').read_text().splitlines()[0])
    require(live['instance_ns']==baseline['instance_ns'],'Client instance changed; no recovery actions taken')
    require([r[0] for r in core.swaps()]==[previous.BIG_SWAP],'Unexpected active swap areas')
    require(TRACE.is_dir() and (TRACE/'tracing_on').read_text().strip()=='0','Unexpected private trace state')
    settings=json.loads((RUN/'settings.before.json').read_text())
    for path,value in settings.items():
        require(previous.setting(path)==value,'Retained setting changed: '+path)
    require(not (previous.BFS_CG/'cgroup.procs').read_text().strip(),'Unexpected BFS tasks')
    records={}
    for path in Path('/proc').iterdir():
        if path.name.isdecimal():
            record=process_record(int(path.name))
            if record: records[record['pid']]=record
    if slot_cache:
        expected=[str(Path(__file__).resolve()),str(RUN),'--recover-core-store']
        owners=[r for r in records.values() if len(r['argv'])==4 and
                Path(r['argv'][0]).name in ('python3','python3.12') and r['argv'][1:]==expected and r['state'] not in ('Z','X')]
        require(len(owners)==1,'Cannot uniquely identify failed recovery controller')
        require(not any(r['ppid']==owners[0]['pid'] and r['state'] not in ('Z','X') for r in records.values()),
                'Failed recovery has live children; no signals sent')
        require(fields((RS/'identity_status').read_text())['bound']=='0','Old identity still bound')
        plan=owners
    else:
        plan=recovery_process_plan(json.loads((RUN/'controller.json').read_text()),records)
    handles={}
    try:
        for record in plan:
            fd=os.pidfd_open(record['pid']); handles[record['pid']]=fd
            actual=process_record(record['pid'])
            require(actual is not None and (actual['start_token'],actual['argv'])==
                    (record['start_token'],record['argv']),'Process identity changed; no signals sent')
        import shutil
        archive=RUN/('failed-slot-cache-check' if slot_cache else 'failed-core-clean-reuse')
        archive.mkdir()
        for path in RUN.iterdir():
            if path.is_file(): shutil.copyfile(path,archive/path.name)
            elif path.name=='controlled.heat': shutil.copytree(path,archive/path.name)
        result('slot-cache-recovery.plan.json' if slot_cache else 'recovery.plan.json',
               dict(processes=plan,retain_rdma=True,reset_test_swap=True,reason='stage isolation / per-CPU free-slot cache'))
        save(marker,time.strftime('%Y-%m-%d %H:%M:%S %z')+'\n')
        def send(pid, sig):
            try: signal.pidfd_send_signal(handles[pid],sig)
            except ProcessLookupError: pass
        owner=plan[0]
        send(owner['pid'],signal.SIGSTOP)
        for record in plan[1:]:
            send(record['pid'],signal.SIGTERM)
            send(record['pid'],signal.SIGCONT)
        deadline=time.monotonic()+60
        while any((r:=process_record(p['pid'])) is not None and r['state'] not in ('Z','X') for p in plan[1:]):
            require(time.monotonic()<deadline,'Owned helper did not exit; preserve environment')
            time.sleep(.1)
        send(owner['pid'],signal.SIGTERM)
        send(owner['pid'],signal.SIGCONT)
        deadline=time.monotonic()+10
        while (r:=process_record(owner['pid'])) is not None and r['state'] not in ('Z','X'):
            require(time.monotonic()<deadline,'Failed controller did not exit')
            time.sleep(.1)
    finally:
        for fd in handles.values(): os.close(fd)
    require(fields((RS/'identity_status').read_text())['bound']=='0','Old identity still bound')
    settle(); reset_test_swap('slot-cache-recovery' if slot_cache else 'recovery-start')
    for name in ('pattern','capacity-pattern','sampler-audit'):
        os.replace(RUN/'recovery-prepared'/name,RUN/name)
    result('controller.json',dict(pid=os.getpid(),start_token=old.process_identity(os.getpid())[1]))
    state('RECOVERY: identified failed controller/helpers stopped; both RDMA Sessions retained; test swap reset; core rerun uses dirty pages')
    swaps=[(r['path'],'','','',str(r['priority'])) for r in json.loads((RUN/'swaps.before.json').read_text())]
    run_tests(settings,json.loads((RUN/'bfs.cgroup.before.json').read_text()),swaps)


def resume_kind(previous_run):
    failed=previous_run/'failure.txt'
    if failed.exists():
        text=failed.read_text()
        if 'Invalid argument' in text and 'alloc_ret == 0' in text and 'capacity-bulk' in text:
            for name in ('core','edges'):
                outcome=json.loads((previous_run/(name+'.results.json')).read_text())
                require(outcome.get('contents') is True and outcome.get('unknown',0)==0 and
                        all(v is True or k=='unknown' and v==0 for k,v in outcome.items()),
                        'Prior '+name+' results incomplete; do not skip')
            require(not (previous_run/'capacity.results.json').exists() and not (previous_run/'bfs.target.json').exists(),
                    'Failure is outside the identified capacity trace setup')
            return ('core','edges')
    require('RuntimeError: controlled sampler exited: 1; inspect stderr' in
            (previous_run/'slot-cache-recovery.failure.txt').read_text(),'Unexpected prior failure')
    require('create output directory (must be new): File exists' in
            (previous_run/'controlled.stderr').read_text(),'Expected diagnosed output-directory collision')
    return ()


def resume_live(previous_run):
    """Resume the retained Client into a newly prepared output directory."""
    global START
    START=time.monotonic()
    require(os.geteuid()==0,'Operator must launch resume with sudo')
    require(RUN!=previous_run and not (RUN/'started').exists(),'Resume requires a fresh prepared directory')
    fresh_outputs(RUN)
    for path,digest in json.loads((RUN/'artifacts.json').read_text()).items():
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest,'Prepared artifact changed: '+path)
    completed=resume_kind(previous_run)
    require(Path('/sys/module/rswap_client/version').read_text().strip()=='1.19','Expected retained Client 1.19')
    baseline=fields((previous_run/'baseline.stats').read_text().splitlines()[0])
    live=fields((RS/'stats').read_text().splitlines()[0])
    require(live['instance_ns']==baseline['instance_ns'],'Retained Client instance changed')
    require([r[0] for r in core.swaps()]==[previous.BIG_SWAP],'Unexpected active swap areas')
    identity=fields((RS/'identity_status').read_text())
    require(TRACE.is_dir() and (TRACE/'tracing_on').read_text().strip()=='0','Unexpected private trace state')
    check_trace_schema()  # Kernel accepts the filter before any process signals or new load.
    original=json.loads((previous_run/'artifacts.json').read_text())
    for name in ('remoteswap/client/rswap-client.ko','remoteswap/client/rswap_client.conf'):
        path=REPO/name
        require(hashlib.sha256(path.read_bytes()).hexdigest()==original[str(path)],'Retained module/config changed')
    owner=json.loads((previous_run/'controller.json').read_text())
    record=process_record(owner['pid'])
    require(record is not None and record['start_token']==owner['start_token'],'Previous controller identity changed')
    expected=[str(Path(__file__).resolve()),str(previous_run),'--recover-slot-cache']
    if completed:
        parent=json.loads((previous_run/'resume.plan.json').read_text())['previous_run']
        expected=[str(Path(__file__).resolve()),str(previous_run),'--resume-live',parent]
    require(len(record['argv'])==len(expected)+1 and Path(record['argv'][0]).name in ('python3','python3.12') and
            record['argv'][1:]==expected,
            'Unexpected previous controller command')
    records=[]
    for path in Path('/proc').iterdir():
        if path.name.isdecimal():
            r=process_record(int(path.name))
            if r: records.append(r)
    children=[r for r in records if r['ppid']==owner['pid'] and r['state'] not in ('Z','X')]
    target=json.loads((previous_run/('capacity.target.json' if completed else 'controlled.target.json')).read_text())
    target_args=[str(previous_run/'capacity-pattern'),str(target['bytes']//1048576)] if completed else [str(previous_run/'pattern')]
    require(len(children)==1 and children[0]['pid']==target['pid'] and
            children[0]['argv']==target_args,'Unexpected prior-controller children')
    require(identity['bound']=='0' or completed and identity['bound']=='1' and int(identity['pid'])==target['pid'],
            'Unexpected active identity')
    require(not any(r['ppid']==children[0]['pid'] and r['state'] not in ('Z','X') for r in records),
            'Unexpected prior workload descendants')
    handles={}
    try:
        for r in [record]+children:
            fd=os.pidfd_open(r['pid']);handles[r['pid']]=fd
            actual=process_record(r['pid'])
            require(actual is not None and (actual['start_token'],actual['argv'])==(r['start_token'],r['argv']),
                    'Process identity changed; no signals sent')
        import shutil
        for name in ('settings.before.json','swaps.before.json','bfs.cgroup.before.json','dmesg.before','baseline.stats'):
            shutil.copyfile(previous_run/name,RUN/name)
        for name in completed: shutil.copyfile(previous_run/(name+'.results.json'),RUN/(name+'.results.json'))
        result('resume.plan.json',dict(previous_run=str(previous_run),processes=[record]+children,
                                       retain_rdma=True,new_output_directory=str(RUN),completed_stages=list(completed)))
        save('started',time.strftime('%Y-%m-%d %H:%M:%S %z')+'\n')
        def send(pid,sig):
            try: signal.pidfd_send_signal(handles[pid],sig)
            except ProcessLookupError: pass
        send(record['pid'],signal.SIGSTOP)
        send(children[0]['pid'],signal.SIGTERM);send(children[0]['pid'],signal.SIGCONT)
        state('RESUME: stopping only identified prior controller and '+('capacity' if completed else 'controlled')+' workload')
        deadline=time.monotonic()+120
        while (r:=process_record(children[0]['pid'])) is not None and r['state'] not in ('Z','X'):
            require(time.monotonic()<deadline,'Prior workload did not exit');time.sleep(.1)
        send(record['pid'],signal.SIGTERM);send(record['pid'],signal.SIGCONT)
        deadline=time.monotonic()+10
        while (r:=process_record(record['pid'])) is not None and r['state'] not in ('Z','X'):
            require(time.monotonic()<deadline,'Prior controller did not exit');time.sleep(.1)
    finally:
        for fd in handles.values(): os.close(fd)
    result('controller.json',dict(pid=os.getpid(),start_token=old.process_identity(os.getpid())[1]))
    settle();reset_test_swap('fresh-output-resume')
    settings=json.loads((RUN/'settings.before.json').read_text())
    if completed:
        group=previous.CG_PARENT/'hermit-p14-fixed'
        if group.exists():
            require(not (group/'cgroup.procs').read_text().strip(),'Prior fixed-policy cgroup still occupied')
            TEST_GROUPS.append(group)
    swaps=[(r['path'],'','','',str(r['priority'])) for r in json.loads((RUN/'swaps.before.json').read_text())]
    state('RESUMED: old controller/workload stopped; fresh outputs; existing RDMA pool retained')
    if completed:
        run_tests(settings,json.loads((RUN/'bfs.cgroup.before.json').read_text()),swaps,completed=completed)
    else:
        run_tests(settings,json.loads((RUN/'bfs.cgroup.before.json').read_text()),swaps)


def main():
    global RUN
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare',action='store_true')
    mode.add_argument('--recover-core-store',action='store_true')
    mode.add_argument('--recover-slot-cache',action='store_true')
    mode.add_argument('--resume-live',type=Path,metavar='PREVIOUS_RUN')
    args=parser.parse_args(); RUN=args.directory.resolve()
    old.RUN=core.RUN=RUN
    if args.prepare: prepare(); return
    require(RUN.is_dir(),'Run --prepare first')
    try:
        if args.resume_live: resume_live(args.resume_live.resolve())
        elif args.recover_slot_cache: recover_core_store(slot_cache=True)
        elif args.recover_core_store: recover_core_store()
        else: execute()
    except Exception:
        name='slot-cache-recovery.failure.txt' if args.recover_slot_cache else 'recovery.failure.txt' if args.recover_core_store else 'failure.txt'
        save(name,traceback.format_exc()); old.pause_owned_jobs()
        state('FAILED: owned jobs paused; retaining swap, RDMA, fds and logs for inspection; no automatic retry')
        while True: time.sleep(30)


if __name__=='__main__': main()

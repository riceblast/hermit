#!/usr/bin/env python3
"""Operator-only cleanup of the identified failed stage-2 run. No test/retry.

Requires sudo on the host, where /proc includes the original controller.
Validates the original module instance, process ancestry and start times, then
signals only that controller and its children through pidfds. Never formats,
removes a swap file, starts a workload, or writes a test SUCCESS marker.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import stage1_core_host as core
import stage1_extended_host as previous

CONTROLLER = core.REPO / 'remoteswap/tests/stage2_host.py'
RUN = None


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def write_json(name, value):
    (RUN / name).write_text(json.dumps(value, indent=2) + '\n')


def process_record(pid):
    try:
        p = Path('/proc') / str(pid)
        text = (p / 'stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        return dict(pid=pid, ppid=int(fields[1]), start=int(fields[19]), state=fields[0],
                    argv=[x.decode(errors='surrogateescape') for x in (p / 'cmdline').read_bytes().split(b'\0') if x])
    except (FileNotFoundError, ProcessLookupError):
        return None


def controller_match(record, run):
    args = record['argv']
    return (len(args) == 3 and Path(args[0]).name in ('python3', 'python3.12') and
            args[1:] == [str(CONTROLLER), str(run)])


def descendants(records, parent):
    selected = {parent}
    changed = True
    while changed:
        changed = False
        for pid, record in records.items():
            if record['ppid'] in selected and pid not in selected:
                selected.add(pid)
                changed = True
    return selected


def known_child(record, run, target):
    if record['state'] in ('Z', 'X'):
        return True
    args = record['argv']
    if not args:
        return False
    if args[0] == str(run / 'pattern'):
        return args == [str(run / 'pattern')]
    if args[0] == str(run / 'sampler-audit'):
        return '--pid' in args and '--publish' in args and '--out' in args
    if args == target['argv']:
        return record['pid'] == target['pid']
    if Path(args[0]).name == 'bash' and len(args) == 2:
        return args[1] == str(previous.BFS_SCRIPT)
    # The original script uses sudo bash -ec '<join cgroup; exec BFS>'.
    if Path(args[0]).name in ('sudo', 'bash'):
        joined = ' '.join(args)
        return ('-ec' in args and str(previous.BFS) in joined and
                str(previous.BFS_CG / 'tasks') in joined)
    return False


def run_anchor(record, run, target):
    """Identify an orphaned job by this run's executable or stdout log."""
    if not known_child(record, run, target) or record['state'] in ('Z', 'X'):
        return False
    if record['argv'][0] in (str(run / 'pattern'), str(run / 'sampler-audit')):
        return True
    try:
        output = os.readlink(f'/proc/{record["pid"]}/fd/1')
        return output in {str(run / 'bfs.console.log'), str(run / 'bfs-sampler.console.log')}
    except (FileNotFoundError, ProcessLookupError):
        return False


def expected_settings(settings):
    paths = {str(core.HMT / name) for name in core.FLAGS}
    paths.update(['/proc/sys/vm/page-cluster', '/sys/kernel/mm/swap/readahead_win',
                  '/sys/kernel/mm/transparent_hugepage/enabled'])
    require(set(settings) == paths, 'Unexpected restoration paths')
    for name in core.FLAGS:
        require(settings[str(core.HMT / name)] in ('Y', 'N'), 'Invalid Hermit setting')
    require(settings['/proc/sys/vm/page-cluster'].isdigit(), 'Invalid page-cluster setting')
    require(settings['/sys/kernel/mm/swap/readahead_win'].isdigit(), 'Invalid readahead setting')
    require(settings['/sys/kernel/mm/transparent_hugepage/enabled'] in ('always', 'madvise', 'never'),
            'Invalid THP setting')


def pin_process(record):
    fd = os.pidfd_open(record['pid'])
    actual = process_record(record['pid'])
    if not actual or actual['state'] in ('Z', 'X'):
        os.close(fd)
        return None
    if actual['start'] != record['start'] or actual['argv'] != record['argv']:
        os.close(fd)
        raise RuntimeError('Process identity changed before cleanup: ' + str(record['pid']))
    return fd


def send(fd, sig):
    try:
        signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass


def finished(record):
    actual = process_record(record['pid'])
    return not actual or actual['start'] != record['start'] or actual['state'] in ('Z', 'X')


def command(args, log, timeout=180):
    with (RUN / log).open('w') as stream:
        subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def execute():
    require(os.geteuid() == 0, 'Run cleanup with sudo on Optane')
    require(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'pidfd support required')
    require(not (RUN / 'cleanup.complete.json').exists(), 'Cleanup already completed')
    require((RUN / 'failure.txt').is_file() and not (RUN / 'SUCCESS').exists(), 'Expected failed test run')
    require('Stale file handle' in (RUN / 'bfs-sampler.stderr').read_text(), 'Unexpected failure type')
    require('BFS sampler exited' in (RUN / 'failure.txt').read_text(), 'Unexpected controller failure')
    require((RUN / 'controlled.checks.json').is_file(), 'Controlled test result missing')
    baseline = core.fields((RUN / 'baseline.stats').read_text().splitlines()[0])
    live = core.fields((core.RS / 'stats').read_text().splitlines()[0])
    require(live['instance_ns'] == baseline['instance_ns'], 'Client instance changed; stop for manual review')
    require(Path('/sys/module/rswap_client/version').read_text().strip() == '1.18', 'Client version changed')
    require([r[0] for r in core.swaps()] == [previous.BIG_SWAP], 'Unexpected active swap areas')
    settings = json.loads((RUN / 'settings.before.json').read_text())
    expected_settings(settings)
    group_before = json.loads((RUN / 'bfs.cgroup.before.json').read_text())
    require(group_before is None or set(group_before) == set(previous.GROUP_SETTINGS), 'Invalid cgroup restoration metadata')
    target = json.loads((RUN / 'bfs.target.json').read_text())
    require(target['argv'] == [str(previous.BFS), '-rounds', '3', str(previous.GRAPH)], 'Unexpected recorded BFS target')
    records = {}
    for path in Path('/proc').iterdir():
        if path.name.isdecimal():
            record = process_record(int(path.name))
            if record:
                records[record['pid']] = record
    owners = [r for r in records.values() if controller_match(r, RUN)]
    require(len(owners) <= 1, 'Multiple controllers found; do not guess which process to stop')
    owner = owners[0] if owners else None
    selected = descendants(records, owner['pid']) if owner else set()
    # The operator may already have closed the controller terminal. Unique
    # executable/log paths identify this run's orphaned jobs without PID guesses.
    for pid, record in records.items():
        if run_anchor(record, RUN, target):
            selected.update(descendants(records, pid))
    require(os.getpid() not in selected, 'Cleanup is a child of the failed controller')
    for pid in selected - ({owner['pid']} if owner else set()):
        require(known_child(records[pid], RUN, target), 'Unrecognized child; no signals sent: ' + str(records[pid]))
    if previous.BFS_CG.exists():
        tasks = {int(x) for x in (previous.BFS_CG / 'cgroup.procs').read_text().split()}
        require(tasks <= selected, 'BFS cgroup has processes outside this test controller')
    plan = [records[pid] for pid in sorted(selected)]
    write_json('cleanup.plan.json', dict(processes=plan, instance_ns=live['instance_ns'],
                                        test_rerun=False, restore_swap='/swap.img', keep_logs=True))
    handles = {}
    try:
        for record in plan:
            if record['state'] not in ('Z', 'X'):
                try:
                    fd = pin_process(record)
                except ProcessLookupError:
                    continue
                if fd is not None:
                    handles[record['pid']] = fd
        owner_fd = handles.get(owner['pid']) if owner else None
        require(not owner or owner_fd is not None, 'Controller exited before cleanup; rerun the read-only checks')
        # The failed controller only sleeps. Stop it first, then terminate the
        # already-finished/paused run and resume stopped children to deliver TERM.
        if owner_fd is not None:
            send(owner_fd, signal.SIGSTOP)
        for pid, fd in handles.items():
            if not owner or pid != owner['pid']:
                send(fd, signal.SIGTERM)
                send(fd, signal.SIGCONT)
        deadline = time.monotonic() + 30
        while any(not finished(r) for r in plan if not owner or r['pid'] != owner['pid']):
            require(time.monotonic() < deadline, 'A test child did not exit after TERM; inspect cleanup.plan.json')
            time.sleep(.1)
        if owner_fd is not None:
            send(owner_fd, signal.SIGTERM)
            send(owner_fd, signal.SIGCONT)
            deadline = time.monotonic() + 10
            while not finished(owner):
                require(time.monotonic() < deadline, 'Failed controller did not exit')
                time.sleep(.1)
    finally:
        for fd in handles.values():
            os.close(fd)
    require(not previous.BFS_CG.exists() or not (previous.BFS_CG / 'cgroup.procs').read_text().strip(),
            'BFS cgroup still contains tasks')
    command(['swapoff', previous.BIG_SWAP], 'cleanup.swapoff.log', timeout=300)
    require(not core.swaps(), 'Swap still active before disconnect')
    core.empty_mapping()
    identity = core.fields((core.RS / 'identity_status').read_text())
    require(identity['bound'] == '0' and identity['entries'] == '0', 'Identity did not reset')
    for name in ('stats', 'mapping_raw', 'identity_status', 'policy_stats'):
        (RUN / ('cleanup.' + name)).write_text((core.RS / name).read_text())
    command(['bash', str(core.REPO / 'remoteswap/client/manage_rswap_client.sh'), 'uninstall'],
            'cleanup.disconnect.log')
    require(not Path('/sys/module/rswap_client').exists(), 'Client still loaded')
    for path, value in settings.items():
        Path(path).write_text(value + '\n')
        require(previous.setting(path) == value, 'Setting restoration failed: ' + path)
    if previous.BFS_CG.exists():
        if group_before is None:
            previous.BFS_CG.rmdir()
        else:
            for name in ('memory.limit_in_bytes', 'memory.memsw.limit_in_bytes', 'memory.swappiness'):
                (previous.BFS_CG / name).write_text(group_before[name] + '\n')
                require((previous.BFS_CG / name).read_text().strip() == group_before[name], 'Cgroup setting not restored')
    command(['swapon', '/swap.img'], 'cleanup.restore-swap.log')
    require([r[0] for r in core.swaps()] == ['/swap.img'], 'Original swap not restored')
    command(['dmesg'], 'cleanup.dmesg.log')
    write_json('cleanup.complete.json', dict(test_rerun=False, original_swap_restored=True,
                                            client_unloaded=True, pools_empty=True, settings_restored=True,
                                            cgroup_restored=True, logs_retained=True,
                                            result='Hardware run had an exit error; fix passed local tests; user declined retest.'))
    print('CLEANUP COMPLETE: no retest; original swap/settings restored; Client unloaded; logs retained.', flush=True)


def main():
    global RUN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    RUN = args.directory.resolve()
    require(RUN.is_dir(), 'Run directory missing')
    try:
        execute()
    except Exception as error:
        write_json('cleanup.failure.json', dict(error=repr(error), test_rerun=False))
        raise


if __name__ == '__main__':
    main()

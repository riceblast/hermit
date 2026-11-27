#!/usr/bin/env python3
"""Start a command in a preconfigured cgroup-v1 memory group with fixed placement."""
import argparse
import ctypes
import errno
import os
from pathlib import Path
import signal
import struct
import sys
import time

POLICIES = {'pureDRAM': 1, 'purePMem': 2}
CONTROL = Path('/sys/kernel/debug/rswap/app_policy')
BATCH_ACCOUNT = Path('/sys/kernel/debug/hermit/batch_account')
COMMAND = struct.Struct('=IiII')
ACK = struct.Struct('=QII')
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
UNLIMITED_MIN = 1 << 60  # Same cgroup-v1 unlimited convention as rswap_hotness.


def group_pids(group):
    return [int(word) for word in (group / 'cgroup.procs').read_text().split()]


def preflight(group):
    group = group.resolve(strict=True)
    if not (group / 'memory.limit_in_bytes').is_file():
        raise ValueError('a cgroup-v1 memory directory is required')
    if not (group.parent / 'memory.limit_in_bytes').is_file():
        raise ValueError('the root memory cgroup cannot be used')
    if group_pids(group):
        raise ValueError('the application cgroup must be empty')
    if any(p.is_dir() and (p / 'cgroup.procs').exists() for p in group.iterdir()):
        raise ValueError('nested cgroups are not supported')
    if BATCH_ACCOUNT.read_text().strip() != 'N':
        raise ValueError('batch_account must already be N; no settings were changed')
    for ancestor in (group, *group.parents):
        if not (ancestor / 'memory.limit_in_bytes').exists():
            break
        limit = int((ancestor / 'memory.memsw.limit_in_bytes').read_text())
        if limit < UNLIMITED_MIN:
            raise ValueError(f'finite memsw limit at {ancestor}; remove it before running')
    return group


def join_group(group, pid):
    with (group / 'cgroup.procs').open('w') as stream:
        stream.write(str(pid))


def register(fd, pid, policy):
    packet = COMMAND.pack(1, pid, POLICIES[policy], 0)
    if os.write(fd, packet) != len(packet):
        raise OSError(errno.EIO, 'short app_policy write')
    data = os.pread(fd, ACK.size, 0)
    if len(data) != ACK.size:
        raise OSError(errno.EIO, 'short app_policy acknowledgement')
    binding, actual, active = ACK.unpack(data)
    if not binding or actual != POLICIES[policy] or active != 1:
        raise OSError(errno.ESTALE, 'application binding is not active')
    return binding


def subreaper(enable):
    """Reap orphaned descendants while waiting for the whole group to finish."""
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0):  # PR_GET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'PR_GET_CHILD_SUBREAPER')
    if libc.prctl(36, int(enable), 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'PR_SET_CHILD_SUBREAPER')
    return previous.value


def signal_group(group, leader, signum):
    # The process group also covers a child still waiting at the launch gate.
    try:
        os.killpg(leader, signum)
    except ProcessLookupError:
        pass
    # Descendants that create another session still belong to the memory group.
    for pid in group_pids(group):
        if pid == os.getpid():
            continue
        try:
            if os.getpgid(pid) == leader:
                continue  # Already signalled through killpg above.
            if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
                os.kill(pid, signum)
                continue
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            # Recheck after acquiring a stable reference to the process.
            if pid in group_pids(group):
                signal.pidfd_send_signal(fd, signum)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)


def wait_group(group, leader, pending, main_status=None):
    deadline = None
    while True:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if not pid:
                break
            if pid == leader:
                main_status = os.waitstatus_to_exitcode(status)
        members = group_pids(group)
        if main_status is not None and not members:
            return 128 + pending[0] if pending[0] else (
                main_status if main_status >= 0 else 128 - main_status)
        if pending[0] and deadline is None:
            signal_group(group, leader, pending[0])
            deadline = time.monotonic() + 5
        elif deadline is not None and time.monotonic() >= deadline:
            signal_group(group, leader, signal.SIGKILL)
            deadline = time.monotonic() + 5
        time.sleep(0.05)


def run(group, policy, command):
    group = preflight(group)
    fd = os.open(CONTROL, os.O_RDWR | os.O_CLOEXEC)
    gate_read = gate_write = -1
    child = None
    old_handlers = {}
    old_mask = None
    previous_subreaper = None
    pending = [0]
    finished = False

    def interrupted(signum, frame):
        if not pending[0]:
            pending[0] = signum

    try:
        previous_subreaper = subreaper(True)
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        for signum in SIGNALS:
            old_handlers[signum] = signal.signal(signum, interrupted)
        gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
        child = os.fork()
        if child == 0:
            try:
                os.close(fd)  # Never let the workload keep the binding alive.
                os.close(gate_write)
                os.setpgid(0, 0)
                for signum in SIGNALS:
                    signal.signal(signum, signal.SIG_DFL)
                signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                if os.read(gate_read, 1) != b'x':
                    os._exit(125)
                os.close(gate_read)
                os.execvp(command[0], command)
            except OSError as exc:
                os.write(2, f'rswap_run: exec: {exc}\n'.encode())
                os._exit(127 if exc.errno == errno.ENOENT else 126)
            except BaseException:
                os._exit(125)
        os.close(gate_read)
        gate_read = -1
        os.setpgid(child, child)
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        join_group(group, child)
        binding = register(fd, child, policy)
        if pending[0]:
            return 128 + pending[0]
        print(f'START pid={child} binding={binding} policy={policy} cgroup={group}', flush=True)
        os.write(gate_write, b'x')
        os.close(gate_write)
        gate_write = -1
        result = wait_group(group, child, pending)
        finished = True
        return result
    finally:
        for pipe in (gate_read, gate_write):
            if pipe >= 0:
                os.close(pipe)
        if child is not None and not finished:
            # Failure before registration never releases the command. Runtime
            # errors stop our tasks before releasing the binding where possible.
            try:
                signal_group(group, child, signal.SIGKILL)
            except OSError:
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass
            try:
                wait_group(group, child, [signal.SIGKILL], main_status=0)
            except OSError:
                pass
        os.close(fd)
        if previous_subreaper is not None:
            subreaper(previous_subreaper)
        if old_mask is not None:
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        if old_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True, choices=POLICIES)
    parser.add_argument('--memory-cgroup', required=True, type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command is required after --')
    try:
        return run(args.memory_cgroup, args.policy, command)
    except (OSError, ValueError) as exc:
        print(f'rswap_run: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

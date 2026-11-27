"""Unprivileged checks for the privileged test controller's process and flow contracts."""
from contextlib import ExitStack
import hashlib
import builtins
import errno
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import symtable
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import stage1_extended_host as host


class RecoveryFixture:
    """Private fake proc/sysfs files; real controller sequencing, no host mutations."""

    def __init__(self, test, directory, after_check=True, existing_group=False):
        self.test, self.base = test, Path(directory)
        self.run = self.base / 'run'
        self.repo = self.base / 'repo'
        self.proc = self.base / 'proc'
        self.module = self.base / 'sys/module/rswap_client'
        self.trace_root = self.base / 'trace'
        self.trace = self.trace_root / 'instances/hermit_p12_extended'
        self.cg = self.base / 'cg/hermit'
        self.bfs_cg = self.base / 'cg/bfs'
        self.hmt = self.base / 'hmt'
        self.small = self.run / 'swap-small.img'
        self.after_check = after_check
        self.controller, self.target, self.waiter = 7000001, 7000002, 7000003
        self.swaps = [str(self.small)]
        self.bound = True
        self.events, self.signals, self.labels = [], [], []
        self.original = {str(self.hmt / 'vaddr_swapout'): 'N', str(self.base / 'page-cluster'): '3'}
        self.group_before = (dict(zip(host.GROUP_SETTINGS, (str(16 * 1024**3), str(8 * 1024**3), '60')))
                             if existing_group else None)
        for path in (self.run, self.trace, self.cg, self.hmt, self.module / 'parameters', self.proc):
            path.mkdir(parents=True, exist_ok=True)
        self.put(self.small, 'swap-image')
        self.put(self.run / 'pattern', 'test-only-binary')
        self.put(self.trace / 'tracing_on', '0\n')
        self.put(self.cg / 'cgroup.procs', str(self.target) + '\n')
        self.put(self.trace_root / 'kprobe_events', 'unrelated_probe\n')
        self.put(self.module / 'version', '1.17\n')
        for name, value in (('nr_servers', '2'), ('sip', '10.0.0.13'), ('sip2', '10.0.0.20'), ('rmsize', '96')):
            self.put(self.module / 'parameters' / name, value + '\n')
        config = self.repo / 'remoteswap/client/rswap_client.conf'
        self.put(config, 'unchanged configuration\n')
        self.put(self.run / 'client.conf.before', config.read_text())
        self.put(self.run / 'settings.before.json', json.dumps(self.original))
        self.put(self.run / 'bfs.cgroup.before.json', json.dumps(self.group_before))
        self.put(self.run / 'hermit.metadata.json', json.dumps(dict(pid=self.target)))
        self.put(self.run / 'reuse.results.json', json.dumps(dict(actual_reused_offsets=7725, content_verified=True)))
        self.put(self.run / 'dmesg.before', 'boot baseline\n')
        self.put(self.hmt / 'vaddr_swapout', 'Y\n')
        self.put(self.base / 'page-cluster', '0\n')
        failure = "kprobe_events open('a') OSError: [Errno 22] Invalid argument\n"
        self.put(self.run / 'failure.txt', failure)
        if after_check:
            self.put(self.run / 'failed-trace-open/failure.txt', failure)
            self.put(self.run / 'failure.txt', 'RuntimeError: Controller PID reused before cleanup\n')
            self.put(self.run / 'recovery.started', 'previous recovery reached its guard\n')
        script_args = [str(Path(host.__file__).resolve()), str(self.run)]
        self.make_process(self.controller, 1, 100, ['python3'] + script_args)
        self.make_process(self.target, self.controller, 300, [str(self.run / 'pattern'), '32'])
        (self.proc / str(self.target) / 'exe').symlink_to(self.run / 'pattern')
        if after_check:
            self.make_process(self.waiter, 1, 200, ['python3'] + script_args + ['--recover-trace-open'])
        artifact = self.run / 'stable-artifact'
        self.put(artifact, 'source or module input\n')
        manifest = 'controller-check-artifacts.json' if after_check else 'recovery-artifacts.json'
        self.put(self.run / manifest, json.dumps({str(artifact): hashlib.sha256(artifact.read_bytes()).hexdigest()}))
        if existing_group:
            self.make_group(self.group_before)

    @staticmethod
    def put(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def make_process(self, pid, parent, start, arguments, state='S'):
        values = [state, str(parent)] + ['0'] * 17 + [str(start)]
        self.put(self.proc / str(pid) / 'stat', str(pid) + ' (name with ) spaces) ' + ' '.join(values))
        (self.proc / str(pid) / 'cmdline').write_bytes(b'\0'.join(value.encode() for value in arguments) + b'\0')

    def make_group(self, values):
        self.put(self.bfs_cg / 'cgroup.procs', '')
        for name, value in values.items():
            self.put(self.bfs_cg / name, value + '\n')

    def translated_path(self, value):
        text = str(value)
        if text == '/proc' or text.startswith('/proc/') or text.startswith('/sys/module/'):
            return self.base / text.lstrip('/')
        return Path(value)

    def kill(self, pid, sig):
        self.test.assertEqual(sig, signal.SIGTERM)
        self.test.assertIn(pid, (self.controller, self.waiter))
        self.signals.append(pid)
        self.events.append('terminate:' + str(pid))
        path = self.proc / str(pid) / 'stat'
        path.write_text(path.read_text().replace(') S ', ') Z '))
        if pid == self.controller:
            path = self.proc / str(self.target) / 'stat'
            path.write_text(path.read_text().replace(') S ', ') Z '))
            (self.cg / 'cgroup.procs').write_text('')
            self.bound = False

    def snapshot(self, label):
        self.labels.append(label)
        return dict(bound=str(int(self.bound)), pid=str(self.target if self.bound else 0),
                    entries='0', aliases='0', alloc_errors='0')

    def command(self, args, label, timeout=180):
        self.events.append(label)
        self.put(self.run / label, '')
        if args[0] == 'swapoff':
            self.test.assertEqual(self.swaps, [str(args[1])])
            self.test.assertIn(self.controller, self.signals)
            self.swaps = []
        elif args[0] == 'swapon':
            self.test.assertEqual(self.swaps, [])
            self.swaps = [str(args[1])]
        elif args[-1] == 'uninstall':
            self.test.assertFalse(self.swaps)
            import shutil
            shutil.rmtree(self.module)
        elif args[0] == 'dmesg':
            self.put(self.run / label, 'boot baseline\nrswap: normal completion\n')
        else:
            self.test.fail('Unexpected external command: ' + repr(args))

    def hermit(self):
        self.test.assertEqual(self.swaps, [str(self.small)])
        self.test.assertFalse(self.cg.exists())
        self.test.assertFalse(self.trace.exists())
        self.events.append('hermit')
        return dict(simulated_stage=True)

    def lifecycle(self, path):
        self.test.assertEqual(path, self.small)
        self.events.append('lifecycle')
        return dict(simulated_stage=True)

    def bfs(self):
        self.test.assertEqual(self.swaps, [host.BIG_SWAP])
        self.events.append('bfs')
        # The real script first removes memsw's limit, then raises memory's limit.
        self.make_group(dict(zip(host.GROUP_SETTINGS, (str(1 << 62), str(24 * 1024**3), '100'))))
        return dict(simulated_stage=True)

    def execute(self):
        actual_rmdir = type(self.base).rmdir
        actual_write = type(self.base).write_text

        def virtual_rmdir(path):
            if path in (self.trace, self.cg, self.bfs_cg):
                for child in path.iterdir():
                    child.unlink()
            return actual_rmdir(path)

        def group_write(path, data, *args, **kwargs):
            if path.parent == self.bfs_cg and path.name in host.GROUP_SETTINGS[:2]:
                other = self.bfs_cg / ('memory.memsw.limit_in_bytes' if path.name == 'memory.limit_in_bytes'
                                       else 'memory.limit_in_bytes')
                if other.exists():
                    memory, memsw = ((int(data), int(other.read_text())) if path.name == 'memory.limit_in_bytes'
                                     else (int(other.read_text()), int(data)))
                    self.test.assertLessEqual(memory, memsw, 'cgroup restoration violated memory <= memsw')
            return actual_write(path, data, *args, **kwargs)

        with ExitStack() as stack:
            for name, value in dict(RUN=self.run, REPO=self.repo, TRACE_ROOT=self.trace_root, TRACE=self.trace,
                                    HMT_CG=self.cg, BFS_CG=self.bfs_cg, HMT=self.hmt, START=time.monotonic(),
                                    OPEN_CONTROLS=set()).items():
                stack.enter_context(patch.object(host, name, value, create=True))
            stack.enter_context(patch.object(host.core, 'RUN', self.run))
            stack.enter_context(patch.object(host, 'Path', side_effect=self.translated_path))
            stack.enter_context(patch.object(host.os, 'geteuid', return_value=0))
            stack.enter_context(patch.object(host.os, 'uname', return_value=SimpleNamespace(version='#9 SMP Sat Nov 28 00:02:37 CST 2026')))
            stack.enter_context(patch.object(host.os, 'kill', side_effect=self.kill))
            stack.enter_context(patch.object(host.core, 'snapshot', side_effect=self.snapshot))
            stack.enter_context(patch.object(host.core, 'swaps', side_effect=lambda: [[name, 'file', '1', '0', '-2'] for name in self.swaps]))
            stack.enter_context(patch.object(host.core, 'empty_mapping'))
            stack.enter_context(patch.object(host, 'command', side_effect=self.command))
            stack.enter_context(patch.object(host, 'state', side_effect=lambda message: (self.run / 'state').write_text(message)))
            stack.enter_context(patch.object(host, 'test_hermit', side_effect=self.hermit))
            stack.enter_context(patch.object(host, 'test_lifecycle', side_effect=self.lifecycle))
            stack.enter_context(patch.object(host, 'test_bfs', side_effect=self.bfs))
            stack.enter_context(patch.object(type(self.base), 'rmdir', virtual_rmdir))
            stack.enter_context(patch.object(type(self.base), 'write_text', group_write))
            host.recover_trace_open(after_controller_check=self.after_check)


class ControllerTests(unittest.TestCase):
    def test_no_unresolved_global_references(self):
        for module in (host, host.core):
            source = Path(module.__file__)
            table = symtable.symtable(source.read_text(), str(source), 'exec')
            bound = {symbol.get_name() for symbol in table.get_symbols()
                     if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()}
            allowed = set(vars(builtins)) | bound | {'__file__', '__name__'}

            def check(node):
                for symbol in node.get_symbols():
                    if symbol.is_referenced() and symbol.is_global():
                        self.assertIn(symbol.get_name(), allowed, source.name + ':' + node.get_name())
                for child in node.get_children():
                    check(child)
            check(table)

    def test_recovery_through_final_cleanup(self):
        for after_check, existing_group in ((False, False), (True, False), (True, True)):
            with self.subTest(after_check=after_check, existing_group=existing_group), tempfile.TemporaryDirectory() as directory:
                fixture = RecoveryFixture(self, directory, after_check, existing_group)
                fixture.execute()
                self.assertTrue((fixture.run / 'SUCCESS').exists())
                self.assertFalse(fixture.small.exists())
                self.assertEqual(fixture.swaps, ['/swap.img'])
                self.assertEqual(fixture.signals, [fixture.controller] + ([fixture.waiter] if after_check else []))
                stages = [name for name in fixture.events if name in ('hermit', 'lifecycle', 'bfs')]
                self.assertEqual(stages, ['hermit', 'lifecycle', 'bfs'])
                self.assertEqual((fixture.hmt / 'vaddr_swapout').read_text().strip(), 'N')
                self.assertEqual((fixture.base / 'page-cluster').read_text().strip(), '3')
                self.assertEqual(fixture.bfs_cg.exists(), existing_group)
                if existing_group:
                    for name, value in fixture.group_before.items():
                        self.assertEqual((fixture.bfs_cg / name).read_text().strip(), value)

    def test_recovery_refuses_wrong_parent_before_any_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RecoveryFixture(self, directory)
            (fixture.proc / str(fixture.controller) / 'cmdline').write_bytes(b'python3\0unrelated.py\0')
            with self.assertRaisesRegex(RuntimeError, 'parent is not the expected'):
                fixture.execute()
            self.assertEqual(fixture.signals, [])
            self.assertEqual(fixture.swaps, [str(fixture.small)])

    def test_recovery_refuses_changed_artifact_before_any_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RecoveryFixture(self, directory)
            (fixture.run / 'stable-artifact').write_text('changed')
            with self.assertRaisesRegex(RuntimeError, 'Recovery artifact changed'):
                fixture.execute()
            self.assertEqual(fixture.signals, [])

    def test_recovery_refuses_unexpected_swap_before_any_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RecoveryFixture(self, directory)
            fixture.swaps = ['/some-other-swap']
            with self.assertRaisesRegex(RuntimeError, 'Unexpected swap'):
                fixture.execute()
            self.assertEqual(fixture.signals, [])

    def test_trace_append_preserves_other_probes_and_never_seeks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'kprobe_events'
            path.write_text('other-probe\n')
            with patch.object(host, 'TRACE_ROOT', root), patch.object(host.os, 'lseek', side_effect=AssertionError('no seek')):
                host.trace_command('p:test/hit hermit_try_to_unmap_one')
                host.trace_command('-:test/hit')
            self.assertEqual(path.read_text(), 'other-probe\np:test/hit hermit_try_to_unmap_one\n-:test/hit\n')

    def test_python_append_seek_error_does_not_affect_raw_writer(self):
        import _pyio
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'kprobe_events'
            path.write_text('existing\n')
            with patch.object(host.os, 'lseek', side_effect=OSError(errno.EINVAL, 'SEEK_END unsupported')):
                with self.assertRaises(OSError):
                    _pyio.FileIO(path, 'a')
                with patch.object(host, 'TRACE_ROOT', root):
                    host.trace_command('new-probe')
            self.assertEqual(path.read_text(), 'existing\nnew-probe\n')

    def test_failure_pauses_running_bfs_without_cleanup(self):
        for already_paused in (False, True):
            with self.subTest(already_paused=already_paused), tempfile.TemporaryDirectory() as directory:
                run = Path(directory)
                job = SimpleNamespace(poll=lambda: None)
                with patch.object(host.core, 'RUN', run), patch.object(host, 'BFS_JOB', job), \
                     patch.object(host, 'BFS_PID', 1234), patch.object(host, 'PAUSED', [(1234, 1)] if already_paused else []), \
                     patch.object(host, 'pause_bfs') as pause, patch.object(host, 'state'), \
                     patch.object(host, 'command') as external:
                    try:
                        raise RuntimeError('identity mismatch sentinel')
                    except RuntimeError:
                        host.record_failure()
                    self.assertIn('identity mismatch sentinel', (run / 'failure.txt').read_text())
                    self.assertEqual(pause.call_count, int(not already_paused))
                    external.assert_not_called()

    def test_bfs_sampling_stable_changed_and_incorrect_entries(self):
        for kind in ('stable', 'changed', 'incorrect'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root, begin = Path(directory), 0x100000
                region = dict(start=begin, end=begin + 4096 * 4096, swap_kib=16384)
                reads = {}

                def pread(fd, size, offset):
                    index = offset // 8 - begin // 4096
                    reads[index] = reads.get(index, 0) + 1
                    if kind == 'changed' and index % 8 == 0 and reads[index] % 2 == 0:
                        return struct.pack('=Q', 1 << 63)
                    return struct.pack('=Q', (1 << 62) | ((index + 1) << 5))

                def lookup(fd, offset):
                    va = begin + (offset - 1) * 4096
                    if kind == 'incorrect' or (kind == 'changed' and (offset - 1) % 8 == 0):
                        return dict(attached='1', found='0')
                    return dict(attached='1', found='1', type='0', pid=str(os.getpid()),
                                vpn=str(va // 4096), va=hex(va), aliases='1')

                with patch.object(host.core, 'RUN', root), patch.object(host, 'RUN', root), \
                     patch.object(host, 'pause_bfs'), patch.object(host, 'resume_bfs') as resume, \
                     patch.object(host, 'BFS', Path(sys.executable)), patch.object(host, 'bfs_regions', return_value=[region]), \
                     patch.object(host.os, 'pread', side_effect=pread), patch.object(host, 'lookup', side_effect=lookup), \
                     patch.object(host.core, 'snapshot'), patch.object(host, 'state'):
                    if kind == 'incorrect':
                        with self.assertRaisesRegex(RuntimeError, 'BFS identity mismatch'):
                            host.audit_bfs(os.getpid(), 123, 0)
                        resume.assert_not_called()
                    else:
                        result = host.audit_bfs(os.getpid(), 123, 0)
                        self.assertEqual(result['checked'], 2048)
                        self.assertEqual(result['changed'] > 0, kind == 'changed')
                        resume.assert_called_once()

    def test_real_thread_group_pause_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / 'payload.py'
            payload.write_text("import json,os,sys,threading\ne=threading.Event()\nthreads=[threading.Thread(target=e.wait) for _ in range(4)]\nfor t in threads: t.start()\nprint(json.dumps(dict(pid=os.getpid())),flush=True)\nsys.stdin.readline()\ne.set()\nfor t in threads: t.join()\n")
            wrapper = root / 'wrapper.py'
            wrapper.write_text("import subprocess,sys\np=subprocess.Popen([sys.executable,'-u',sys.argv[1]],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)\nprint(p.stdout.readline(),end='',flush=True)\np.stdin.write(sys.stdin.readline());p.stdin.flush()\np.wait(timeout=10)\n")
            parent = subprocess.Popen([sys.executable, '-u', str(wrapper), str(payload)], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            target = None
            try:
                target = json.loads(parent.stdout.readline())['pid']
                with patch.object(host, 'BFS_JOB', parent), patch.object(host, 'PAUSED', []):
                    host.pause_bfs(target)
                    states = [path.read_text().split('State:')[1].lstrip()[0]
                              for path in Path('/proc/' + str(target) + '/task').glob('*/status')]
                    self.assertEqual(len(states), 5)
                    self.assertTrue(all(value == 'T' for value in states))
                    self.assertEqual([pid for pid, _ in host.PAUSED], [target, parent.pid])
                    host.resume_bfs()
                    self.assertEqual(host.PAUSED, [])
                parent.stdin.write('finish\n')
                parent.stdin.flush()
                self.assertEqual(parent.wait(timeout=10), 0)
            finally:
                if target is not None and not host.process_gone(target):
                    os.kill(target, signal.SIGCONT)
                    os.kill(target, signal.SIGTERM)
                if parent.poll() is None:
                    parent.terminate()
                    parent.wait(timeout=10)
                for stream in (parent.stdin, parent.stdout, parent.stderr):
                    stream.close()

    def test_resume_tolerates_an_ancestor_exiting(self):
        with patch.object(host, 'PAUSED', [(1234, 10)]), \
             patch.object(host, 'proc_identity', return_value=(1, 10)), \
             patch.object(host.os, 'kill', side_effect=ProcessLookupError):
            host.resume_bfs()
            self.assertEqual(host.PAUSED, [])

    def test_lifecycle_fd_and_mm_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root, contexts, instances, audits = Path(directory), {}, [], []
            current, next_fd, active = None, 100, True

            class Workload:
                pid, length, mm = 4242, 4 * 1024**2, 1
                def __init__(self, name, mib):
                    instances.append(self)
                def send(self, text):
                    return dict(command=text)
                def execute_again(self):
                    self.mm += 1
                def finish(self):
                    self.finished = True

            def opened(path, flags):
                nonlocal next_fd
                self.assertEqual(path, host.RS / 'identity')
                next_fd += 1
                contexts[next_fd] = dict(used=False)
                return next_fd

            def written(fd, data):
                nonlocal current
                self.assertEqual(data, b'bind 4242\n')
                if contexts[fd]['used']:
                    raise OSError(errno.ESTALE, 'one binding per fd')
                self.assertTrue(active)
                self.assertIsNone(current)
                contexts[fd].update(used=True, mm=instances[0].mm)
                current = fd
                return len(data)

            def read(fd, size, offset):
                return f'attached={int(current == fd)} pid=4242 binding_id={fd}\n'.encode()

            def closed(fd):
                nonlocal current
                if current == fd:
                    current = None
                del contexts[fd]

            def snapshot(label):
                return dict(bound=str(int(current is not None)), binding_id=str(current or 0),
                            entries='0', aliases='0', alloc_errors='0')

            def audit(work, label, expected):
                self.assertEqual(host.core.CONTROL, current)
                self.assertEqual(contexts[current]['mm'] == work.mm, expected)
                audits.append(expected)
                return dict(checked=1024)

            def stop_swap(path, label):
                nonlocal active, current
                self.assertTrue(active)
                active, current = False, None

            def command(args, label):
                nonlocal active
                self.assertEqual(args, ['swapon', str(root / 'swap-small.img')])
                self.assertFalse(active)
                active = True

            with patch.object(host.core, 'RUN', root), patch.object(host, 'RUN', root), \
                 patch.object(host, 'OPEN_CONTROLS', set()), patch.object(host.core, 'CONTROL', None), \
                 patch.object(host, 'Workload', Workload), patch.object(host.os, 'open', side_effect=opened), \
                 patch.object(host.os, 'write', side_effect=written), patch.object(host.os, 'pread', side_effect=read), \
                 patch.object(host.os, 'close', side_effect=closed), patch.object(host.core, 'snapshot', side_effect=snapshot), \
                 patch.object(host.core, 'audit', side_effect=audit), patch.object(host.core, 'await_swap'), \
                 patch.object(host, 'stop_swap', side_effect=stop_swap), patch.object(host, 'command', side_effect=command), \
                 patch.object(host, 'state'):
                result = host.test_lifecycle(root / 'swap-small.img')
                self.assertEqual(host.OPEN_CONTROLS, set())
                self.assertEqual(contexts, {})
            self.assertEqual(audits, [True, True, False, True, True])
            self.assertEqual(result['binding_ids'], [101, 102, 103, 104])
            self.assertTrue(instances[0].finished)

    def test_hermit_trace_configuration_and_hit_requirement(self):
        for matching_pid in (True, False):
            with self.subTest(matching_pid=matching_pid), tempfile.TemporaryDirectory() as directory:
                import shutil
                root = Path(directory)
                run, cg, hmt, trace_root = [root / name for name in ('run', 'cg', 'hmt', 'trace-root')]
                trace = trace_root / 'instance'
                for path in (run, hmt, trace_root):
                    path.mkdir()
                (trace_root / 'kprobe_profile').write_text('test/hermit_hit 1 0\n')
                actual_mkdir, actual_rmdir = type(root).mkdir, type(root).rmdir
                commands, sends = [], []

                def mkdir(path, *args, **kwargs):
                    actual_mkdir(path, *args, **kwargs)
                    if path == trace:
                        (path / 'options').mkdir()
                        (path / 'per_cpu/cpu0').mkdir(parents=True)
                        (path / 'per_cpu/cpu0/stats').write_text('overrun: 0\n')
                        pid = 4242 if matching_pid else 4243
                        (path / 'trace').write_text(f'pattern-{pid} [000] ... hermit_hit: (hermit_try_to_unmap_one+0x0)\n')
                    if path == cg:
                        (path / 'cgroup.procs').write_text('')

                def rmdir(path):
                    if path in (trace, cg):
                        return shutil.rmtree(path)
                    return actual_rmdir(path)

                def trace_command(text):
                    commands.append(text)
                    if text.startswith('p:'):
                        (trace / 'events' / host.PROBE).mkdir(parents=True)

                test = self
                class Workload:
                    pid, length = 4242, 32 * 1024**2
                    def __init__(self, name, mib):
                        test.assertEqual(os.environ['RSWAP_STAGE1_CGROUP'], str(cg))
                    def send(self, text):
                        sends.append(text)
                    def finish(self):
                        sends.append('finish')

                with ExitStack() as stack:
                    for name, value in dict(RUN=run, HMT_CG=cg, HMT=hmt, TRACE=trace, TRACE_ROOT=trace_root).items():
                        stack.enter_context(patch.object(host, name, value))
                    stack.enter_context(patch.object(host.core, 'RUN', run))
                    stack.enter_context(patch.object(type(root), 'mkdir', mkdir))
                    stack.enter_context(patch.object(type(root), 'rmdir', rmdir))
                    stack.enter_context(patch.object(host, 'Workload', Workload))
                    stack.enter_context(patch.object(host, 'bind', return_value=(99, 123)))
                    close = stack.enter_context(patch.object(host, 'close_control'))
                    stack.enter_context(patch.object(host.core, 'snapshot', return_value=dict(bound='0', entries='0', aliases='0')))
                    stack.enter_context(patch.object(host.core, 'audit', return_value=dict(checked=4096)))
                    stack.enter_context(patch.object(host.core, 'await_swap'))
                    stack.enter_context(patch.object(host, 'state'))
                    stack.enter_context(patch.object(host, 'trace_command', side_effect=trace_command))
                    if matching_pid:
                        result = host.test_hermit()
                        self.assertEqual(result['recorded_function_hits'], 1)
                        self.assertFalse(trace.exists())
                        self.assertFalse(cg.exists())
                        self.assertEqual((hmt / 'vaddr_swapout').read_text().strip(), 'N')
                        close.assert_called_once_with(99)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'No recorded Hermit function hit'):
                            host.test_hermit()
                        self.assertTrue(trace.exists())
                        self.assertTrue(cg.exists())
                        for name, value in (('context-info', '1'), ('raw', '0'), ('hex', '0'), ('bin', '0')):
                            self.assertEqual((trace / 'options' / name).read_text().strip(), value)
                        close.assert_not_called()
                    self.assertNotIn('RSWAP_STAGE1_CGROUP', os.environ)
                self.assertEqual(sends, ['rewrite', 'verify', 'rewrite', 'finish'])
                self.assertEqual(len(commands), 2 if matching_pid else 1)

    def test_real_parent_start_time_and_guarded_termination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'holder.py'
            script.write_text("import json,subprocess,sys,time\ntime.sleep(.15)\np=subprocess.Popen([sys.executable,'-c','import sys; sys.stdin.read()'],stdin=subprocess.PIPE)\nprint(json.dumps(dict(target=p.pid)),flush=True)\nwhile True: time.sleep(1)\n")
            args = [sys.executable, '-u', str(script)]
            parent = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            target = None
            try:
                target = json.loads(parent.stdout.readline())['target']
                pid, start = host.held_controller_identity(target, args[1:])
                self.assertEqual(pid, parent.pid)
                self.assertNotEqual(start, host.proc_identity(target)[1])
                with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                    host.stop_expected_process(pid, start + 1)
                self.assertIsNone(parent.poll())
                host.stop_expected_process(pid, start)
                self.assertEqual(parent.wait(timeout=10), -signal.SIGTERM)
                deadline = time.monotonic() + 10
                while not host.process_gone(target):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
            finally:
                if parent.poll() is None:
                    parent.terminate()
                    parent.wait(timeout=10)
                parent.stdout.close()
                parent.stderr.close()
                if target is not None and not host.process_gone(target):
                    os.kill(target, signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()

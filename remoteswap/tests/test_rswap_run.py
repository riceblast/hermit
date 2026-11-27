"""Unprivileged launch-gate/process tests; cgroup and module operations are fakes."""
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
import rswap_run as runner


PROCESS_FIXTURE = r'''
import errno, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import rswap_run as r
root=Path(sys.argv[2]); mode=sys.argv[3]; command=json.loads(sys.argv[4])
state={}; control=root/'control'; control.touch(); r.CONTROL=control
r.preflight=lambda group: group
def join(group,pid): state['pid']=pid
r.join_group=join
def register(fd,pid,policy):
    assert state['pid']==pid and not (root/'started').exists()
    assert os.readlink('/proc/%d/fd/%d' % (os.getpid(),fd))==str(control)
    if mode=='register-fail': raise OSError(errno.EBUSY,'duplicate binding')
    return 42
r.register=register
def pids(group):
    if mode=='runtime-error' and (root/'started').exists() and not state.get('failed'):
        state['failed']=True; raise OSError(errno.EIO,'injected read error')
    ids=[state['pid']] if 'pid' in state else []
    if (root/'descendant').exists(): ids.append(int((root/'descendant').read_text()))
    live=[]
    for pid in ids:
        try: text=Path('/proc/%d/stat' % pid).read_text()
        except FileNotFoundError: continue
        if text[text.rfind(')')+2:].split()[0]!='Z': live.append(pid)
    return live
r.group_pids=pids
try: result=r.run(root,'pureDRAM',command)
except OSError as exc:
    print('EXPECTED ERROR:',exc); result=1
assert not pids(root), 'workload survived controller completion'
for fd in Path('/proc/self/fd').iterdir():
    try: assert os.readlink(fd)!=str(control), 'control fd leaked'
    except FileNotFoundError: pass
sys.exit(result)
'''


class RunnerTests(unittest.TestCase):
    def fixture(self, root):
        parent=root/'memory'; group=parent/'application'; group.mkdir(parents=True)
        for path in (parent,group):
            (path/'memory.limit_in_bytes').write_text(str(4<<30))
            (path/'memory.memsw.limit_in_bytes').write_text(str((1<<63)-4096))
            (path/'cgroup.procs').write_text('')
        batch=root/'batch_account'; batch.write_text('N\n')
        return parent,group,batch

    def test_preflight_accepts_existing_group_without_modifying_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); parent,group,batch=self.fixture(root)
            before={p:p.read_bytes() for p in group.iterdir()}
            with patch.object(runner,'BATCH_ACCOUNT',batch):
                self.assertEqual(runner.preflight(group),group)
            self.assertEqual(before,{p:p.read_bytes() for p in group.iterdir()})

    def test_preflight_rejects_known_unsafe_configurations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); parent,group,batch=self.fixture(root)
            with patch.object(runner,'BATCH_ACCOUNT',batch):
                batch.write_text('Y')
                with self.assertRaisesRegex(ValueError,'batch_account'): runner.preflight(group)
                batch.write_text('N'); (parent/'memory.memsw.limit_in_bytes').write_text(str(8<<30))
                with self.assertRaisesRegex(ValueError,'finite memsw'): runner.preflight(group)
                (parent/'memory.memsw.limit_in_bytes').write_text(str((1<<63)-4096))
                (group/'cgroup.procs').write_text('123\n')
                with self.assertRaisesRegex(ValueError,'empty'): runner.preflight(group)
                (group/'cgroup.procs').write_text('')
                nested=group/'nested'; nested.mkdir(); (nested/'cgroup.procs').touch()
                with self.assertRaisesRegex(ValueError,'nested'): runner.preflight(group)
                with self.assertRaisesRegex(ValueError,'root'): runner.preflight(parent)

    def test_registration_abi_and_stale_ack(self):
        for policy,number in runner.POLICIES.items():
            with patch.object(os,'write',return_value=16) as write, patch.object(
                    os,'pread',return_value=runner.ACK.pack(42,number,1)):
                self.assertEqual(runner.register(9,123,policy),42)
                self.assertEqual(write.call_args.args,(9,runner.COMMAND.pack(1,123,number,0)))
        with patch.object(os,'write',return_value=16), patch.object(
                os,'pread',return_value=runner.ACK.pack(42,1,0)):
            with self.assertRaises(OSError) as error: runner.register(9,123,'pureDRAM')
            self.assertEqual(error.exception.errno,errno.ESTALE)

    def process(self, root, mode, command, signal_after_start=False):
        args=[sys.executable,'-c',PROCESS_FIXTURE,str(Path(runner.__file__).parent),
              str(root),mode,json.dumps(command)]
        if not signal_after_start:
            return subprocess.run(args,text=True,capture_output=True,timeout=10)
        proc=subprocess.Popen(args,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not (root/'started').exists():
                if proc.poll() is not None or time.monotonic()>deadline:
                    self.fail('workload did not start')
                time.sleep(.01)
            proc.send_signal(signal.SIGTERM)
            stdout,stderr=proc.communicate(timeout=8)
            return subprocess.CompletedProcess(args,proc.returncode,stdout,stderr)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.communicate()

    def test_gate_exec_exit_code_and_control_fd_not_inherited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            code="""import os,sys
from pathlib import Path
r=Path(sys.argv[1])
for fd in Path('/proc/self/fd').iterdir():
    try: assert os.readlink(fd)!=str(r/'control')
    except FileNotFoundError: pass
(r/'started').write_text('yes')
sys.exit(7)
"""
            result=self.process(root,'normal',[sys.executable,'-c',code,str(root)])
            self.assertEqual(result.returncode,7,result.stderr)
            self.assertTrue((root/'started').exists())
            self.assertIn('binding=42 policy=pureDRAM',result.stdout)

    def test_registration_failure_never_executes_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            result=self.process(root,'register-fail',['touch',str(root/'started')])
            self.assertEqual(result.returncode,1,result.stderr)
            self.assertFalse((root/'started').exists())

    def test_missing_command_returns_127(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=self.process(Path(tmp),'normal',['/does/not/exist'])
            self.assertEqual(result.returncode,127,result.stderr)

    def test_waits_for_orphan_descendant_after_main_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            code="""import os,sys,time
from pathlib import Path
r=Path(sys.argv[1]); pid=os.fork()
if not pid:
    time.sleep(.25); (r/'done').write_text('yes'); os._exit(0)
(r/'descendant').write_text(str(pid))
sys.exit(7)
"""
            result=self.process(root,'normal',[sys.executable,'-c',code,str(root)])
            self.assertEqual(result.returncode,7,result.stderr)
            self.assertTrue((root/'done').exists())

    def test_signal_and_runtime_error_clean_up_workload(self):
        code="import sys,time; from pathlib import Path; Path(sys.argv[1]).write_text('yes'); time.sleep(30)"
        for mode in ('normal','runtime-error'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                result=self.process(root,mode,[sys.executable,'-c',code,str(root/'started')],
                                    signal_after_start=mode=='normal')
                self.assertEqual(result.returncode,143 if mode=='normal' else 1,result.stderr)


if __name__=='__main__':
    unittest.main()

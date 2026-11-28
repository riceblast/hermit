"""Validate operator helpers before any privileged stage-3 run."""
import importlib
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import mapping_harness
import placement_harness
import stage3_host as host
from test_stage1_identity import SUPPORT
from test_stage2_hardware_helpers import CASES
from test_stage3_policy import EXTRA, production

ROOT = Path(__file__).resolve().parents[1]


def trace_fixture(root):
    header=(ROOT/'client/rswap_trace.h').read_text()
    event=header.split('TRACE_EVENT(vpn_decision,',1)[1].split('TP_fast_assign',1)[0]
    declared=re.findall(r'__field\(([^,]+),\s*(\w+)\)',event)
    for name in ('vpn_decision','decision_raw','app_decision','decision','store_result'):
        path=root/'events/rswap'/name;path.mkdir(parents=True,exist_ok=True)
        (path/'filter').write_text('0\n');(path/'enable').write_text('0\n')
    (root/'tracing_on').write_text('0\n');(root/'trace').write_text('')
    (root/'events/rswap/vpn_decision/format').write_text('\n'.join(
        f'field:{kind} {name}; offset:{i*8}; size:8; signed:0;' for i,(kind,name) in enumerate(declared))+
        '\nprint fmt: "alloc_ret=%d"\n')

# Catch real same-value writes using read-only PTEs, without changing /proc or
# generating hardware swap traffic. A write fault makes that one page writable.
DIRTY_WRAPPER = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
static uintptr_t base;
static size_t length, expected;
static volatile sig_atomic_t faults;
static int armed;
static void on_fault(int sig, siginfo_t *info, void *context) {
    (void)sig;(void)context;
    uintptr_t addr=(uintptr_t)info->si_addr;
    if(!armed || addr<base || addr>=base+length) _exit(80);
    if(mprotect((void *)(addr&~(uintptr_t)4095),4096,PROT_READ|PROT_WRITE)) _exit(81);
    faults++;
}
int __real_madvise(void *,size_t,int);
int __wrap_madvise(void *addr,size_t len,int advice) {
    if(advice==MADV_NOHUGEPAGE) {
        base=(uintptr_t)addr;length=len;
        struct sigaction a={.sa_sigaction=on_fault,.sa_flags=SA_SIGINFO};
        sigemptyset(&a.sa_mask);assert(!sigaction(SIGSEGV,&a,NULL));
    }
    if(advice==MADV_PAGEOUT) {
        if(armed) assert((size_t)faults==expected);
        return 0; /* Never touch live swap/RDMA from a local helper test. */
    }
    return __real_madvise(addr,len,advice);
}
char *__real_fgets(char *,int,FILE *);
char *__wrap_fgets(char *buf,int size,FILE *f) {
    if(armed) {assert((size_t)faults==expected);armed=0;}
    char *ret=__real_fgets(buf,size,f);
    if(!ret) return ret;
    uintptr_t start=base;size_t bytes=length;unsigned long page;
#ifdef CHECK_DIRTY_COMMAND
    int test=!strcmp(buf,"dirty\n");
#else
    int test=!strcmp(buf,"out all\n");
    if(sscanf(buf,"out %lu",&page)==1) {test=1;start=base+page*4096;bytes=4096;}
#endif
    (void)page;
    if(test) {expected=bytes/4096;faults=0;armed=1;assert(!mprotect((void *)start,bytes,PROT_READ));}
    return ret;
}
'''


def build_safe_pattern(directory, source, dirty_command=False):
    wrapper=directory/'dirty-wrapper.c';wrapper.write_text(DIRTY_WRAPPER)
    binary=directory/'pattern'
    flags=['-DCHECK_DIRTY_COMMAND'] if dirty_command else []
    subprocess.run(['gcc','-O2','-std=gnu11','-Wall','-Wextra','-Werror','-pthread',*flags,
                    str(ROOT/'tests'/source),str(wrapper),'-Wl,--wrap=madvise','-Wl,--wrap=fgets',
                    '-o',str(binary)],check=True)
    return binary


class HelperTests(unittest.TestCase):
    def test_bfs_sampler_10s_two_rounds_keeps_core_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(host,'RUN',Path(tmp)), \
             patch.object(host.old,'spawn',return_value=SimpleNamespace(pid=99)) as spawn:
            host.Sampler(77,'bfs',window=host.BFS_EWMA_WINDOW,interval=host.BFS_INTERVAL)
            args=spawn.call_args.args[0]
            self.assertEqual(args[args.index('--a-bit-window')+1],'10')
            self.assertEqual(args[args.index('--ewma-window')+1],'2')
            host.Sampler(77,'core')
            args=spawn.call_args.args[0]
            self.assertEqual(args[args.index('--a-bit-window')+1],'5')
            self.assertEqual(args[args.index('--ewma-window')+1],'4')

    def test_existing_sampler_output_is_rejected_before_spawning(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(host,'RUN',Path(tmp)), \
             patch.object(host.old,'spawn') as spawn:
            (Path(tmp)/'controlled.heat').mkdir()
            with self.assertRaisesRegex(RuntimeError,'Sampler output must be new'):
                host.Sampler(77,'controlled')
            spawn.assert_not_called()

    def test_complete_flow_requires_unused_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);(run/'artifacts.json').write_text('{}')
            host.fresh_outputs(run)
            for name in ('controlled.heat','bfs-sampler.heat','bfs.ewma.tsv','core.results.json'):
                path=run/name;path.touch()
                with self.subTest(name=name),self.assertRaisesRegex(RuntimeError,'fresh directory'):
                    host.fresh_outputs(run)
                path.unlink()

    def test_real_cli_creates_summary_in_new_output_directory(self):
        from test_stage2_loop import HARNESS
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'loop.c';source.write_text(HARNESS);binary=root/'loop'
            subprocess.run(['gcc','-O1','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                            '-I',str(ROOT/'tools'),str(source),'-o',str(binary)],check=True)
            out=root/'controlled.heat'
            subprocess.run([str(binary),str(out),'0'],check=True,capture_output=True,timeout=15)
            self.assertEqual({p.name for p in out.iterdir()},{'summary.tsv','hot_distribute.tsv'})
            self.assertGreater(len((out/'summary.tsv').read_text().splitlines()),1)

    def test_resume_to_fresh_directory_preserves_prior_evidence_and_only_stops_known_jobs(self):
        self.resume_fixture(False)

    def test_resume_capacity_keeps_prior_passes_and_accepts_its_own_live_binding(self):
        self.resume_fixture(True)

    def resume_fixture(self, capacity_only):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);run=base/'new';prior=base/'old';debug=base/'debug';trace=base/'trace'
            for p in (run,prior,debug,trace): p.mkdir()
            (run/'artifacts.json').write_text('{}')
            (prior/'slot-cache-recovery.failure.txt').write_text('RuntimeError: controlled sampler exited: 1; inspect stderr')
            (prior/'controlled.stderr').write_text('create output directory (must be new): File exists\n')
            (prior/'controlled.heat').mkdir();(prior/'controlled.heat'/'summary.tsv').write_text('old evidence')
            (prior/'baseline.stats').write_text('instance_ns=123\n')
            (debug/'stats').write_text('instance_ns=123\n');(debug/'identity_status').write_text('bound=0\n')
            (trace/'tracing_on').write_text('0\n')
            trace_fixture(trace)
            for name,obj in [('controller.json',dict(pid=101,start_token=1001)),
                             ('controlled.target.json',dict(pid=102)),('settings.before.json',{}),
                             ('swaps.before.json',[]),('bfs.cgroup.before.json',None)]:
                (prior/name).write_text(json.dumps(obj))
            (prior/'dmesg.before').write_text('baseline\n')
            paths=[host.REPO/n for n in ('remoteswap/client/rswap-client.ko','remoteswap/client/rswap_client.conf')]
            (prior/'artifacts.json').write_text(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}))
            records={101:dict(pid=101,ppid=1,start_token=1001,state='S',argv=['/usr/bin/python3',str(Path(host.__file__).resolve()),str(prior),'--recover-slot-cache']),
                     102:dict(pid=102,ppid=101,start_token=1002,state='T',argv=[str(prior/'pattern')])}
            cg=base/'memory';cg.mkdir()
            if capacity_only:
                (prior/'failure.txt').write_text("Invalid argument; Trace('capacity-bulk',sampled=True); slot < 64 && alloc_ret == 0")
                (prior/'core.results.json').write_text(json.dumps(dict(contents=True,unknown=0,hot_and_cold=True)))
                (prior/'edges.results.json').write_text(json.dumps(dict(contents=True,fixed_priority=True)))
                (prior/'capacity.target.json').write_text(json.dumps(dict(pid=102,bytes=40*1024**3)))
                (prior/'resume.plan.json').write_text(json.dumps(dict(previous_run='/tmp/ancestor')))
                (debug/'identity_status').write_text('bound=1 pid=102\n')
                records[101]['argv']=['/usr/bin/python3',str(Path(host.__file__).resolve()),str(prior),'--resume-live','/tmp/ancestor']
                records[102]['argv']=[str(prior/'capacity-pattern'),'40960']
                group=cg/'hermit-p14-fixed';group.mkdir();(group/'cgroup.procs').write_text('')
            events=[]
            def send(fd,sig):
                pid=fd-9000;events.append((pid,sig))
                if sig==host.signal.SIGTERM: records[pid]['state']='Z'
            original_read=Path.read_text;original_iter=Path.iterdir
            def read(path,*args,**kw):
                if str(path)=='/sys/module/rswap_client/version': return '1.19\n'
                return original_read(path,*args,**kw)
            def iterate(path):
                return iter(Path('/proc')/str(p) for p in records) if str(path)=='/proc' else original_iter(path)
            with ExitStack() as stack:
                for obj,name,value in [(host,'RUN',run),(host,'RS',debug),(host,'TRACE',trace)]:
                    stack.enter_context(patch.object(obj,name,value))
                stack.enter_context(patch.object(host.previous,'CG_PARENT',cg))
                groups=stack.enter_context(patch.object(host,'TEST_GROUPS',[]))
                stack.enter_context(patch.object(Path,'read_text',read));stack.enter_context(patch.object(Path,'iterdir',iterate))
                stack.enter_context(patch.object(host.os,'geteuid',return_value=0))
                stack.enter_context(patch.object(host.os,'pidfd_open',side_effect=lambda pid:pid+9000))
                stack.enter_context(patch.object(host.os,'close'))
                stack.enter_context(patch.object(host.signal,'pidfd_send_signal',side_effect=send))
                stack.enter_context(patch.object(host,'process_record',side_effect=lambda pid:records.get(pid)))
                stack.enter_context(patch.object(host.core,'swaps',return_value=[[host.previous.BIG_SWAP]]))
                stack.enter_context(patch.object(host.old,'process_identity',return_value=(1,999)))
                stack.enter_context(patch.object(host,'settle'));stack.enter_context(patch.object(host,'state'))
                reset=stack.enter_context(patch.object(host,'reset_test_swap'))
                tests=stack.enter_context(patch.object(host,'run_tests'))
                command=stack.enter_context(patch.object(host,'command'))
                host.resume_live(prior)
                reset.assert_called_once_with('fresh-output-resume')
                if capacity_only:
                    tests.assert_called_once_with({},None,[],completed=('core','edges'))
                    self.assertEqual(host.TEST_GROUPS,[group])
                    self.assertTrue((run/'core.results.json').exists())
                    host.fresh_outputs(run,completed=('core','edges'))
                else: tests.assert_called_once_with({},None,[])
                command.assert_not_called()
            self.assertEqual((prior/'controlled.heat'/'summary.tsv').read_text(),'old evidence')
            self.assertFalse((run/'controlled.heat').exists())
            self.assertEqual({p for p,s in events},{101,102})
            self.assertLess(events.index((102,host.signal.SIGTERM)),events.index((101,host.signal.SIGTERM)))

    def test_sampled_trace_uses_declared_kernel_fields_not_print_labels(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(host,'TRACE',Path(tmp)):
            root=Path(tmp);trace_fixture(root)
            event=root/'events/rswap/vpn_decision'
            expression=host.vpn_sample_filter(event)
            self.assertEqual(expression,'slot < 64 && ret == 0\n')
            host.Trace('sampled',sampled=True)
            self.assertEqual((event/'filter').read_text(),expression)
            self.assertNotIn('alloc_ret',expression)

    def test_schema_preflight_records_acceptance_and_resets_filter_with_tracing_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);trace=base/'trace';run=base/'run';run.mkdir();trace_fixture(trace)
            with patch.object(host,'TRACE',trace),patch.object(host,'RUN',run): host.check_trace_schema()
            self.assertEqual((trace/'tracing_on').read_text(),'0\n')
            self.assertEqual((trace/'events/rswap/vpn_decision/filter').read_text(),'0\n')
            self.assertEqual(json.loads((run/'trace-schema.json').read_text())['expression'],'slot < 64 && ret == 0')

    def test_printed_alloc_ret_without_real_ret_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);trace_fixture(root);entry=root/'events/rswap/vpn_decision'
            text=(entry/'format').read_text().replace(' ret;',' wrong;')
            (entry/'format').write_text(text)
            with self.assertRaisesRegex(RuntimeError,'schema lacks slot/ret'): host.vpn_sample_filter(entry)
            self.assertEqual((entry/'filter').read_text(),'0\n')

    def test_review_freezes_sampler_and_bfs_until_explicit_stop_or_continue(self):
        for action in ('stop','continue'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(host,'RUN',Path(tmp)), patch.object(host.old,'target_finished',return_value=False), \
                 patch.object(host.old,'child_state',return_value='T'), \
                 patch.object(host,'snapshot',return_value={'pools':{(0,'dram'):{'used':20},(0,'pmem'):{'used':40}}}), \
                 patch.object(host,'state'), patch.object(host,'process_command',side_effect=[None,action]), \
                 patch.object(host.old,'check_kernel'), patch.object(host.time,'sleep'):
                events=[]
                report=SimpleNamespace(rows=[{'window':w} for w in range(16)])
                sampler=SimpleNamespace(job=SimpleNamespace(pid=88))
                with patch.object(host,'pin_owned',side_effect=lambda p,t,e:p+1000), \
                     patch.object(host.signal,'pidfd_send_signal',side_effect=lambda fd,s:events.append(('bfs' if fd==1077 else 'sampler',s))), \
                     patch.object(host.os,'close') as close:
                    self.assertEqual(host.wait_bfs_review(77,1,sampler,2,report,'adaptive_10_ewma'),action)
                    self.assertEqual({call.args[0] for call in close.call_args_list},{1077,1088})
                self.assertEqual(events[:2],[('sampler',host.signal.SIGSTOP),('bfs',host.signal.SIGSTOP)])
                self.assertEqual(events[-2:],[('bfs',host.signal.SIGCONT),('sampler',host.signal.SIGCONT)])
                self.assertEqual(('bfs',host.signal.SIGTERM) in events,action=='stop')
                review=json.loads((Path(tmp)/'bfs.review.json').read_text())
                self.assertEqual((review['completed_windows'],review['adaptive_ewma']),(15,10))
                self.assertEqual(review['pool_at_pause']['ratio'],'1:2')
                self.assertEqual(len(report.rows),16)

    def test_review_stop_tolerates_bfs_exiting_before_sigcont(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(host,'RUN',Path(tmp)), \
             patch.object(host.old,'target_finished',return_value=False), \
             patch.object(host.old,'child_state',return_value='T'), \
             patch.object(host,'snapshot',return_value={'pools':{(0,'dram'):{'used':20},(0,'pmem'):{'used':40}}}), \
             patch.object(host,'state'),patch.object(host,'process_command',return_value='stop'):
            sent=[]
            def send(fd,sig):
                sent.append((fd,sig))
                if (fd,sig)==(1077,host.signal.SIGCONT): raise ProcessLookupError()
            with patch.object(host,'pin_owned',side_effect=lambda p,t,e:p+1000) as pin, \
                 patch.object(host.signal,'pidfd_send_signal',side_effect=send),patch.object(host.os,'close'):
                action=host.wait_bfs_review(77,1,SimpleNamespace(job=SimpleNamespace(pid=88)),2,
                                          SimpleNamespace(rows=[{'window':w} for w in range(16)]),'adaptive_10_ewma')
            self.assertEqual(action,'stop')
            self.assertEqual(pin.call_count,2)  # identities pinned once across the whole wait
            self.assertIn((1088,host.signal.SIGCONT),sent)

    def test_process_exit_during_pin_is_normalized_and_closes_descriptor(self):
        with patch.object(host.os,'pidfd_open',return_value=99), \
             patch.object(host.old,'process_identity',side_effect=FileNotFoundError()), \
             patch.object(host.os,'close') as close:
            with self.assertRaises(ProcessLookupError): host.pin_owned(77,1,Path('/bin/other'))
            close.assert_called_once_with(99)

    def test_real_owned_child_pidfd_stays_safe_after_exit(self):
        binary=Path('/usr/bin/python3')
        job=subprocess.Popen([str(binary),'-c','import time; time.sleep(60)'])
        fd=None
        try:
            token=host.old.process_identity(job.pid)[1]
            fd=host.pin_owned(job.pid,token,binary)
            self.assertTrue(host.send_pinned(fd,host.signal.SIGSTOP))
            deadline=time.monotonic()+5
            while host.old.child_state(job.pid) not in ('T','t'):
                self.assertLess(time.monotonic(),deadline);time.sleep(.01)
            self.assertTrue(host.send_pinned(fd,host.signal.SIGTERM))
            host.send_pinned(fd,host.signal.SIGCONT)
            self.assertEqual(job.wait(timeout=5),-host.signal.SIGTERM)
            self.assertFalse(host.send_pinned(fd,host.signal.SIGCONT))
        finally:
            if fd is not None: os.close(fd)
            if job.poll() is None: job.kill();job.wait()

    def test_captured_post_or_wc_error_fails_immediately(self):
        from test_stage3_tools import decision
        from test_patch11_tools import result
        for results in ([result(0,-5)],[result(0),result(1,-5)]):
            with self.subTest(results=results),tempfile.TemporaryDirectory() as tmp, \
                 patch.object(host,'RUN',Path(tmp)):
                with self.assertRaisesRegex(RuntimeError,'post/WC reported an error'):
                    host.validate_trace('error',''.join([decision(),*results]))

    def test_operator_command_is_explicit_logged_and_consumed_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(host,'RUN',Path(tmp)):
            self.assertIsNone(host.process_command())
            path=Path(tmp)/'bfs.command'
            for action in ('continue','stop'):
                path.write_text(action+'\n')
                self.assertEqual(host.process_command(),action)
                self.assertFalse(path.exists())
                self.assertIsNone(host.process_command())
            self.assertEqual(len((Path(tmp)/'bfs.commands.jsonl').read_text().splitlines()),2)
            path.write_text('kill everything')
            with self.assertRaisesRegex(RuntimeError,'stop or continue'): host.process_command()

    def test_pattern_pageout_discard_refault_and_all_word_content_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary=build_safe_pattern(Path(tmp),'stage3_capacity_pattern.c')
            job=subprocess.Popen([str(binary),'4'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,text=True)
            try:
                ready=json.loads(job.stdout.readline())
                self.assertEqual(ready['bytes'],4*1024**2)
                for command in ('out all','verify','drop 7','refault 7','out 7','verify','finish'):
                    job.stdin.write(command+'\n');job.stdin.flush()
                    self.assertEqual(json.loads(job.stdout.readline()),dict(event='done',command=command))
                self.assertEqual(job.wait(timeout=15),0)
                self.assertEqual(job.stderr.read(),'')
            finally:
                if job.poll() is None: job.kill();job.wait()
                job.stdin.close();job.stdout.close();job.stderr.close()

    def test_controlled_dirty_writes_every_page_without_changing_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary=build_safe_pattern(Path(tmp),'stage2_pattern.c',dirty_command=True)
            job=subprocess.Popen([str(binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(json.loads(job.stdout.readline())['pages'],8192)
                for command in ('dirty','out','in','dirty','outcold','in','finish'):
                    job.stdin.write(command+'\n');job.stdin.flush()
                    self.assertEqual(json.loads(job.stdout.readline()),dict(event='done',command=command))
                self.assertEqual(job.wait(timeout=15),0)
                self.assertEqual(job.stderr.read(),'')
            finally:
                if job.poll() is None: job.kill();job.wait()
                job.stdin.close();job.stdout.close();job.stderr.close()

    def test_recovery_plan_accepts_only_original_controller_and_two_known_children(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(host,'RUN',Path(tmp)):
            run=Path(tmp);(run/'controlled.target.json').write_text(json.dumps(dict(pid=102)))
            owner=dict(pid=101,start_token=1001)
            records={
                101:dict(pid=101,ppid=1,start_token=1001,state='S',
                         argv=['/usr/bin/python3',str(Path(host.__file__).resolve()),str(run)]),
                102:dict(pid=102,ppid=101,start_token=1002,state='T',argv=[str(run/'pattern')]),
                103:dict(pid=103,ppid=101,start_token=1003,state='T',argv=[str(run/'sampler-audit'),
                    '--placement','--a-bit-window','5','--ewma-window','4','--out',str(run/'controlled.heat'),'--pid','102'])}
            self.assertEqual({r['pid'] for r in host.recovery_process_plan(owner,records)},{101,102,103})
            with self.assertRaisesRegex(RuntimeError,'PID was reused'):
                host.recovery_process_plan(dict(owner,start_token=99),records)
            records[104]=dict(pid=104,ppid=101,start_token=1004,state='S',argv=['/bin/other'])
            with self.assertRaisesRegex(RuntimeError,'Unexpected failed-controller children'):
                host.recovery_process_plan(owner,records)

    def test_recovery_sequence_retains_connections_and_only_replaces_helpers_after_exit(self):
        self.recovery_sequence(False)

    def test_slot_cache_recovery_stops_only_failed_recovery_controller(self):
        self.recovery_sequence(True)

    def recovery_sequence(self, slot_cache):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp)/'run';run.mkdir()
            debug=Path(tmp)/'debug';debug.mkdir()
            trace=Path(tmp)/'trace';trace.mkdir();(trace/'tracing_on').write_text('0\n')
            cg=Path(tmp)/'cg';cg.mkdir();(cg/'cgroup.procs').write_text('')
            (debug/'stats').write_text('instance_ns=123\n')
            (debug/'identity_status').write_text('bound=0\n')
            (run/'baseline.stats').write_text('instance_ns=123\n')
            (run/'failure.txt').write_text('RuntimeError: Real sampler did not produce both hot and cold Store decisions')
            for name,obj in [('settings.before.json',{}),('bfs.cgroup.before.json',None),('swaps.before.json',[]),
                             ('controller.json',dict(pid=101,start_token=1001)),('controlled.target.json',dict(pid=102))]:
                (run/name).write_text(json.dumps(obj))
            inputs=[host.REPO/name for name in ('remoteswap/client/rswap-client.ko','remoteswap/client/rswap_client.conf')]
            (run/'artifacts.json').write_text(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}))
            (run/'recovery-prepared').mkdir()
            for name in ('pattern','capacity-pattern','sampler-audit'):
                (run/name).write_text('old')
                (run/'recovery-prepared'/name).write_text('fixed')
            (run/'recovery-artifacts.json').write_text('{}')
            (run/'slot-cache-artifacts.json').write_text('{}')
            (run/'recovery.failure.txt').write_text('RuntimeError: Mappings remain after owned workload exit')
            records={
                101:dict(pid=101,ppid=1,start_token=1001,state='S',
                         argv=['/usr/bin/python3',str(Path(host.__file__).resolve()),str(run)]),
                102:dict(pid=102,ppid=101,start_token=1002,state='T',argv=[str(run/'pattern')]),
                103:dict(pid=103,ppid=101,start_token=1003,state='T',argv=[str(run/'sampler-audit'),
                    '--placement','--a-bit-window','5','--ewma-window','4','--out',str(run/'controlled.heat'),'--pid','102'])}
            if slot_cache:
                records={101:dict(records[101],argv=records[101]['argv']+['--recover-core-store'])}
            events=[]
            def send(fd,sig):
                pid=fd-9000;events.append((pid,sig))
                if sig==host.signal.SIGTERM: records[pid]['state']='Z'
            original_read=Path.read_text
            original_iter=Path.iterdir
            def read(path,*a,**kw):
                if str(path)=='/sys/module/rswap_client/version': return '1.19\n'
                return original_read(path,*a,**kw)
            def iterate(path):
                if str(path)=='/proc': return iter(Path('/proc')/str(p) for p in records)
                return original_iter(path)
            def run_remaining(*args):
                events.append('run-tests')
                for name in ('pattern','capacity-pattern','sampler-audit'): self.assertEqual((run/name).read_text(),'fixed')
            with ExitStack() as stack:
                for obj,name,value in [(host,'RUN',run),(host,'RS',debug),(host,'TRACE',trace),
                                        (host.previous,'BFS_CG',cg)]: stack.enter_context(patch.object(obj,name,value))
                stack.enter_context(patch.object(Path,'read_text',read))
                stack.enter_context(patch.object(Path,'iterdir',iterate))
                stack.enter_context(patch.object(host.os,'geteuid',return_value=0))
                stack.enter_context(patch.object(host.os,'pidfd_open',side_effect=lambda pid:pid+9000))
                stack.enter_context(patch.object(host.os,'close'))
                stack.enter_context(patch.object(host.signal,'pidfd_send_signal',side_effect=send))
                stack.enter_context(patch.object(host,'process_record',side_effect=lambda pid:records.get(pid)))
                stack.enter_context(patch.object(host.core,'swaps',return_value=[[host.previous.BIG_SWAP]]))
                stack.enter_context(patch.object(host.old,'process_identity',return_value=(1,999)))
                stack.enter_context(patch.object(host,'empty'))
                stack.enter_context(patch.object(host,'settle'))
                stack.enter_context(patch.object(host,'snapshot'))
                reset=stack.enter_context(patch.object(host,'reset_test_swap'))
                command=stack.enter_context(patch.object(host,'command'))
                remaining=stack.enter_context(patch.object(host,'run_tests',side_effect=run_remaining))
                host.recover_core_store(slot_cache=slot_cache)
                command.assert_not_called();remaining.assert_called_once_with({},None,[])
                reset.assert_called_once_with('slot-cache-recovery' if slot_cache else 'recovery-start')
            if not slot_cache:
                self.assertLess(events.index((102,host.signal.SIGTERM)),events.index((101,host.signal.SIGTERM)))
                self.assertLess(events.index((103,host.signal.SIGTERM)),events.index((101,host.signal.SIGTERM)))
            else:
                self.assertEqual({v[0] for v in events if isinstance(v,tuple)},{101})
            self.assertEqual(events[-1],'run-tests')
            archive='failed-slot-cache-check' if slot_cache else 'failed-core-clean-reuse'
            self.assertEqual((run/archive/'pattern').read_text(),'old')

    def test_stage_reset_drains_swap_before_empty_check_and_keeps_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            rs=Path(tmp);(rs/'identity_status').write_text('bound=0\n')
            (rs/'placement_raw').write_text('state=inactive\n')
            swaps=[[host.previous.BIG_SWAP]];events=[]
            def command(args,label,timeout=180):
                events.append(args[0]);swaps.clear()
                if args[0]=='swapon': swaps.append([host.previous.BIG_SWAP])
            def empty():
                events.append('empty')
            with patch.object(host,'RS',rs),patch.object(host,'OPEN_FDS',set()), \
                 patch.object(host.old,'JOBS',[]),patch.object(host.core,'swaps',side_effect=lambda:list(swaps)), \
                 patch.object(host,'snapshot'),patch.object(host,'state'), \
                 patch.object(host,'command',side_effect=command),patch.object(host,'empty',side_effect=empty):
                host.reset_test_swap('test')
            self.assertEqual(events,['swapoff','empty','swapon','empty'])

    def test_stage_reset_refuses_active_job_or_open_policy(self):
        for fds,jobs in [({42},[]),(set(),[SimpleNamespace(poll=lambda:None)])]:
            with self.subTest(fds=fds),patch.object(host,'OPEN_FDS',fds), \
                 patch.object(host.old,'JOBS',jobs),patch.object(host,'command') as command:
                with self.assertRaises(RuntimeError): host.reset_test_swap('test')
                command.assert_not_called()

    def test_same_tier_slot_release_flushes_one_cpu_batch_without_freeing_preferred_dram(self):
        rows=[dict(tier=0,server=1,vpn=100)]+[dict(tier=1,server=i%2,vpn=200+i) for i in range(64)]
        pending=[];released=[];commands=[]
        pools={(0,'dram'):{'free':0},(1,'dram'):{'free':0}}
        def send(command):
            commands.append(command)
            page=int(command.split()[1])
            if len(pending)>=64:
                released.extend(pending)
                if 0 in pending: pools[1,'dram']['free']+=1
                pending.clear()
            pending.append(page)
        work=SimpleNamespace(base=100,pages=1000,job=SimpleNamespace(pid=77),send=send)
        with patch.object(host.os,'sched_getaffinity',return_value={2,3}), \
             patch.object(host.os,'sched_setaffinity') as pin, \
             patch.object(host,'collect'),patch.object(host,'parse_bundle',return_value={'pools':pools}):
            index,other=host.release_capacity_slot(work,rows,0)
        pin.assert_called_once_with(77,{2})
        self.assertEqual(index,0)
        self.assertEqual(len(other),64)
        self.assertEqual(len(commands),65)
        self.assertIn(0,released)
        self.assertEqual(len(pending),1)
        self.assertEqual(pools[0,'dram']['free'],0)

    def test_policy_audit_bind_against_real_identity_receiver_and_policy_lease(self):
        audit=(ROOT/'tests/stage2_sampler_audit.c').read_text()
        for name in ('write','pread','dup','close'):
            audit=re.sub(r'\b'+name+r'\(', 'fixture_'+name+'(', audit)
        audit=audit.replace('int main(int argc,char **argv)','int audit_cli_main(int argc,char **argv)')
        audit=audit.replace('raise(SIGSTOP);','abort();')
        declarations='''
static ssize_t fixture_write(int,const void *,size_t);
static ssize_t fixture_pread(int,void *,size_t,off_t);
static int fixture_dup(int);
static int fixture_close(int);
'''
        mapping=mapping_harness.STUBS.replace('#define kvfree free','')
        mapping=mapping.replace('return calloc(n, size);','void *p=calloc(n,size); if(p) live_allocations++; return p;')
        cases=CASES.replace('rswap_vpn_heat_area(true);','''rswap_vpn_heat_area(true);
            const struct rswap_pool_layout layout={.nr_servers=2,.total_pages=8,
                .server_pages={4,4},.tier_pages={{2,2},{2,2}}};
            rswap_placement_init(&layout);rswap_placement_area(true);''')
        cases=cases.replace('.version=1,.op=1,.binding_id=id','.version=1,.op=6,.binding_id=id')
        code=placement_harness.STUBS+SUPPORT+mapping+EXTRA+production()+declarations+audit+cases
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'audit.c';source.write_text(code);binary=source.with_suffix('')
            subprocess.run(['gcc','-O1','-g','-std=gnu11','-Wall','-Wextra','-Werror','-pthread',
                            '-Wno-unused-function','-Wno-unused-parameter','-Wno-unused-but-set-variable',
                            '-Wno-misleading-indentation','-fsanitize=undefined',
                            '-I',str(ROOT/'client'),'-I',str(ROOT/'tools'),'-I',str(ROOT/'tests'),
                            str(source),'-o',str(binary)],check=True)
            run=subprocess.run([str(binary)],text=True,capture_output=True,check=True,timeout=15)
            row=json.loads(next(l[6:] for l in run.stdout.splitlines() if l.startswith('AUDIT ')))
            self.assertEqual((row['hot_pages'],row['cold_pages'],row['zero_pages']),(2250,2250,0))
            self.assertIn('"unknown":0',run.stdout)
            self.assertIn('LIFECYCLE closed_identity_rejected=1',run.stdout)
            self.assertIn('AUDIT_TARGET_EXIT unfinished_observation_discarded=1',run.stdout)

    def test_target_signal_rejects_pid_reuse_and_wrong_executable(self):
        for token,exe in ((123,host.previous.BFS),(456,Path('/bin/other'))):
            with self.subTest(token=token), patch.object(host.os,'pidfd_open',return_value=99), \
                 patch.object(host.os,'close') as close, patch.object(host.old,'process_identity',return_value=(1,token)), \
                 patch.object(Path,'resolve',return_value=exe), \
                 patch.object(host.signal,'pidfd_send_signal') as send:
                # Resolve the expected binary separately for the wrong-exe case.
                if token==456:
                    with patch.object(Path,'resolve',side_effect=[exe,host.previous.BFS]):
                        with self.assertRaises(RuntimeError): host.signal_target(777,456,host.signal.SIGTERM)
                else:
                    with self.assertRaises(RuntimeError): host.signal_target(777,456,host.signal.SIGTERM)
                send.assert_not_called();close.assert_called_once_with(99)


if __name__=='__main__': unittest.main()

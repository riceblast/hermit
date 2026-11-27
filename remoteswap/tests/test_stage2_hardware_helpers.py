"""Review the new operator tools before requesting privileged hardware runs."""
from pathlib import Path
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest

import placement_harness
from test_stage1_identity import SUPPORT
from test_stage2_publish import EXTRA

ROOT = Path(__file__).resolve().parents[1]

CASES = r'''
static struct file id_file, heat_file;
static bool heat_closed;
static bool exit_on_commit;
static ssize_t fixture_write(int fd,const void *p,size_t n) {
    loff_t pos=0; ssize_t ret;
    if(fd==100 || fd==200) ret=vpn_write(&heat_file,p,n,&pos);
    else if(fd==101) ret=rswap_identity_write(&id_file,p,n,&pos);
    else return write(fd,p,n);
    if(ret<0) { errno=-ret; return -1; }
    if(fd==100 && exit_on_commit && n>=sizeof(struct rswap_vpn_heat_command)) {
        struct rswap_vpn_heat_command c;memcpy(&c,p,sizeof(c));
        if(c.op==RSWAP_VPN_HEAT_COMMIT) processes[1].task->mm=NULL;
    }
    return ret;
}
static ssize_t fixture_pread(int fd,void *p,size_t n,off_t offset) {
    loff_t pos=offset; ssize_t ret;
    if(fd==100 || fd==200) ret=vpn_read(&heat_file,p,n,&pos);
    else return pread(fd,p,n,offset);
    if(ret<0) { errno=-ret; return -1; } return ret;
}
static int fixture_dup(int fd) { assert(fd==100); return 200; }
static int fixture_close(int fd) {
    if(fd==100) { heat_closed=true; return 0; }
    if(fd==200) { assert(heat_closed); return vpn_release(NULL,&heat_file); }
    if(fd==101) return rswap_identity_release(NULL,&id_file);
    return close(fd);
}
int main(void) {
    static struct mm_struct mm={1,1}; static struct task_struct task={&mm,0};
    processes[1].task=&task; assert(!rswap_identity_init(100000));
    rswap_identity_area(true,2); rswap_vpn_heat_area(true);
    assert(!rswap_identity_open(NULL,&id_file)); assert(!vpn_open(NULL,&heat_file));
    assert(audited_write(101,"bind 1",6)==6);
    u64 id=((struct rswap_identity_context *)id_file.private_data)->id;
    struct rswap_vpn_heat_command c={.version=1,.op=1,.binding_id=id};
    assert(!command_write(100,&c,NULL));
    watch_count=3; watch_vpn[0]=10; watch_vpn[1]=20; watch_vpn[2]=999999;
    range_begin=10; range_count=2;
    /* More than two batches, current population distinct from retained history. */
    struct page_list p={0}; struct totals t;
    for(uint64_t i=0;i<9001;i++) assert(!append_page(&p,(struct hot_page){.vpn=i,.heat=i,.flags=i%2 ? IN_SCOPE : 0}));
    assert(!count_pages(&p,&t));
    struct publisher pub={.heat_fd=100,.binding=id};
    /* Call production sender commands through our audit hook. */
    c.op=2; c.count=p.n; c.pages=t.pages; c.sum_heat=t.sum;
    assert(!command_write(100,&c,NULL));
    struct rswap_vpn_heat_record batch[4096];
    c.op=3; c.pages=c.sum_heat=0;
    for(size_t i=0;i<p.n;) {
        size_t n=p.n-i; if(n>4096) n=4096;
        for(size_t j=0;j<n;j++) batch[j]=(struct rswap_vpn_heat_record){.vpn=i+j,.heat=i+j,.in_scope=(i+j)%2};
        c.count=n; assert(!command_write(100,&c,batch)); i+=n;
    }
    c.op=4; c.count=0; assert(!command_write(100,&c,NULL));
    assert(!transaction && seen==9001 && current_pages==4500 && range_records==2 && range_pages==1);
    assert(selected_count==32 && watched_count==2 && heat_hold==200 && identity_owner==101);
    /* The real receiver can accept COMMIT before exit invalidates queries.
     * Model an ended/replaced observer PID; this must discard the audit and
     * return ESTALE to the CLI, rather than SIGSTOP or claim another success. */
    target_pid=getpid(); assert(identity(target_pid,&target_token)==1); target_token++;
    c=(struct rswap_vpn_heat_command){.version=1,.op=2,.binding_id=id,.window=1};
    assert(!command_write(100,&c,NULL));
    exit_on_commit=true;c.op=4;
    assert(command_write(100,&c,NULL)==-1 && errno==ESTALE);
    task.mm=&mm;
    assert(!audited_close(100)); assert(!audited_close(101));
    assert(heat_hold==-1 && !rswap_identity_binding_valid(id));
    free(p.v); rswap_vpn_heat_area(false); rswap_identity_exit(); assert(!live_allocations);
    (void)pub;
    return 0;
}
'''


class HelperTests(unittest.TestCase):
    def test_actual_audit_hooks_against_real_kernel_receiver(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            production = ''
            for name in ('rswap_identity.c', 'rswap_vpn_heat.c'):
                production += '\n'.join(l for l in (ROOT/'client'/name).read_text().splitlines()
                                        if not l.startswith('#include')) + '\n'
            audit = (ROOT/'tests/stage2_sampler_audit.c').read_text()
            # Adapt only system calls made by the observer, not the included
            # production CLI. Fixtures still execute the real Client receiver.
            for name in ('write', 'pread', 'dup', 'close'):
                audit = re.sub(r'\b' + name + r'\(', 'fixture_' + name + '(', audit)
            audit = audit.replace('int main(int argc,char **argv)', 'int audit_cli_main(int argc,char **argv)')
            audit = audit.replace('raise(SIGSTOP);', 'abort();')
            declarations = '''
static ssize_t fixture_write(int,const void *,size_t);
static ssize_t fixture_pread(int,void *,size_t,off_t);
static int fixture_dup(int);
static int fixture_close(int);
'''
            source = path/'audit.c'
            source.write_text(placement_harness.STUBS + SUPPORT + EXTRA + production + declarations + audit + CASES)
            binary = path/'audit'
            subprocess.run(['gcc','-O1','-g','-std=gnu11','-Wall','-Wextra','-Werror',
                            '-Wno-unused-function','-Wno-unused-parameter','-Wno-unused-but-set-variable',
                            '-Wno-misleading-indentation','-fsanitize=undefined','-pthread',
                            '-I',str(ROOT/'client'),'-I',str(ROOT/'tools'),'-I',str(ROOT/'tests'),
                            str(source),'-o',str(binary)],check=True)
            run = subprocess.run([str(binary)],check=True,capture_output=True,text=True,timeout=10)
            lines = run.stdout.splitlines()
            audit_row = json.loads(next(l[6:] for l in lines if l.startswith('AUDIT ')))
            self.assertEqual((audit_row['records'],audit_row['pages'],audit_row['unknown']),(9001,4500,0))
            self.assertEqual(audit_row['checked_queries'],34)
            self.assertIn('LIFECYCLE closed_identity_rejected=1',lines)
            self.assertTrue(any(l.startswith('MISSING_ZERO ') for l in lines))
            self.assertIn('AUDIT_TARGET_EXIT unfinished_observation_discarded=1',lines)
            self.assertEqual(sum(l.startswith('AUDIT ') for l in lines),1)

    def test_pattern_dontneed_refault_and_full_content_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp)/'pattern'
            subprocess.run(['gcc','-O2','-std=gnu11','-Wall','-Wextra','-Werror','-pthread',
                            str(ROOT/'tests/stage2_pattern.c'),'-o',str(binary)],check=True)
            job = subprocess.Popen([str(binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                ready=json.loads(job.stdout.readline())
                self.assertEqual(ready['pages'],8192)
                for command in ('discard','refault','finish'):
                    job.stdin.write(command+'\n');job.stdin.flush()
                    self.assertEqual(json.loads(job.stdout.readline()),dict(event='done',command=command))
                self.assertEqual(job.wait(timeout=10),0)
                self.assertEqual(job.stderr.read(),'')
            finally:
                if job.poll() is None:
                    job.kill();job.wait()
                job.stdin.close();job.stdout.close();job.stderr.close()

    def test_controller_ignores_only_partial_final_audit_line(self):
        import stage2_host as controller
        with tempfile.TemporaryDirectory() as tmp:
            sampler=controller.Sampler.__new__(controller.Sampler)
            sampler.stdout=Path(tmp)/'console'
            sampler.stdout.write_text('AUDIT {"window":0}\nAUDIT {"window":')
            self.assertEqual(sampler.records('AUDIT'),[dict(window=0)])
            sampler.stdout.write_text('AUDIT broken\nAUDIT {"window":1}\n')
            with self.assertRaisesRegex(RuntimeError,'Malformed'):
                sampler.records('AUDIT')

    def test_controller_accepts_exiting_flag_before_zombie(self):
        from unittest.mock import patch
        import stage2_host as controller
        for state,flags,expected in [('R',0,False),('T',0,False),('R',4,True),('Z',0,True),('X',0,True)]:
            fields=[state]+['0']*20
            fields[6]=str(flags)
            text='77 (BFS ) cleanup) '+' '.join(fields)
            with self.subTest(state=state,flags=flags), patch.object(Path,'read_text',return_value=text):
                self.assertEqual(controller.target_finished(77),expected)
        with patch.object(Path,'read_text',side_effect=FileNotFoundError):
            self.assertTrue(controller.target_finished(77))


if __name__=='__main__':
    unittest.main()

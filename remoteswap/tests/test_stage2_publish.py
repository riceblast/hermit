"""Real sampler -> real VPN snapshot/identity code with userspace RCU/VM fixtures."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import placement_harness
from test_stage1_identity import SUPPORT

ROOT = Path(__file__).resolve().parents[1]

EXTRA = r'''
#include <unistd.h>
#define U32_MAX UINT32_MAX
#define RCU_INIT_POINTER(p,v) rcu_assign_pointer(p,v)
#include "rswap_vpn_heat.h"
'''
BRIDGE = r'''
static ssize_t sampler_write(int,const void *,size_t);
static ssize_t sampler_pread(int,void *,size_t,off_t);
#define write sampler_write
#define pread sampler_pread
#define main sampler_program_main
#include "rswap_hotness.c"
#undef main
#undef write
#undef pread
static struct mm_struct mm={1,1}, replacement={1,1};
static struct task_struct task={&mm,0};
static struct file id_fd, heat_fd;
static uint64_t binding;
static struct publisher pub;
static ssize_t sampler_write(int fd,const void *p,size_t n) {
    loff_t pos=0; ssize_t ret;
    if(fd==100) ret=vpn_write(&heat_fd,p,n,&pos);
    else if(fd==101) ret=rswap_identity_write(&id_fd,p,n,&pos);
    else return write(fd,p,n);
    if(ret<0) { errno=-ret; return -1; } return ret;
}
static ssize_t sampler_pread(int fd,void *p,size_t n,off_t offset) {
    loff_t pos=offset; ssize_t ret;
    if(fd==100) ret=vpn_read(&heat_fd,p,n,&pos);
    else if(fd==101) ret=rswap_identity_read(&id_fd,p,n,&pos);
    else return pread(fd,p,n,offset);
    if(ret<0) { errno=-ret; return -1; } return ret;
}
static ssize_t packet(struct file *f,unsigned op,uint64_t win,uint64_t count,
                      uint64_t pages,uint64_t sum,const struct rswap_vpn_heat_record *rows) {
    char buffer[sizeof(struct rswap_vpn_heat_command)+RSWAP_VPN_HEAT_BATCH*sizeof(*rows)];
    struct rswap_vpn_heat_command cmd={.version=1,.op=op,.binding_id=binding,
        .window=win,.count=count,.pages=pages,.sum_heat=sum};
    size_t bytes=op==RSWAP_VPN_HEAT_DATA ? count*sizeof(*rows) : 0;
    assert(bytes<=sizeof(buffer)-sizeof(cmd)); memcpy(buffer,&cmd,sizeof(cmd));
    if(bytes) memcpy(buffer+sizeof(cmd),rows,bytes);
    loff_t pos=0; return vpn_write(f,buffer,sizeof(cmd)+bytes,&pos);
}
static void setup(void) {
    processes[1].task=&task; assert(!rswap_identity_init(1ULL<<30));
    rswap_identity_area(true,2); rswap_vpn_heat_area(true);
    assert(!rswap_identity_open(NULL,&id_fd));
    loff_t pos=0; assert(rswap_identity_write(&id_fd,"bind 1",6,&pos)==6);
    binding=((struct rswap_identity_context *)id_fd.private_data)->id;
    assert(rswap_identity_binding_valid(binding));
    assert(!vpn_open(NULL,&heat_fd)); assert(packet(&heat_fd,1,0,0,0,0,NULL)>0);
    pub=(struct publisher){101,100,binding}; assert(!publisher_current(&pub));
}
static void cleanup(void) {
    vpn_release(NULL,&heat_fd); rswap_identity_release(NULL,&id_fd); rswap_vpn_heat_area(false);
    rswap_identity_exit(); assert(!live_allocations && !task.refs && !processes[1].refs && mm.count==1);
}
static void publish(uint64_t window) {
    struct hot_page rows[]={ {.vpn=1,.heat=RSWAP_VPN_HEAT_SCALE/2,.flags=IN_SCOPE},
        {.vpn=2,.heat=RSWAP_VPN_HEAT_SCALE,.flags=IN_SCOPE},
        {.vpn=9,.heat=RSWAP_VPN_HEAT_SCALE/4} };
    struct page_list p={.v=rows,.n=3}; struct totals t;
    assert(!count_pages(&p,&t)); assert(!publish_snapshot(&pub,&p,window,&t));
}
static void expect_snapshot(uint64_t win) {
    struct rswap_vpn_heat_ack a;
    assert(!rswap_vpn_heat_lookup(binding,2,&a) && a.has_snapshot && a.window==win && a.found);
    assert(a.records==3 && a.pages==2 && a.sum_heat==3*RSWAP_VPN_HEAT_SCALE/2 && a.heat==RSWAP_VPN_HEAT_SCALE);
}
static void bridge_case(void) {
    setup(); publish(0); expect_snapshot(0);
    struct rswap_vpn_heat_ack a;
    assert(!rswap_vpn_heat_lookup(binding,9,&a) && a.found && a.heat==RSWAP_VPN_HEAT_SCALE/4);
    assert(!rswap_vpn_heat_lookup(binding,99,&a) && !a.found && !a.heat);
    assert(rswap_vpn_heat_lookup(binding+1,2,&a)==-ESTALE && !a.found);
    assert(packet(&heat_fd,2,0,0,0,0,NULL)==-ESTALE); expect_snapshot(0);
    /* Transaction staging cannot leak into a reader's previous snapshot. */
    assert(packet(&heat_fd,2,1,3,2,3*RSWAP_VPN_HEAT_SCALE/2,NULL)>0); expect_snapshot(0);
    struct rswap_vpn_heat_record one={.vpn=1,.heat=RSWAP_VPN_HEAT_SCALE/2,.in_scope=1};
    assert(packet(&heat_fd,3,1,1,0,0,&one)>0); expect_snapshot(0);
    assert(packet(&heat_fd,4,1,0,0,0,NULL)==-EINVAL); expect_snapshot(0);
    publish(1); expect_snapshot(1);
    struct rswap_vpn_heat_command q={.version=1,.op=5,.binding_id=binding,.vpn=1};
    assert(!command_write(100,&q,NULL));
    assert(sampler_pread(100,&a,sizeof(a),0)==sizeof(a) && a.heat==one.heat);
    cleanup();
}
static void malformed_case(void) {
    setup(); publish(0);
    struct rswap_vpn_heat_record rows[2]={{.vpn=2,.heat=1,.in_scope=1},{.vpn=1,.heat=1,.in_scope=1}};
    assert(packet(&heat_fd,2,1,2,2,2,NULL)>0);
    assert(packet(&heat_fd,3,1,2,0,0,rows)==-EINVAL); expect_snapshot(0);
    rows[1].vpn=2; assert(packet(&heat_fd,2,1,2,2,2,NULL)>0);
    assert(packet(&heat_fd,3,1,2,0,0,rows)==-EINVAL); expect_snapshot(0);
    rows[1].vpn=3; rows[1].reserved=1; assert(packet(&heat_fd,2,1,2,2,2,NULL)>0);
    assert(packet(&heat_fd,3,1,2,0,0,rows)==-EINVAL); expect_snapshot(0);
    rows[1].reserved=0; rows[1].heat=RSWAP_VPN_HEAT_SCALE+1;
    assert(packet(&heat_fd,2,1,2,2,RSWAP_VPN_HEAT_SCALE+1,NULL)>0);
    assert(packet(&heat_fd,3,1,2,0,0,rows)==-EINVAL); expect_snapshot(0);
    assert(packet(&heat_fd,2,1,2,2,2,NULL)>0);
    loff_t pos=0; assert(vpn_write(&heat_fd,"x",1,&pos)==-EINVAL);
    assert(!((struct vpn_uploader *)heat_fd.private_data)->staging); expect_snapshot(0);
    allocation_fail=0; assert(packet(&heat_fd,2,1,0,0,0,NULL)==-ENOMEM); allocation_fail=-1;
    expect_snapshot(0);
    copy_fault=true; assert(packet(&heat_fd,2,1,0,0,0,NULL)==-EFAULT); copy_fault=false;
    privileged=false; assert(packet(&heat_fd,2,1,0,0,0,NULL)==-EPERM); privileged=true;
    assert(packet(&heat_fd,2,1,UINT64_MAX,0,0,NULL)==-EINVAL);
    publish(1); expect_snapshot(1); cleanup();
}
static void lifecycle_case(void) {
    setup(); publish(0);
    task.mm=&replacement;
    struct rswap_vpn_heat_ack a;
    assert(!rswap_identity_binding_valid(binding) && publisher_current(&pub)<0 && errno==ESTALE);
    assert(rswap_vpn_heat_lookup(binding,1,&a)==-ESTALE);
    assert(packet(&heat_fd,2,1,0,0,0,NULL)==-ESTALE);
    rswap_identity_release(NULL,&id_fd);
    assert(!rswap_identity_open(NULL,&id_fd)); loff_t pos=0;
    assert(rswap_identity_write(&id_fd,"bind 1",6,&pos)==6);
    uint64_t old=binding; binding=((struct rswap_identity_context *)id_fd.private_data)->id; assert(old!=binding);
    struct file fresh; assert(!vpn_open(NULL,&fresh)); assert(packet(&fresh,1,0,0,0,0,NULL)>0);
    /* Closing an old heat fd cannot revoke the replacement binding. */
    vpn_release(NULL,&heat_fd); heat_fd=fresh; pub.binding=binding;
    publish(0); expect_snapshot(0);
    struct file other; assert(!vpn_open(NULL,&other)); assert(packet(&other,1,0,0,0,0,NULL)==-EBUSY);
    vpn_release(NULL,&other);
    rswap_vpn_heat_area(false); assert(rswap_vpn_heat_lookup(binding,1,&a)==-ESTALE);
    rswap_vpn_heat_area(true); assert(packet(&heat_fd,1,0,0,0,0,NULL)==-ESTALE);
    cleanup(); task.mm=&mm; assert(replacement.count==1);
}
static void batches_case(void) {
    setup(); struct page_list p={0}; struct totals t;
    for(uint64_t i=0;i<9001;i++) assert(!append_page(&p,(struct hot_page){.vpn=i,.heat=i,.flags=i%2 ? IN_SCOPE : 0}));
    assert(!count_pages(&p,&t)); assert(!publish_snapshot(&pub,&p,0,&t));
    struct rswap_vpn_heat_ack a; assert(!rswap_vpn_heat_lookup(binding,9000,&a));
    assert(a.has_snapshot && a.records==9001 && a.pages==4500 && a.heat==9000);
    free(p.v);
    /* Zero histories/population is a valid completed snapshot, not unknown. */
    p=(struct page_list){0}; assert(!count_pages(&p,&t)); assert(!publish_snapshot(&pub,&p,1,&t));
    assert(!rswap_vpn_heat_lookup(binding,1,&a) && a.has_snapshot && !a.pages && !a.found);
    cleanup();
}
static atomic_bool stop_reader;
static void *reader(void *unused) {
    while(!atomic_load(&stop_reader)) {
        struct rswap_vpn_heat_ack a;
        assert(!rswap_vpn_heat_lookup(binding,1,&a));
        if(a.has_snapshot) assert(a.records==3 && a.pages==2 && a.heat==RSWAP_VPN_HEAT_SCALE/2 && a.sum_heat==3*RSWAP_VPN_HEAT_SCALE/2);
    }
    return NULL;
}
static void concurrency_case(void) {
    setup(); pthread_t thread; assert(!pthread_create(&thread,NULL,reader,NULL));
    for(uint64_t w=0;w<500;w++) publish(w);
    atomic_store(&stop_reader,true); pthread_join(thread,NULL); expect_snapshot(499); cleanup();
}
int main(int argc,char **argv) {
    assert(argc==2);
    switch(atoi(argv[1])) { case 0:bridge_case();break;case 1:malformed_case();break;
    case 2:lifecycle_case();break;case 3:batches_case();break;case 4:concurrency_case();break;default:abort(); }
    return 0;
}
'''


class PublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hermit-stage2-publish-')
        path = Path(cls.tmp.name)
        production = ''
        for name in ('rswap_identity.c', 'rswap_vpn_heat.c'):
            source = (ROOT / 'client' / name).read_text()
            production += '\n'.join(line for line in source.splitlines() if not line.startswith('#include')) + '\n'
        source = path / 'publish.c'
        source.write_text(placement_harness.STUBS + SUPPORT + EXTRA + production + BRIDGE)
        cls.binary = path / 'publish'
        subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-unused-but-set-variable',
                        '-Wno-misleading-indentation', '-fsanitize=undefined', '-I', str(ROOT/'client'),
                        '-I', str(ROOT/'tools'), str(source), '-pthread', '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self, case):
        subprocess.run([str(self.binary), str(case)], check=True, timeout=20)

    def test_real_producer_protocol_atomic_publish_and_queries(self):
        self.run_case(0)

    def test_bad_packets_partial_commit_allocation_copy_and_permission_errors(self):
        self.run_case(1)

    def test_exec_rebind_stale_fd_and_swapoff(self):
        self.run_case(2)

    def test_multiple_data_batches_and_empty_snapshot(self):
        self.run_case(3)

    def test_concurrent_snapshot_read_and_replace(self):
        self.run_case(4)


if __name__ == '__main__':
    unittest.main()

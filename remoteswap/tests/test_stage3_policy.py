"""Real identity, VPN receiver/policy, legacy ownership and Slot allocator."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import placement_harness
import mapping_harness
from test_stage1_identity import SUPPORT

ROOT = Path(__file__).resolve().parents[1]

EXTRA = r'''
#include <unistd.h>
#include <fcntl.h>
#define RCU_INIT_POINTER(p,v) rcu_assign_pointer(p,v)
#define spin_lock_irqsave(p,f) do { (f)=0; pthread_mutex_lock(p); } while(0)
#define spin_unlock_irqrestore(p,f) do { (void)(f); pthread_mutex_unlock(p); } while(0)
#define WARN_ON(x) ({ bool bad=!!(x); assert(!bad); bad; })
#include "rswap_placement.h"
#include "rswap_vpn_heat.h"
static void mock_mapping_trace(u64 seq,unsigned int action,u64 offset,unsigned int s,
    unsigned int t,u64 slot,unsigned int users,unsigned int live,unsigned int fallback) {}
'''
CASES = r'''
#define Q RSWAP_VPN_HEAT_SCALE
static struct mm_struct mm={1,1},other_mm={1,1};
static struct task_struct task={&mm,0},other_task={&other_mm,0};
static struct file id_file,heat_file;
static u64 binding;
static struct rswap_pool_layout layout={.nr_servers=2,.total_pages=8,
    .server_pages={4,4},.tier_pages={{2,2},{2,2}}};
static ssize_t packet(struct file *f,unsigned op,u64 window,u64 count,u64 pages,u64 sum,
                      const struct rswap_vpn_heat_record *rows) {
    char buffer[sizeof(struct rswap_vpn_heat_command)+RSWAP_VPN_HEAT_BATCH*sizeof(*rows)];
    struct rswap_vpn_heat_command c={.version=1,.op=op,.binding_id=binding,
        .window=window,.count=count,.pages=pages,.sum_heat=sum};
    size_t bytes=op==3 ? count*sizeof(*rows) : 0; assert(bytes<=sizeof(buffer)-sizeof(c));
    memcpy(buffer,&c,sizeof(c));if(bytes) memcpy(buffer+sizeof(c),rows,bytes);
    loff_t pos=0;return vpn_write(f,buffer,sizeof(c)+bytes,&pos);
}
static void kernel_setup(void) {
    processes[1].task=&task; processes[2].task=&other_task;
    assert(!rswap_mapping_init(&layout));
    assert(!rswap_identity_init(layout.total_pages));
    rswap_identity_area(true,2);rswap_vpn_heat_area(true);rswap_placement_init(&layout);rswap_placement_area(true);
    rswap_policy_stats_init(1);
}
static void setup(bool policy) {
    kernel_setup();
    assert(!rswap_identity_open(NULL,&id_file));loff_t pos=0;
    assert(rswap_identity_write(&id_file,"bind 1",6,&pos)==6);
    binding=((struct rswap_identity_context *)id_file.private_data)->id;
    assert(!vpn_open(NULL,&heat_file));assert(packet(&heat_file,policy?6:1,0,0,0,0,NULL)>0);
}
static void cleanup(void) {
    vpn_release(NULL,&heat_file);rswap_identity_release(NULL,&id_file);
    rswap_vpn_heat_area(false);rswap_identity_exit();rswap_placement_exit();
    rswap_mapping_reset();rswap_mapping_destroy();
    assert(!live_allocations && mm.count==1 && other_mm.count==1 && !task.refs && !processes[1].refs);
}
static void bind_entry(u64 offset,u64 vpn) { bind_probe(NULL,&mm,vpn<<12,2,offset); }
static void publish(u64 window,const struct rswap_vpn_heat_record *rows,size_t n) {
    u64 pages=0,sum=0;
    for(size_t i=0;i<n;i++) if(rows[i].in_scope) { pages++;sum+=rows[i].heat; }
    assert(packet(&heat_file,2,window,n,pages,sum,NULL)>0);
    if(n) assert(packet(&heat_file,3,window,n,0,0,rows)>0);
    assert(packet(&heat_file,4,window,0,0,0,NULL)>0);
}
static void through(u64 window,const struct rswap_vpn_heat_record *rows,size_t n) {
    for(u64 i=0;i<=window;i++) publish(i,rows,n);
}
static struct rswap_decision choose(u64 offset) {
    struct rswap_decision d;assert(rswap_vpn_placement_choose(2,offset,&layout,&d)==1);
    return d;
}
static void warmup_case(void) {
    setup(true);bind_entry(1,100);bind_entry(1,200);
    struct rswap_vpn_heat_record rows[]={{100,Q/4,1,0},{200,3*Q/4,1,0},{300,Q,1,0}};
    struct rswap_decision d=choose(1);
    assert(d.state==RSWAP_WARMUP && d.tier==RSWAP_TIER_PMEM && !d.heat_q32 && !d.generation);
    assert(d.rr==0 && d.server==0);
    for(u64 w=0;w<5;w++) {
        publish(w,rows,3);atomic_store(&mock_now,1000000000000000ULL+w);
        d=choose(1);assert(d.state==RSWAP_WARMUP && d.tier==1 && d.generation==w);
        assert(d.heat_q32==3*Q/4 && d.vpn==200 && d.aliases==2 && d.pages==3 && d.sum_heat==2*Q);
    }
    assert(packet(&heat_file,2,5,3,3,2*Q,NULL)>0);
    d=choose(1);assert(d.state==RSWAP_WARMUP); /* staged group is not completed */
    assert(packet(&heat_file,3,5,3,0,0,rows)>0);
    assert(packet(&heat_file,4,5,0,0,0,NULL)>0);
    d=choose(1);assert(d.state==RSWAP_ADAPTIVE && d.reason==RSWAP_REASON_HOT && d.tier==0);
    assert(d.heat_q32==3*Q/4 && d.rr==0 && d.server==0);
    d=choose(1);assert(d.rr==1 && d.server==1);
    struct rswap_identity_result id;assert(!rswap_identity_lookup(2,1,&id) && id.vpn==100 && id.aliases==2);
    cleanup();
}
static void zeros_scope_case(void) {
    setup(true);bind_entry(1,100);
    struct rswap_vpn_heat_record row={300,Q,1,0};through(5,&row,1);
    struct rswap_decision d=choose(1);
    assert(d.reason==RSWAP_REASON_COLD && d.heat_q32==0 && d.aliases==1 && d.tier==1);
    row.heat=0;publish(6,&row,1);d=choose(1);
    assert(d.reason==RSWAP_REASON_HOT && !d.heat_q32 && !d.sum_heat && d.tier==0);
    publish(7,NULL,0);d=choose(1);
    assert(d.reason==RSWAP_REASON_HOT && !d.pages && !d.sum_heat);
    d=choose(2);u64 slot;unsigned s,t;assert(!rswap_map_page(&layout,2,&s,&t,&slot));
    assert(d.state==RSWAP_INACTIVE && !d.aliases && !d.rr && d.server==s && d.tier==t);
    bind_probe(NULL,&other_mm,100ULL<<12,2,3);d=choose(3);assert(!d.aliases && !d.state);
    assert(rswap_vpn_placement_choose(1,1,&layout,&d)==1 && !d.aliases && !d.state);
    assert(rswap_vpn_placement_choose(2,8,&layout,&d)==-ERANGE);
    cleanup();
}
static void precision_case(void) {
    setup(true);bind_entry(1,100);
    struct rswap_vpn_heat_record rows[]={{100,Q/2,1,0},{200,Q/2,1,0},{300,Q/2+1,1,0}};
    through(5,rows,3);struct rswap_decision d=choose(1);
    assert(d.reason==RSWAP_REASON_COLD && d.heat_q32*3<d.sum_heat && d.heat_q32==d.sum_heat/3);
    rows[2].heat=Q/2;publish(6,rows,3);d=choose(1);
    assert(d.reason==RSWAP_REASON_HOT && d.heat_q32*3==d.sum_heat);
    /* Retained/out-of-scope history still applies when the VPN reappears. */
    bind_entry(1,400);struct rswap_vpn_heat_record more[]={{100,Q/2,1,0},{200,Q/2,1,0},{400,Q,0,0}};
    publish(7,more,3);d=choose(1);assert(d.heat_q32==Q && d.vpn==400 && d.pages==2 && d.sum_heat==Q);
    cleanup();
}
static void transaction_case(void) {
    setup(true);bind_entry(1,100);struct rswap_vpn_heat_record row={100,Q/2,1,0};through(4,&row,1);
    assert(packet(&heat_file,2,6,1,1,Q/2,NULL)==-EINVAL);assert(choose(1).state==RSWAP_WARMUP);
    assert(packet(&heat_file,2,5,2,2,Q,NULL)>0);
    assert(packet(&heat_file,3,5,1,0,0,&row)>0);
    assert(packet(&heat_file,4,5,0,0,0,NULL)==-EINVAL);assert(choose(1).generation==4);
    allocation_fail=0;assert(packet(&heat_file,2,5,1,1,Q/2,NULL)==-ENOMEM);allocation_fail=-1;
    assert(choose(1).state==RSWAP_WARMUP);
    publish(5,&row,1);assert(choose(1).state==RSWAP_ADAPTIVE);
    cleanup();
}
static void observer_exclusion_case(void) {
    setup(false);bind_entry(1,100);struct rswap_vpn_heat_record row={100,Q,1,0};
    publish(10,&row,1);struct rswap_decision d;
    assert(rswap_vpn_placement_choose(2,1,&layout,&d)==0 && !vpn_policy_binding);
    assert(packet(&heat_file,6,0,0,0,0,NULL)==-ESTALE);
    cleanup();
    /* Opposite exclusion directions use the actual policy writer mutex. */
    setup(true);struct rswap_uploader legacy={0};struct rswap_upload_command cmd={.version=2,.op=1};
    mutex_lock(&policy_mutex);assert(rswap_upload_start(&legacy,&cmd)==-EBUSY);mutex_unlock(&policy_mutex);
    loff_t pos=0;assert(rswap_control_write(NULL,"stop",4,&pos)==4);
    assert(rswap_vpn_placement_choose(2,1,&layout,&d)==0 && !vpn_policy_binding);
    mutex_lock(&policy_mutex);assert(!rswap_upload_start(&legacy,&cmd));mutex_unlock(&policy_mutex);
    vpn_release(NULL,&heat_file);assert(!vpn_open(NULL,&heat_file));
    assert(packet(&heat_file,6,0,0,0,0,NULL)==-EBUSY);
    mutex_lock(&policy_mutex);rswap_policy_stop();mutex_unlock(&policy_mutex);
    assert(packet(&heat_file,6,0,0,0,0,NULL)>0);
    cleanup();
}
static void lifecycle_case(void) {
    setup(true);bind_entry(1,100);struct rswap_vpn_heat_record row={100,Q,1,0};through(5,&row,1);
    assert(choose(1).state==RSWAP_ADAPTIVE);task.mm=&other_mm;
    struct rswap_decision d=choose(1);assert(!d.state && !d.aliases);
    rswap_identity_release(NULL,&id_file);assert(!rswap_identity_open(NULL,&id_file));loff_t pos=0;
    assert(rswap_identity_write(&id_file,"bind 1",6,&pos)==6);
    u64 old_binding=binding;binding=((struct rswap_identity_context *)id_file.private_data)->id;
    struct file old_heat=heat_file;assert(!vpn_open(NULL,&heat_file));assert(packet(&heat_file,6,0,0,0,0,NULL)>0);
    assert(binding!=old_binding && vpn_policy_binding==binding);
    vpn_release(NULL,&old_heat);assert(vpn_policy_binding==binding);
    bind_probe(NULL,&other_mm,200ULL<<12,2,1);d=choose(1);assert(d.state==RSWAP_WARMUP && !d.heat_q32);
    rswap_identity_area(false,2);rswap_vpn_heat_area(false);rswap_placement_area(false);
    assert(!vpn_policy_binding && rswap_vpn_placement_choose(2,1,&layout,&d)==0);
    cleanup();task.mm=&mm;
}
static void capacity_case(void) {
    setup(true);struct rswap_location locs[8],read,again;
    for(u64 o=0;o<8;o++) {
        bind_entry(o,100+o);struct rswap_decision d=choose(o);
        assert(d.tier==1 && d.server==o%2);
        assert(!rswap_mapping_alloc_preferred(o,d.server,d.tier,&locs[o]));
        assert(locs[o].tier==(o<4 ? 1 : 0) && locs[o].server==o%2);
        rswap_mapping_put(locs[o].id);
    }
    assert(!rswap_mapping_get(0,&read));rswap_mapping_invalidate(0);
    struct rswap_decision d=choose(0);
    assert(rswap_mapping_alloc_preferred(0,d.server,d.tier,&again)==-ENOSPC);
    rswap_mapping_put(read.id);d=choose(0);
    assert(!rswap_mapping_alloc_preferred(0,d.server,d.tier,&again) && again.tier==1);
    rswap_mapping_put(again.id);
    struct rswap_vpn_heat_record row={100,Q,1,0};through(5,&row,1);
    assert(choose(0).tier==0);assert(!rswap_mapping_get(0,&read));
    assert(read.tier==1 && read.id==again.id);rswap_mapping_put(read.id); /* no migration */
    for(u64 o=0;o<8;o++) rswap_mapping_invalidate(o);
    cleanup();
}
static atomic_bool stop_reader;
static void *reader(void *unused) {
    while(!atomic_load(&stop_reader)) {
        struct rswap_decision d=choose(1);
        assert(d.state==RSWAP_ADAPTIVE);
        if(d.generation%2) assert(d.heat_q32==Q && d.sum_heat==2*Q && d.reason==RSWAP_REASON_HOT);
        else assert(d.heat_q32==Q/4 && d.sum_heat==5*Q/4 && d.reason==RSWAP_REASON_COLD);
        sched_yield();
    }
    return NULL;
}
static void concurrent_case(void) {
    setup(true);bind_entry(1,100);bind_entry(1,200);
    struct rswap_vpn_heat_record rows[]={{100,0,1,0},{200,Q,1,0},{300,Q,1,0}};through(5,rows,3);
    pthread_t thread;assert(!pthread_create(&thread,NULL,reader,NULL));
    for(u64 w=6;w<=200;w++) { rows[1].heat=w%2 ? Q : Q/4;publish(w,rows,3); }
    atomic_store(&stop_reader,true);pthread_join(thread,NULL);cleanup();
}

static void producer_case(void) {
    kernel_setup();
    struct publisher p={.identity_fd=-1,.heat_fd=-1};
    assert(!publisher_start_mode(&p,1,true));binding=p.binding;
    bind_entry(1,100);bind_entry(1,200);
    struct hot_page records[]={{.vpn=100,.heat=Q/4,.flags=IN_SCOPE},{.vpn=200,.heat=Q,.flags=IN_SCOPE}};
    struct page_list pages={.v=records,.n=2};struct totals t;assert(!count_pages(&pages,&t));
    for(u64 w=0;w<=5;w++) assert(!publish_snapshot(&p,&pages,w,&t));
    struct rswap_decision d=choose(1);assert(d.reason==RSWAP_REASON_HOT && d.vpn==200 && d.heat_q32==Q);
    assert(d.pages==2 && d.sum_heat==5*Q/4);cleanup();
    kernel_setup();p=(struct publisher){.identity_fd=-1,.heat_fd=-1};
    assert(!publisher_start_mode(&p,1,false));binding=p.binding;
    assert(!publish_snapshot(&p,&pages,20,&t));
    assert(rswap_vpn_placement_choose(2,1,&layout,&d)==0);cleanup();
}
int main(int argc,char **argv) {
    assert(argc==2);
    switch(atoi(argv[1])) { case 0:warmup_case();break;case 1:zeros_scope_case();break;
    case 2:precision_case();break;case 3:transaction_case();break;case 4:observer_exclusion_case();break;
    case 5:lifecycle_case();break;case 6:capacity_case();break;case 7:concurrent_case();break;case 8:producer_case();break;default:abort(); }
    return 0;
}
'''


def production():
    source=''
    for name in ('rswap_mapping.c','rswap_placement.c','rswap_identity.c','rswap_vpn_heat.c'):
        source+='\n'.join(l for l in (ROOT/'client'/name).read_text().splitlines() if not l.startswith('#include'))+'\n'
    return source


BRIDGE_INCLUDE = r'''
static ssize_t bridge_write(int,const void *,size_t);
static ssize_t bridge_read(int,void *,size_t,off_t);
static int bridge_open(const char *,int,...);
#define write bridge_write
#define pread bridge_read
#define open bridge_open
#define main sampler_program_main
#include "rswap_hotness.c"
#undef main
#undef open
#undef pread
#undef write
'''
BRIDGE_IMPL = r'''
static ssize_t bridge_write(int fd,const void *p,size_t n) {
    loff_t pos=0;ssize_t ret;
    if(fd==100) ret=vpn_write(&heat_file,p,n,&pos);
    else if(fd==101) ret=rswap_identity_write(&id_file,p,n,&pos);
    else return write(fd,p,n);
    if(ret<0) {errno=-ret;return -1;}return ret;
}
static ssize_t bridge_read(int fd,void *p,size_t n,off_t offset) {
    loff_t pos=offset;ssize_t ret;
    if(fd==100) ret=vpn_read(&heat_file,p,n,&pos);
    else if(fd==101) ret=rswap_identity_read(&id_file,p,n,&pos);
    else return pread(fd,p,n,offset);
    if(ret<0) {errno=-ret;return -1;}return ret;
}
static int bridge_open(const char *path,int flags,...) {
    if(strstr(path,"/rswap/identity")) {assert(!rswap_identity_open(NULL,&id_file));return 101;}
    if(strstr(path,"/rswap/vpn_heat")) {assert(!vpn_open(NULL,&heat_file));return 100;}
    errno=ENOENT;return -1;
}
'''

def source_code():
    mapping=mapping_harness.STUBS.replace('#define kvfree free','')
    mapping=mapping.replace('return calloc(n, size);','void *p=calloc(n,size); if(p) live_allocations++; return p;')
    return placement_harness.STUBS+SUPPORT+mapping+EXTRA+production()+BRIDGE_INCLUDE+CASES+BRIDGE_IMPL


class PolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='hermit-stage3-')
        source=Path(cls.tmp.name)/'policy.c';source.write_text(source_code())
        cls.binary=source.with_suffix('')
        subprocess.run(['gcc','-O1','-g','-std=gnu11','-Wall','-Wextra','-Werror','-pthread',
                        '-Wno-unused-function','-Wno-unused-parameter','-Wno-unused-but-set-variable',
                        '-Wno-misleading-indentation','-fsanitize=undefined','-I',str(ROOT/'client'),'-I',str(ROOT/'tools'),
                        str(source),'-o',str(cls.binary)],check=True)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def run_case(self,n): subprocess.run([str(self.binary),str(n)],check=True,timeout=25)
    def test_five_commits_warmup_max_alias_and_tier_round_robin(self): self.run_case(0)
    def test_missing_history_zero_equality_empty_mean_and_scope(self): self.run_case(1)
    def test_exact_rational_comparison_and_retained_history(self): self.run_case(2)
    def test_failed_or_skipped_window_does_not_advance_phase(self): self.run_case(3)
    def test_observation_mode_stop_and_legacy_exclusion(self): self.run_case(4)
    def test_exec_rebind_stale_fd_and_swapoff(self): self.run_case(5)
    def test_real_slot_capacity_fallback_retired_slot_and_no_migration(self): self.run_case(6)
    def test_concurrent_decisions_use_one_snapshot(self): self.run_case(7)
    def test_actual_sampler_policy_bind_publish_and_observe_only(self): self.run_case(8)


if __name__=='__main__': unittest.main()

"""Real VPN chooser -> real Store/Slot/QP path, with identity/RDMA fixtures."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_patch3_completion import client_code, definition, SUPPORT
from placement_harness import function

ROOT = Path(__file__).resolve().parents[1]

CASES = r'''
static void page_init(struct page *p) {
    *p=(struct page){0};atomic_store(&p->refs,1);atomic_store(&p->locked,1);
}
static struct fs_rdma_req *store(struct page *p,u64 offset) {
    void *cookie;page_init(p);assert(!rswap_store_prepare(0,offset,p,0,&cookie));
    struct fs_rdma_req *req=cookie;
    atomic_store(&p->valid,1);rswap_store_commit(cookie);
    return req;
}
int main(void) {
    init();rswap_mapping_destroy();trace_enabled=false;
    pool_layout=(struct rswap_pool_layout){.nr_servers=2,.total_pages=8,
        .server_pages={4,4},.tier_pages={{2,2},{2,2}}};
    assert(!rswap_mapping_init(&pool_layout));
    struct vpn_snapshot *snap=calloc(1,sizeof(*snap)+16*sizeof(snap->records[0]));assert(snap);
    snap->window=5;snap->publish_ns=1;snap->count=snap->pages=16;snap->sum_heat=8*RSWAP_VPN_HEAT_SCALE;
    for(unsigned i=0;i<8;i++) {
        snap->records[i]=(struct rswap_vpn_heat_record){100+2*i,0,1,0};
        snap->records[8+i]=(struct rswap_vpn_heat_record){200+2*i,RSWAP_VPN_HEAT_SCALE,1,0};
    }
    struct vpn_uploader u={.binding_id=1,.placement=true,.snapshot=snap};vpn_heat_owner=&u;
    struct page pages[8],extra,old_read={0},fresh,new_read={0};
    /* Max alias heat selects DRAM even though the lowest VPN is zero. */
    for(unsigned i=0;i<8;i++) {
        struct fs_rdma_req *req=store(&pages[i],i);
        assert(req->policy_reason==RSWAP_REASON_HOT);
        assert(req->tier==(i<4 ? RSWAP_TIER_DRAM : RSWAP_TIER_PMEM));
        assert(req->rdma_queue==&queues[i%2][QP_STORE]);
        complete_one(req->rdma_queue->cq);
    }
    assert(vpn_trace_count==8 && displayed_vpn==8 && identity_visits==8);
    assert(strstr(last_vpn_text,"hot(1.00 >= 0.50)") && strstr(last_vpn_text,"capacity_fallback"));
    assert(strstr(last_vpn_text,"target_mem: pmem"));
    /* Retired outstanding Read keeps the pool full; failed Store returns to
     * Frontswap/disk without an allocation or a display event. */
    page_init(&old_read);assert(!rswap_rdma_submit(0,0,&old_read,QP_LOAD_SYNC,NULL));
    rswap_invalidate_page(0,0);page_init(&extra);void *cookie;
    assert(rswap_store_prepare(0,0,&extra,0,&cookie)==-ENOSPC && !cookie);
    assert(vpn_trace_count==9 && displayed_vpn==8);
    complete_one(&cqs[0][QP_LOAD_SYNC]);
    /* New cold classification can use the released DRAM slot by capacity
     * fallback while retaining a cold reason. */
    snap->records[8].heat=0;snap->records[1].heat=RSWAP_VPN_HEAT_SCALE;
    struct fs_rdma_req *req=store(&fresh,0);
    assert(req->policy_reason==RSWAP_REASON_COLD && req->tier==RSWAP_TIER_DRAM);
    assert(strstr(last_vpn_text,"cold(0.00 < 0.50)") && strstr(last_vpn_text,"target_mem: dram"));
    complete_one(req->rdma_queue->cq);
    u.placement=false;
    page_init(&new_read);assert(!rswap_rdma_submit(0,4,&new_read,QP_LOAD_SYNC,NULL));
    assert(cqs[0][QP_LOAD_SYNC].queued->tier==RSWAP_TIER_PMEM);
    complete_one(&cqs[0][QP_LOAD_SYNC]);
    assert(displayed_vpn==9 && atomic_load(&new_read.uptodate));
    /* Fixed purePMem preference bypasses VPN lookup and its RR ticket. */
    rswap_invalidate_area(0);rswap_mapping_destroy();assert(!rswap_mapping_init(&pool_layout));
    area_active=true;u.placement=true;app_override=1;u.rr[0]=u.rr[1]=0;
    unsigned visits=identity_visits;
    req=store(&extra,0);assert(req->tier==RSWAP_TIER_PMEM && req->policy_reason==RSWAP_REASON_COUNT);
    complete_one(req->rdma_queue->cq);
    assert(identity_visits==visits && atomic64_read(&u.rr[0])==0 && atomic64_read(&u.rr[1])==0);
    rswap_invalidate_area(0);rswap_mapping_destroy();free(snap);
    return 0;
}
'''


class IOTests(unittest.TestCase):
    def test_real_vpn_store_fixed_precedence_capacity_and_load(self):
        source=(ROOT/'client/rswap_vpn_heat.c').read_text()
        code=client_code(SUPPORT.replace('int main(void)','int legacy_main(void)'))
        code=code.replace('#define rswap_vpn_placement_choose(...) 0','')
        code=code.replace('#define trace_vpn_decision(...) ((void)0)','')
        core=r'''
#include "../rswap_vpn_heat_uapi.h"
#define rcu_read_lock() ((void)0)
#define rcu_read_unlock() ((void)0)
#define rcu_dereference(p) (p)
#define atomic64_inc_return(p) (atomic_fetch_add(p,1)+1)
static u64 ktime_get_ns(void);
static unsigned identity_visits;
'''
        core+=definition((ROOT/'client/rswap_identity.h').read_text(),'rswap_identity_result',struct=True)
        core+=r'''
static bool rswap_identity_binding_valid(u64 id) {return id==1;}
static int rswap_identity_visit(unsigned int type,u64 offset,u64 id,void (*visit)(void *,u64),
                               void *arg,struct rswap_identity_result *result) {
    identity_visits++;visit(arg,100+2*offset);visit(arg,200+2*offset);
    *result=(struct rswap_identity_result){.binding_id=1,.vpn=100+2*offset,.aliases=2};return 0;
}
'''
        for name in ('vpn_snapshot','vpn_uploader','vpn_reduction'):
            core+=definition(source,name,struct=True).replace(' __rcu','')
        core+='static struct vpn_uploader *vpn_heat_owner;\n'
        for name in ('vpn_heat_at','vpn_max_alias','rswap_vpn_placement_choose'):
            core+=function(source,name)
        code=code.replace(definition(code,'rswap_placement_choose'),definition(code,'rswap_placement_choose')+core)
        formatter=(ROOT/'client/rswap_trace.h').read_text()
        display='#define scnprintf snprintf\n'+function(formatter,'rswap_format_decision')+function(formatter,'rswap_format_vpn_decision')
        display+=r'''
static unsigned vpn_trace_count,displayed_vpn;
static char last_vpn_text[256];
static void trace_vpn_decision(u64 offset,u64 pfn,const struct rswap_decision *d,
                               const struct rswap_location *loc,int ret) { vpn_trace_count++; }
static void trace_decision(u64 pfn,unsigned state,unsigned app,unsigned preferred,
                          const struct rswap_location *loc,const struct rswap_decision *vpn) {
    if(vpn) { rswap_format_vpn_decision(last_vpn_text,sizeof(last_vpn_text),pfn,vpn,loc);displayed_vpn++; }
}
'''
        code=code.replace(definition(code,'trace_decision'),display)
        with tempfile.TemporaryDirectory(prefix='hermit-stage3-io-') as tmp:
            path=Path(tmp)/'io.c';path.write_text(code+CASES);binary=path.with_suffix('')
            subprocess.run(['gcc','-O1','-g','-std=gnu11','-Wall','-Wextra','-Werror','-pthread',
                            '-Wno-unused-parameter','-I',str(ROOT/'client'),str(path),'-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True,timeout=20)


if __name__=='__main__': unittest.main()

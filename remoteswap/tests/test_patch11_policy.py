"""Real kernel formatter/counters and actual fast-completion request plumbing."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import placement_harness
from test_patch3_completion import ROOT, SUPPORT, client_code, definition

sys.path.insert(0, str(ROOT / 'tools'))
import rswap_pool as pool

FORMAT_CASES = r'''
int main(int argc, char **argv) {
    unsigned int servers=argc>1 ? atoi(argv[1]) : 1;
    struct rswap_pool_layout p={.nr_servers=servers,.total_pages=servers*16,
        .server_pages={16,16},.tier_pages={{8,8},{8,8}}};
    rswap_placement_init(&p); rswap_policy_stats_init(42);
    for (unsigned int r=0;r<RSWAP_REASON_COUNT;r++) {
        struct rswap_decision d={.reason=r,.state=r<2?r:2,.generation=r>=3,
            .decision_ns=2000000000ULL,.publish_ns=1000000000ULL,
            .server=r%servers,.tier=r&1};
        struct rswap_location loc={.server=d.server,.tier=d.tier};
        rswap_policy_record_alloc(&d,&loc,0);
        rswap_policy_record_submit(r,0); rswap_policy_record_wc(r,0);
    }
    rswap_placement_area(false); /* Stop must preserve cumulative counters. */
    struct seq_file seq;
    rswap_policy_stats_show(&seq,NULL);
    return 0;
}
'''

IO_CASES = r'''
int main(void) {
    struct page p; void *cookie;
    init(); reset(&p); trace_enabled=false;
    rswap_policy_stats_init(123);
    immediate=1;
    assert(!prepare(0,1,&p,0,&cookie));
    assert(result_count==2 && result_stages[0]==1 && result_stages[1]==0);
    assert(result_ids[0] && result_ids[0]==result_ids[1]);
    assert(atomic64_read(&policy_counts[0].post_ok)==1);
    assert(atomic64_read(&policy_counts[0].wc_ok)==1);
    assert(((struct fs_rdma_req *)cookie)->alloc_seq==result_ids[0]);
    atomic_store(&p.valid,1); rswap_store_commit(cookie);
    rswap_invalidate_page(0,1);
    assert(atomic64_read(&policy_counts[0].decisions)==1);
    /* Slow WC: counters use copied reason, independent of later choices. */
    reset(&p); trace_enabled=false; immediate=0;
    policy_override=0;
    assert(!prepare(0,1,&p,0,&cookie));
    struct fs_rdma_req *req=cookie;
    assert(req->policy_reason==RSWAP_REASON_HOT);
    policy_override=-1;
    atomic_store(&p.valid,1); rswap_store_commit(cookie);
    complete_one(&cqs[0][QP_STORE]);
    assert(atomic64_read(&policy_counts[5].post_ok)==1 && atomic64_read(&policy_counts[5].wc_ok)==1);
    assert(atomic64_read(&policy_counts[0].wc_ok)==1);
    rswap_invalidate_area(0); rswap_mapping_destroy();
    puts("PASS: real counters, copied Store key/reason, WC-before-post and post-before-WC");
    return 0;
}
'''


class PolicyStatsTests(unittest.TestCase):
    def test_actual_formatter_single_dual_and_stop_retention(self):
        with tempfile.TemporaryDirectory(prefix='hermit-p11-policy-') as tmp:
            source, binary = Path(tmp)/'policy.c', Path(tmp)/'policy'
            source.write_text(placement_harness.STUBS + placement_harness.production() + FORMAT_CASES)
            subprocess.run(['gcc','-std=gnu11','-O1','-pthread','-Wall','-Wextra','-Werror',
                            '-Wno-unused-parameter','-I',str(ROOT/'client'),str(source),'-o',str(binary)],check=True)
            for servers in (1,2):
                output=subprocess.run([str(binary),str(servers)],check=True,text=True,capture_output=True).stdout
                result=pool.parse_policy(output);pool.validate_settled(result)
                summary=pool.policy_summary(result['counters'])
                self.assertEqual(result['header']['instance_ns'],42)
                self.assertEqual((summary['decisions'],summary['adaptive_decisions'],summary['known_decisions']),(7,5,2))
                self.assertEqual(summary['adaptive_heat_coverage'],0.4)
                self.assertEqual(sum(summary['snapshot_age_buckets'].values()),4)

    def test_actual_request_result_ordering(self):
        source=(ROOT/'client/rswap_placement.c').read_text()
        counters=source[source.index('static u64 policy_instance_ns;'):source.index('static unsigned int rswap_policy_phase')]
        code=client_code(SUPPORT.replace('int main(void)','int legacy_main(void)'))
        for name in ('rswap_policy_record_alloc','rswap_policy_record_submit','rswap_policy_record_wc'):
            code=code.replace(definition(code,name),'')
        counter_stub=definition(code,'trace_store_result')
        tracking=r'''
static int result_count;
static unsigned int result_stages[8]; static u64 result_ids[8];
static void trace_store_result(u64 id,u64 off,unsigned int s,unsigned int t,unsigned int stage,int status) {
    assert(result_count<8 && status==0);result_stages[result_count]=stage;result_ids[result_count++]=id;
}
'''
        code=code.replace(counter_stub,tracking+'\n#define NSEC_PER_SEC 1000000000ULL\n'+counters)
        code=code.replace('d->server = 0; d->tier = policy_override;',
                          'd->server = 0; d->tier = policy_override; d->reason = RSWAP_REASON_HOT;')
        with tempfile.TemporaryDirectory(prefix='hermit-p11-request-') as tmp:
            path,binary=Path(tmp)/'request.c',Path(tmp)/'request'
            path.write_text(code+IO_CASES)
            subprocess.run(['gcc','-std=gnu11','-O1','-pthread','-Wall','-Wextra','-Werror',
                            '-Wno-unused-parameter','-I',str(ROOT/'client'),str(path),'-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True,timeout=20)


if __name__ == '__main__':
    unittest.main()

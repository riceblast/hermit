"""Real capacity model, upload transaction, routing, and offline source checks."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import placement_harness
from test_patch10_upload import BRIDGE
from test_patch10_trace import decision

ROOT = placement_harness.ROOT
sys.path.insert(0, str(ROOT / 'tools'))
import rswap_trace as trace
import rswap_pool as pool
from rswap_analysis import hotness_summary, check_heat


def compile_c(source, path, *extra):
    path.write_text(source)
    binary = path.with_suffix('')
    subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-I', str(ROOT / 'tools'), '-I', str(ROOT / 'client'),
                    *extra, str(path), '-o', str(binary)], check=True)
    return binary


MATH = r'''
#include <assert.h>
#include <stdio.h>
#include "rswap_threshold.h"
static struct rswap_threshold_metadata select_model(struct rswap_threshold_hist *h,
        uint64_t local, int mean, struct rswap_threshold_metadata *old) {
    struct rswap_heat_bin b[RSWAP_THRESHOLD_BINS];
    struct rswap_threshold_config c={.policy=1,.min_pages=1,.min_coverage_ppm=0};
    size_t n=rswap_threshold_bins(h,b);
    return rswap_threshold_select(&c,h,b,n,local,7,mean,old);
}
int main(void) {
    _Static_assert(sizeof(struct rswap_threshold_metadata)==72,"ABI2 metadata");
    struct rswap_threshold_hist h={.population=100,.known=100};
    h.counts[10][10]=40;h.counts[10][1]=30;h.counts[10][0]=30;
    struct rswap_threshold_metadata m=select_model(&h,40,430000,NULL);
    assert(m.threshold_ppm==50000 && m.candidate_pages==60 && m.source==1);
    /* Partial known coverage: scale the sample; do not remove local pages from K. */
    h=(struct rswap_threshold_hist){.population=16384,.known=2048};
    h.counts[4][0]=1024;h.counts[4][1]=512;h.counts[4][4]=512;
    m=select_model(&h,6144,300000,NULL);
    assert(m.candidate_pages==1280 && m.threshold_ppm==50000);
    /* A cut inside a bucket and floor of fixed-point thirds. */
    h=(struct rswap_threshold_hist){.population=7,.known=7};
    h.counts[3][1]=3;h.counts[1][0]=2;h.counts[1][1]=2;
    m=select_model(&h,3,500000,NULL);
    assert(m.candidate_pages==4 && m.threshold_ppm==166666);
    h=(struct rswap_threshold_hist){.population=100,.known=100};h.counts[4][0]=100;
    m=select_model(&h,40,0,NULL);assert(m.threshold_ppm==0 && m.source==1);
    h.counts[4][0]=0;h.counts[4][4]=100;
    m=select_model(&h,40,1000000,NULL);assert(m.threshold_ppm==1000000);
    struct rswap_threshold_metadata old=m;old.generation=2;
    m=select_model(&h,100,123456,&old);
    assert(m.source==2 && m.status==2 && m.threshold_ppm==1000000 && m.generation==2);
    m=select_model(&h,100,123456,NULL);
    assert(m.source==3 && m.threshold_ppm==123456 && m.generation==7);
    h=(struct rswap_threshold_hist){.population=100,.known=0};
    m=select_model(&h,40,-1,&old);assert(m.source==2 && m.status==1);
    m=select_model(&h,40,-1,NULL);assert(m.source==3 && m.threshold_ppm==-1 && !m.generation);
    struct rswap_threshold_config limited={.policy=1,.min_pages=1,.min_coverage_ppm=50000};
    struct rswap_heat_bin b[1]={{.heat=0,.pages=4}};
    h=(struct rswap_threshold_hist){.population=100,.known=4};
    m=rswap_threshold_select(&limited,&h,b,1,40,7,123,NULL);
    assert(m.status==1 && m.source==3 && m.threshold_ppm==123);
    limited.min_coverage_ppm=0;limited.min_pages=4;
    m=rswap_threshold_select(&limited,&h,b,1,90,7,123,NULL);
    assert(m.status==3 && m.candidate_pages==1 && m.source==3);
    /* Products exceed 64 bits, while the final result remains representable. */
    h=(struct rswap_threshold_hist){.population=1ULL<<62,.known=1ULL<<60};h.counts[1][1]=h.known;
    m=select_model(&h,1ULL<<61,1000000,NULL);
    assert(m.candidate_pages==(1ULL<<59) && m.threshold_ppm==1000000);
    puts("PASS: real proportional trim, exact bins, zero/equal heat, fallback and 128-bit products");
}
'''

KERNEL = r'''
static struct file owner;
static loff_t pos;
static ssize_t simple(unsigned int op,u64 gen) {
    struct rswap_upload_command cmd={.version=RSWAP_PLACEMENT_VERSION,.op=op,.generation=gen,.value=op==1};
    return rswap_upload_write(&owner,(void *)&cmd,sizeof(cmd),&pos);
}
static ssize_t begin(u64 gen,int mean,struct rswap_threshold_metadata m) {
    struct {struct rswap_upload_command cmd;struct rswap_threshold_metadata m;} p={
        .cmd={.version=RSWAP_PLACEMENT_VERSION,.op=2,.generation=gen,.count=3,.value=10,.mean_ppm=mean},.m=m};
    return rswap_upload_write(&owner,(void *)&p,sizeof(p),&pos);
}
static void commit(u64 gen) {
    struct {struct rswap_upload_command cmd;struct rswap_heat_record r[3];} p={
        .cmd={.version=RSWAP_PLACEMENT_VERSION,.op=3,.generation=gen,.count=3},
        .r={{.pfn=65,.heat_ppm=0},{.pfn=66,.heat_ppm=250000},{.pfn=67,.heat_ppm=100000}}};
    assert(rswap_upload_write(&owner,(void *)&p,sizeof(p),&pos)>0);
    assert(simple(4,gen)>0);
}
static struct rswap_decision choose(u64 pfn) {
    struct rswap_decision d;assert(!rswap_placement_choose(1,pfn,&d));return d;
}
int main(void) {
    struct rswap_pool_layout layout={.nr_servers=2,.total_pages=32,.server_pages={16,16},.tier_pages={{8,8},{8,8}}};
    rswap_placement_init(&layout);rswap_placement_area(true);assert(!rswap_upload_open(NULL,&owner));
    assert(simple(1,0)>0); /* manual warmup with min duration zero */
    struct rswap_threshold_metadata m={.policy=1,.source=1,.threshold_ppm=250000,
        .generation=1,.population_pages=100,.sample_pages=10,.candidate_pages=6,.local_pages=40,.min_pages=1};
    assert(begin(1,500000,m)>0);commit(1);
    assert(choose(66).reason==RSWAP_REASON_WARMUP);
    assert(rswap_control_write(NULL,"adaptive",8,&pos)>0);
    struct rswap_decision d=choose(65);
    assert(d.reason==RSWAP_REASON_ZERO && d.server==1 && d.tier==0 && d.heat==0);
    d=choose(99);assert(d.reason==RSWAP_REASON_UNKNOWN && d.server==1 && d.tier==0);
    d=choose(66);assert(d.reason==RSWAP_REASON_HOT && d.tier==0 && d.server==0 && d.rr==0);
    assert(d.threshold==250000 && d.mean==500000 && d.threshold_generation==1);
    d=choose(67);assert(d.reason==RSWAP_REASON_COLD && d.tier==1 && d.rr==0);
    m.source=2;m.status=1;assert(begin(2,900000,m)>0);commit(2);
    d=choose(66);assert(d.reason==RSWAP_REASON_HOT && d.generation==2 && d.threshold_generation==1);
    m.threshold_ppm=300000;assert(begin(3,900000,m)==-EINVAL);
    assert(choose(66).generation==2); /* Invalid metadata cannot change active snapshot. */
    struct rswap_upload_command old={.version=1,.op=1};
    assert(rswap_upload_write(&owner,(void *)&old,sizeof(old),&pos)==-EINVAL);
    assert(choose(66).generation==2);
    rswap_upload_release(NULL,&owner);rswap_placement_exit();assert(!live_allocations);
    puts("PASS: actual ABI2 staging, zero/unknown Interleave, positive equality, retained threshold and rejection");
}
'''


class CapacityTests(unittest.TestCase):
    def test_real_trim_math(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary=compile_c(MATH,Path(tmp)/'math.c')
            subprocess.run([str(binary)],check=True,timeout=5)

    def test_real_kernel_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary=compile_c(placement_harness.STUBS+placement_harness.production()+KERNEL,
                             Path(tmp)/'kernel.c','-pthread')
            subprocess.run([str(binary)],check=True,timeout=5)

    def test_real_sampler_upload_and_threshold_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);out=directory/'heat';out.mkdir()
            bridge=BRIDGE.replace('.placement=true', '.placement=true, .pages=true').replace('.placement_log=tmpfile()',
                '.window=4, .out=OUT_DIR, .local_bytes=8192, '
                '.threshold_config={.policy=1,.min_pages=1}, '
                '.placement_log=fopen(OUT_DIR "/placement.tsv","w"), '
                '.distribution_log=fopen(OUT_DIR "/distribution.tsv","w")')
            bridge=bridge.replace('FILE *summary=tmpfile();', 'FILE *summary=fopen(OUT_DIR "/summary.tsv","w");')
            bridge=bridge.replace('struct page_list pages={.v=rows,.n=5};',
                'for (size_t i=0;i<5;i++) { rows[i].va=(i+1)*4096; } struct page_list pages={.v=rows,.n=5};')
            bridge=bridge.replace('.state=RESIDENT,.pfn=pages.n-i', '.state=RESIDENT,.va=(i+1)*4096,.pfn=pages.n-i')
            bridge=bridge.replace('uint64_t start;', r'''
    fputs(summary_header,summary);
    fputs("version\tsample_generation\trun_id\tstart_ns\tgeneration\tpublish_ns\trecords\tknown\tmean_ppm\tupload_ms\tpolicy\tthreshold_ppm\tthreshold_source\tthreshold_generation\tthreshold_status\tpopulation_pages\tthreshold_sample_pages\tcandidate_pages\tlocal_pages\tmin_pages\tmin_coverage_ppm\n",c.placement_log);
    fputs("version\tsample_generation\theat_ppm\tpages\n",c.distribution_log);
    uint64_t start;''')
            bridge=bridge.replace('d.reason==RSWAP_REASON_NO_MEAN && d.generation==2',
                                  'd.reason==RSWAP_REASON_UNKNOWN && d.generation==2')
            bridge=bridge.replace('fclose(summary); fclose(c.placement_log);',
                'assert(!run_metadata(&c,1,start,monotonic_ns(),0,0,0)); '
                'fclose(summary); fclose(c.placement_log); fclose(c.distribution_log);')
            bridge=bridge.replace('int main(void) {', r'''
static int logged_snapshot(struct config *c,FILE *f,const struct page_list *p,
        uint64_t gen,uint64_t begin,uint64_t end,uint64_t start) {
    uint64_t tick=monotonic_ns(); atomic_store(&mock_now,tick);
    return snapshot(c,f,p,gen,tick,tick,start);
}
#define snapshot(...) logged_snapshot(__VA_ARGS__)
int main(void) {''')
            binary=compile_c('#define OUT_DIR '+json.dumps(str(out))+'\n'+placement_harness.STUBS+
                             placement_harness.production()+bridge, directory/'bridge.c','-pthread')
            subprocess.run([str(binary)],check=True,timeout=10)
            r=hotness_summary(out)
            self.assertEqual(r['publications'],3)
            with (out/'placement.tsv').open() as f:rows=list(csv.DictReader(f,delimiter='\t'))
            self.assertEqual([int(r['threshold_source']) for r in rows],[1,2,1])
            self.assertEqual([int(r['threshold_ppm']) for r in rows],[0,0,1000000])
            def logged(seq,pfn,reason,gen=1,heat=1000000):
                pub=rows[gen-1]
                line=decision(alloc_seq=seq,pfn=pfn,reason=reason,generation=gen,
                              run=int(pub['run_id']),mean_ppm=int(pub['mean_ppm']),
                              publish_ns=int(pub['publish_ns']),decision_ns=int(pub['publish_ns'])+1,
                              heat_ppm=heat,preferred_server=0 if reason==5 else 1,
                              server=0 if reason==5 else 1).rstrip('\n')
                return line+' '+ ' '.join(k+'='+pub[k] for k in
                    ('threshold_ppm','policy','threshold_source','threshold_generation','threshold_status'))+'\n'
            decisions=trace.read_decisions([logged(1,65,5),logged(2,66,7,heat=0),
                                            logged(3,65,4,gen=2,heat=-1)],((16,32),(16,32)))
            checked=check_heat(decisions,out)
            self.assertEqual(checked['known_pfn_consistent'],2)
            self.assertEqual(checked['zero_interleave_consistent'],1)
            self.assertEqual(checked['pfn_miss_consistent'],1)
            path=out/'placement.tsv';original=path.read_text()
            rows[0]['threshold_ppm']='1'
            with path.open('w') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter='\t');w.writeheader();w.writerows(rows)
            with self.assertRaises(ValueError):hotness_summary(out)
            path.write_text(original)

    def test_new_trace_rules_and_zero_coverage(self):
        def modern(**kw):
            line=decision(**kw).rstrip('\n')
            return line+' threshold_ppm=500000 policy=0 threshold_source=0 threshold_generation=1 threshold_status=0\n'
        d=trace.read_decisions([modern(heat_ppm=500000)],((16,32),(16,32)))
        self.assertEqual(d[1]['reason'],5)
        z=modern(reason=7,heat_ppm=0,preferred_server=1,server=1)
        self.assertEqual(trace.read_decisions([z],((16,32),(16,32)))[1]['reason'],7)
        for bad in (modern(heat_ppm=0),modern(reason=7,heat_ppm=250000,preferred_server=1,server=1)):
            with self.assertRaises(ValueError):trace.read_decisions([bad],((16,32),(16,32)))
        from test_patch11_tools import policy
        self.assertIsNone(pool.policy_summary(pool.parse_policy(policy(count=10))['counters'])['positive_heat_coverage'])
        text=policy(count=10).replace('version=1','version=2').replace('reason 5 10 10 0 10 0','reason 5 0 0 0 0 0')
        text+='reason 7 10 10 0 10 0\n'
        p=pool.parse_policy(text);pool.validate_settled(p);r=pool.policy_summary(p['counters'])
        self.assertEqual((r['known_decisions'],r['classified_decisions']),(10,0))
        self.assertEqual(r['adaptive_heat_coverage'],1)
        self.assertEqual(r['positive_heat_coverage'],0)

    def test_cgroup_effective_capacity_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cg=root/'parent'/'child';cg.mkdir(parents=True)
            (cg/'memory.limit_in_bytes').write_text(str(8*1024**3))
            (cg.parent/'memory.limit_in_bytes').write_text(str(6*1024**3))
            source=r'''
#define main sampler_unused_main
#include "rswap_hotness.c"
#undef main
#include <assert.h>
int main(int argc,char **argv) {
    assert(argc==2);struct config c={.memory_cgroup=argv[1],.reserve_bytes=1ULL<<30};
    uint64_t pages;assert(!local_capacity(&c,&pages) && pages==(5ULL<<30)/4096);
    c.local_bytes=4ULL<<30;assert(!local_capacity(&c,&pages) && pages==(3ULL<<30)/4096);
    c.reserve_bytes=4ULL<<30;assert(local_capacity(&c,&pages)<0);
    return 0;
}
'''
            binary=compile_c(source,root/'cgroup.c')
            subprocess.run([str(binary),str(cg)],check=True,timeout=5)


if __name__=='__main__':unittest.main()

"""Exercise actual display formatters, exact sidecars and Store-only events."""
import csv
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from placement_harness import ROOT, function
sys.path.insert(0, str(ROOT/'tools'))
from test_patch3_completion import client_code, definition, SUPPORT
from test_patch10_trace import decision
from rswap_hotness_check import read_summary, read_pages
from rswap_trace import read_decisions


def build(code, path, *extra):
    path.write_text(code)
    binary = path.with_suffix('')
    subprocess.run(['gcc', '-std=gnu11', '-O1', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-I', str(ROOT/'client'), '-I', str(ROOT/'tools'),
                    str(path), *extra, '-o', str(binary)], check=True)
    return binary


SNAPSHOT = r'''
#define main sampler_program_main
#include "../tests/fixtures/rswap_hotness_v1.c"
#undef main
#include <assert.h>
int main(int argc, char **argv) {
    assert(argc == 3);
    struct config c = {.pid=getpid(),.out=argv[1],.raw_logs=atoi(argv[2]),
        .pages=true,.window=8,.interval=1};
    assert(!open_raw_logs(&c));
    struct hot_page rows[] = {
        {.va=4096,.pfn=65,.state=RESIDENT,.heat={1,15}},
        {.va=8192,.previous_pfn=66,.state=SWAPPED,.swap=(100ULL<<5)|2,.heat={3,7}},
        {.va=12288,.previous_pfn=67,.state=CHANGED},
        {.va=16384,.pfn=68,.state=RESIDENT,.heat={1,255}},
        {.va=20480,.pfn=65,.state=RESIDENT,.alias=1,.heat={1,1}},
    };
    struct page_list pages={.v=rows,.n=5};
    char *path; assert(asprintf(&path,"%s/summary.tsv",c.out)>=0);
    FILE *summary=fopen(path,"wx"); free(path); assert(summary);
    fputs(summary_header,summary);
    uint64_t now=monotonic_ns();
    assert(!snapshot(&c,summary,&pages,0,now,now,now)); fclose(summary);
    if(c.raw_summary) fclose(c.raw_summary);
    assert(!run_metadata(&c,1,now,monotonic_ns(),0,0,0));
    free(c.raw_out);
    return 0;
}
'''


class DisplayTests(unittest.TestCase):
    def test_actual_tsv_defaults_and_optional_precise_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = build(SNAPSHOT, root/'snapshot.c')
            for raw in (False, True):
                out = root/str(raw)
                out.mkdir()
                subprocess.run([str(binary), str(out), str(int(raw))], check=True, capture_output=True)
                with (out/'summary.tsv').open() as f:
                    reader = csv.DictReader(f, delimiter='\t')
                    self.assertEqual(reader.fieldnames, ['version','pid','generation','pages','present','swapped','others','mean','scan_ms','pages_file'])
                    row = next(reader)
                self.assertEqual([row[k] for k in ('version','pages','present','swapped','others','mean')], ['2','5','3','1','1','0.35'])
                with (out/'pages-000000.tsv').open() as f:
                    reader = csv.DictReader(f, delimiter='\t')
                    self.assertEqual(reader.fieldnames, ['va','pfn','last_pfn','state','heat'])
                    self.assertEqual([p['heat'] for p in reader], ['0.25','0.67','-','0.13','1.00'])
                if raw:
                    snapshot = read_summary(out/'summary.tsv')[0]
                    self.assertEqual(snapshot.path.parent, out/'raw')
                    self.assertEqual(set(read_pages(snapshot)), {65,68})
                    self.assertAlmostEqual(snapshot.mean, (0.25+2/3+0.125)/3, places=8)
                else:
                    self.assertFalse((out/'raw').exists())
                    with self.assertRaisesRegex(ValueError, '--raw-logs'):
                        read_summary(out/'summary.tsv')

    def test_actual_decision_formatter_uses_final_tier_and_safe_text(self):
        source = (ROOT/'client/rswap_trace.h').read_text()
        code = r'''
#include <stdio.h>
#include <stdint.h>
typedef uint64_t u64;
#define scnprintf snprintf
#define RSWAP_WARMUP 1
#define RSWAP_ADAPTIVE 2
#define RSWAP_TIER_DRAM 0
#define RSWAP_APP_DRAM 1
struct rswap_location { unsigned int server, tier; };
'''
        code += function(source, 'rswap_format_decision')
        code += r'''
int main(void) {
    for (unsigned int state=0;state<3;state++)
        for (unsigned int server=0;server<2;server++)
            for (unsigned int tier=0;tier<2;tier++)
                for(unsigned int reason=0;reason<8;reason++) {
                    struct rswap_location loc={server,tier}; char text[192];
                    rswap_format_decision(text,sizeof(text),UINT64_MAX,state,0,tier,&loc);
                    puts(text);
                }
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            binary = build(code, Path(tmp)/'format.c')
            lines = subprocess.check_output([str(binary)], text=True).splitlines()
        self.assertEqual(len(lines),96)
        index = 0
        for state in range(3):
            for server in range(2):
                for tier in range(2):
                    for reason in range(8):
                        policy = ('interleave','adaptive(warmup)','adaptive')[state]
                        why = ('interleave','interleave(warmup)',('hot','cold')[tier])[state]
                        self.assertEqual(lines[index], f'req_type: store, pfn: 0xffffffffffffffff, placement_policy: {policy}, reason: {why}, server: {server}, target_mem: {("dram","pmem")[tier]},')
                        self.assertLess(len(lines[index]),192)
                        index += 1

    def test_exact_trace_parser_uses_raw_event_and_keeps_old_logs(self):
        old = decision()
        raw = old.replace('decision:', 'decision_raw:')
        display = 'decision: req_type: store, pfn: 0x41, placement_policy: adaptive, reason: hot, server: 0, target_mem: dram,\n'
        layout = ((16,32),(16,32))
        self.assertEqual(read_decisions([old],layout), read_decisions([display,raw],layout))
        with self.assertRaisesRegex(ValueError,'decision_raw'):
            read_decisions([display],layout)

    def test_fixed_decision_format_and_tier_fallback(self):
        source = (ROOT/'client/rswap_trace.h').read_text()
        code = r'''
#include <stdio.h>
#include <stdint.h>
typedef uint64_t u64;
#define scnprintf snprintf
#define RSWAP_WARMUP 1
#define RSWAP_ADAPTIVE 2
#define RSWAP_TIER_DRAM 0
#define RSWAP_APP_DRAM 1
struct rswap_location { unsigned int server, tier; };
'''
        code += function(source, 'rswap_format_decision')
        code += r'''
int main(void) {
    for (unsigned int state=0;state<3;state++)
        for (unsigned int policy=1;policy<=2;policy++)
            for (unsigned int server=0;server<2;server++)
                for (unsigned int tier=0;tier<2;tier++) {
                    struct rswap_location loc={server,tier}; char text[192];
                    rswap_format_decision(text,sizeof(text),UINT64_MAX,state,policy,policy-1,&loc);
                    puts(text);
                }
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            binary = build(code, Path(tmp)/'fixed-format.c')
            lines = subprocess.check_output([str(binary)], text=True).splitlines()
        self.assertEqual(len(lines),24)
        expected = []
        for state in range(3):
            for policy in (1,2):
                for server in range(2):
                    for tier in range(2):
                        name = ('pureDRAM','purePMem')[policy-1]
                        reason = 'fixed' if tier == policy-1 else 'capacity_fallback'
                        expected.append(f'req_type: store, pfn: 0xffffffffffffffff, placement_policy: {name}, reason: {reason}, server: {server}, target_mem: {("dram","pmem")[tier]},')
        self.assertEqual(lines,expected)
        self.assertTrue(all(len(line)<192 and line.count(',')==6 for line in lines))

    def test_fixed_store_emission_success_failure_fallback_and_load(self):
        code = client_code(SUPPORT.replace('int main(void)', 'int legacy_main(void)'))
        formatter = '#define scnprintf snprintf\n' + function(
            (ROOT/'client/rswap_trace.h').read_text(), 'rswap_format_decision')
        code = code.replace(definition(code,'trace_decision'), formatter + r'''
static int displayed, app_detailed;
static char last_text[192];
static void trace_decision(u64 pfn,unsigned int state,unsigned int app_policy,
                          unsigned int preferred_tier,const struct rswap_location *loc, const struct rswap_decision *vpn) {
    assert(app_policy>=1 && app_policy<=2 && preferred_tier==app_policy-1);
    rswap_format_decision(last_text,sizeof(last_text),pfn,state,app_policy,preferred_tier,loc);
    displayed++;
}
''')
        code = code.replace('#define trace_app_decision(...) ((void)0)', r'''
static void trace_app_decision(u64 offset,u64 pfn,const struct rswap_app_decision *d,
                               const struct rswap_location *loc,int ret);
''')
        code += r'''
static void trace_app_decision(u64 offset,u64 pfn,const struct rswap_app_decision *d,
                               const struct rswap_location *loc,int ret) { app_detailed++; }
static void fresh(struct page *p) {
    *p=(struct page){0}; atomic_store(&p->refs,1); atomic_store(&p->locked,1);
}
static void store(struct page *p,u64 offset) {
    void *cookie; fresh(p);
    assert(!rswap_store_prepare(0,offset,p,0,&cookie));
    struct ib_cq *cq=((struct fs_rdma_req *)cookie)->rdma_queue->cq;
    atomic_store(&p->valid,1); rswap_store_commit(cookie); complete_one(cq);
}
int main(void) {
    init(); rswap_mapping_destroy();
    pool_layout=(struct rswap_pool_layout){.nr_servers=2,.total_pages=8,
        .server_pages={4,4},.tier_pages={{2,2},{2,2}}};
    assert(!rswap_mapping_init(&pool_layout)); trace_enabled=false;
    for (int preferred=0;preferred<2;preferred++) {
        struct page pages[8],extra,read;
        struct rswap_location pinned;
        void *cookie;
        app_override=preferred; app_rr=0; displayed=app_detailed=0;
        for (unsigned off=0;off<8;off++) {
            store(&pages[off],off);
            assert(displayed==(int)off+1 && app_detailed==(int)off+1);
            assert(strstr(last_text,preferred ? "placement_policy: purePMem," : "placement_policy: pureDRAM,"));
            assert(strstr(last_text,off<4 ? "reason: fixed," : "reason: capacity_fallback,"));
        }
        fresh(&extra);
        assert(rswap_store_prepare(0,0,&extra,0,&cookie)==-EEXIST);
        assert(displayed==8 && app_detailed==9);
        assert(!rswap_mapping_get(0,&pinned)); rswap_mapping_invalidate(0);
        assert(rswap_store_prepare(0,0,&extra,0,&cookie)==-ENOSPC);
        assert(displayed==8 && app_detailed==10);
        rswap_mapping_put(pinned.id);
        store(&extra,0);
        assert(displayed==9 && app_detailed==11 && strstr(last_text,"reason: fixed,"));
        fresh(&read);
        assert(!rswap_rdma_submit(0,0,&read,QP_LOAD_SYNC,NULL));
        complete_one(&cqs[0][QP_LOAD_SYNC]);
        assert(displayed==9 && app_detailed==11 && atomic_load(&read.uptodate));
        rswap_mapping_reset();
    }
    /* Another Server in the same Tier is still labelled fixed. */
    struct rswap_location loc; struct page p;
    for (unsigned off=0;off<2;off++) {
        assert(!rswap_mapping_alloc_preferred(off,0,0,&loc)); rswap_mapping_put(loc.id);
    }
    app_override=0; app_rr=0; store(&p,2);
    assert(strstr(last_text,"reason: fixed,") && strstr(last_text,"server: 1, target_mem: dram,"));
    rswap_invalidate_area(0); rswap_mapping_destroy();
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            binary = build(code, Path(tmp)/'fixed-io.c', '-pthread')
            subprocess.run([str(binary)], check=True, capture_output=True, timeout=15)

    def test_actual_io_emits_no_load_or_failed_allocation_display(self):
        code = client_code(SUPPORT.replace('int main(void)', 'int legacy_main(void)'))
        code = code.replace(definition(code,'trace_decision'), '''
static int displayed, detailed;
static void trace_decision(u64 pfn,unsigned int state,unsigned int app_policy,
                          unsigned int preferred_tier,const struct rswap_location *loc, const struct rswap_decision *vpn) {
    displayed++;
}
''')
        code = code.replace(definition(code,'trace_decision_raw'), '''
static void trace_decision_raw(u64 off,u64 pfn,const struct rswap_decision *d,const struct rswap_location *loc,int ret) { detailed++; }
''')
        code += r'''
int main(void) {
    init(); struct page p; reset(&p); void *cookie;
    assert(rswap_store_prepare(0,1,&p,0,&cookie)==-EEXIST);
    assert(displayed==0 && detailed==1);
    assert(!prepare(0,1,&p,0,&cookie));
    struct fs_rdma_req *req=cookie; struct ib_cq *cq=req->rdma_queue->cq;
    atomic_store(&p.valid,1); rswap_store_commit(cookie); complete_one(cq);
    assert(displayed==1 && detailed==2);
    struct page read={0}; atomic_store(&read.refs,1); atomic_store(&read.locked,1);
    assert(!rswap_rdma_submit(0,1,&read,QP_LOAD_SYNC,NULL));
    complete_one(&cqs[1][QP_LOAD_SYNC]);
    assert(displayed==1 && detailed==2);
    rswap_invalidate_area(0); rswap_mapping_destroy();
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            binary = build(code, Path(tmp)/'io.c', '-pthread')
            subprocess.run([str(binary)], check=True, capture_output=True, timeout=15)

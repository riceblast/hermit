"""Exercise the current VPN sampler with pagemap, kpageflags and idle fixtures."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from rswap_hotness_check import read_vpn_summary, read_vpn_reports, LEGACY_VPN_HEADER
from rswap_analysis import hotness_summary, publications

HARNESS = r'''
#define main sampler_program_main
#include "rswap_hotness.c"
#undef main
#include <assert.h>
static FILE *maps, *pm, *flags, *idle;
static void word(FILE *f, uint64_t index, uint64_t value) {
    assert(!exact_io(fileno(f), &value, 8, index*8, true));
}
static void map_text(const char *s) {
    assert(!ftruncate(fileno(maps),0)); rewind(maps); fputs(s,maps); fflush(maps);
}
static void setup(void) {
    maps=tmpfile(); pm=tmpfile(); flags=tmpfile(); idle=tmpfile();
    assert(maps && pm && flags && idle);
    assert(!ftruncate(fileno(pm),4096*8)); assert(!ftruncate(fileno(flags),4096*8));
    assert(!ftruncate(fileno(idle),4096));
    map_text("00001000-00008000 rw-p 00000000 00:00 0 [heap]\n"
             "00008000-00009000 rw-s 00000000 00:00 0 /dev/shm/shared\n");
    word(pm,1,PM_PRESENT|65); word(pm,2,PM_PRESENT|65); word(pm,3,PM_SWAPPED|(99<<5)|2);
    word(pm,5,PM_PRESENT|67); word(pm,6,PM_PRESENT|PM_FILE|68); word(pm,7,PM_PRESENT|69);
    word(pm,8,PM_PRESENT|65);
    uint64_t anon=FLAG(5)|FLAG(12)|FLAG(14);
    word(flags,65,anon); word(flags,66,anon); word(flags,67,anon|FLAG(22));
    word(flags,68,FLAG(5)); word(flags,69,FLAG(5)|FLAG(14));
}
static void teardown(void) { fclose(maps); fclose(pm); fclose(flags); fclose(idle); }
static void round_next(struct page_list *old, unsigned round, unsigned group) {
    /* Fixture page_idle regular-file writes do not emulate OR semantics. Write
     * a clean observed bitmap explicitly to emulate accesses to every frame. */
    word(idle,1,0);
    struct page_list next={0};
    assert(!collect_round(maps,fileno(pm),fileno(flags),fileno(idle),old,&next));
    free(old->v); *old=next;
    if(round) finish_round(old,group,round%group==0);
}
static void history_case(void) {
    setup(); struct page_list p={0}; struct totals t;
    round_next(&p,0,4);
    assert(p.n==3 && p.v[0].heat==0 && p.v[0].sample==0);
    assert(!count_pages(&p,&t) && t.pages==3 && t.sum==0);
    for(unsigned i=1;i<=4;i++) round_next(&p,i,4);
    uint64_t hot=heat_update(0,4,4);
    assert(p.v[0].heat==hot && p.v[1].heat==hot && p.v[2].heat==0);
    assert(!count_pages(&p,&t) && t.pages==3 && t.bins[1]==2 && t.bins[0]==1);
    /* MADV_DONTNEED at VPN1; VPN2 swaps out. Both keep and decay history. */
    word(pm,1,0); word(pm,2,PM_SWAPPED|(123<<5)|2);
    for(unsigned i=1;i<=4;i++) round_next(&p,i,4);
    uint64_t decayed=heat_update(hot,0,4);
    assert(p.n==3 && !p.v[0].flags && p.v[0].heat==decayed && p.v[1].heat==decayed);
    assert(!count_pages(&p,&t) && t.pages==2 && t.sum==decayed);
    /* Return at a different PFN and with different VMA metadata. No reset. */
    map_text("00001000-00008000 rw-p 00002000 00:00 0 [different]\n");
    word(pm,1,PM_PRESENT|66);
    for(unsigned i=1;i<=4;i++) round_next(&p,i,4);
    assert(p.v[0].heat==heat_update(decayed,3,4));
    assert(!count_pages(&p,&t) && t.pages==3);
    /* munmap all old addresses, while another empty VMA remains. */
    map_text("00010000-00011000 rw-p 00000000 00:00 0\n");
    uint64_t before=p.v[0].heat;
    for(unsigned i=1;i<=4;i++) round_next(&p,i,4);
    assert(p.n==3 && p.v[0].heat==heat_update(before,0,4));
    assert(!count_pages(&p,&t) && t.pages==0 && t.sum==0);
    /* Same-address remap resumes the decayed VPN, first sample zero. */
    map_text("00001000-00002000 rw-p 00000000 00:00 0\n");
    before=p.v[0].heat; round_next(&p,1,4);
    assert(p.n==3 && p.v[0].heat==before && p.v[0].sample==0);
    assert(!count_pages(&p,&t) && t.pages==1);
    free(p.v); teardown();
}
static void race_case(void) {
    setup(); struct page_list p={0}; round_next(&p,0,4);
    p.v[0].sample=1; word(pm,1,0);
    p.v[1].sample=1; word(pm,2,PM_PRESENT|66);
    word(pm,3,0);
    assert(!verify_pages(&p,fileno(pm),fileno(flags)));
    assert(!p.v[0].flags && !p.v[0].sample);
    assert((p.v[1].flags&IN_SCOPE) && p.v[1].pfn==66 && !p.v[1].sample);
    assert(!p.v[2].flags);
    /* No write based on the stale PFN ref after verify changed the frame. */
    struct pfn_ref ref={65,1}; word(idle,1,0);
    assert(!arm_pages(&p,&ref,1,fileno(idle)));
    uint64_t value; assert(!exact_io(fileno(idle),&value,8,8,false) && !value);
    free(p.v); teardown();
}
static void denominator_case(void) {
    setup(); struct page_list p={0}; round_next(&p,0,4);
    round_next(&p,1,4); round_next(&p,2,4);
    word(pm,4,PM_PRESENT|66); round_next(&p,3,4); round_next(&p,4,4);
    assert(p.n==4 && p.v[3].vpn==4 && p.v[3].heat==heat_update(0,1,4));
    free(p.v); teardown();
}
static void output_case(const char *dir) {
    char path[PATH_MAX]; snprintf(path,sizeof(path),"%s/summary.tsv",dir);
    FILE *f=fopen(path,"wx"); assert(f); fputs(summary_header,f);
    snprintf(path,sizeof(path),"%s/hot_distribute.tsv",dir);
    FILE *d=fopen(path,"wx"); assert(d); fputs(distribution_header,d);
    struct hot_page v[6]={0}; struct page_list p={.v=v,.n=6}; struct totals t;
    for(int i=0;i<6;i++) v[i].flags=IN_SCOPE;
    assert(!count_pages(&p,&t)); assert(!snapshot(f,d,42,0,0,&t));
    for(int i=0;i<6;i++) v[i].heat=((uint64_t)i*RSWAP_VPN_HEAT_SCALE+2)/5;
    assert(!count_pages(&p,&t));
    assert(t.bins[0]==1 && t.bins[1]==1 && t.bins[2]==1 && t.bins[3]==1 && t.bins[4]==2);
    assert(!snapshot(f,d,42,1,84012345678ULL,&t));
    memset(&t,0,sizeof(t)); assert(!snapshot(f,d,42,2,168000000000ULL,&t));
    /* A displayed mean of 0.0000 must not change the exact classification. */
    p.n=3; for(int i=0;i<3;i++) v[i].heat=i;
    assert(!count_pages(&p,&t)); assert(t.hot==2 && t.cold==1);
    assert(!snapshot(f,d,42,3,252000000000ULL,&t)); fclose(f); fclose(d);
}
static void classification_case(void) {
    struct hot_page v[4]={0}; struct page_list p={.v=v,.n=4}; struct totals t;
    for(int i=0;i<4;i++) v[i].flags=IN_SCOPE;
    assert(!count_pages(&p,&t) && t.hot==4 && !t.cold);
    for(int i=0;i<4;i++) v[i].heat=RSWAP_VPN_HEAT_SCALE;
    assert(!count_pages(&p,&t) && t.hot==4 && !t.cold);
    v[0].heat=v[1].heat=0;
    assert(!count_pages(&p,&t) && t.hot==2 && t.cold==2);
    v[0].heat=0; v[1].heat=1; v[2].heat=2; v[3].flags=0;
    assert(!count_pages(&p,&t) && t.pages==3 && t.sum==3 && t.hot==2 && t.cold==1);
    p.n=0; assert(!count_pages(&p,&t) && !t.pages && !t.hot && !t.cold);
}
static void output_failure_case(void) {
    struct totals t={0}; FILE *good=tmpfile(), *bad=fopen("/dev/full","w");
    assert(good && bad);
    assert(snapshot(good,bad,42,0,0,&t)==-1);
    assert(ftell(good)==0); /* Failed histogram must not emit a summary row. */
    fclose(good); fclose(bad);
    good=tmpfile(); bad=fopen("/dev/full","w"); assert(good && bad);
    assert(snapshot(bad,good,42,0,0,&t)==-1);
    fclose(good); fclose(bad);
}
static void math_case(void) {
    uint64_t h=0; h=heat_update(h,4,4);
    assert(h==858993459ULL && heat_bin(h)==1 && heat_bin(h-1)==0);
    h=heat_update(h,4,4); assert(h==1546188226ULL);
    assert(heat_update(RSWAP_VPN_HEAT_SCALE,64,64)==RSWAP_VPN_HEAT_SCALE);
    assert(heat_bin(RSWAP_VPN_HEAT_SCALE)==4);
    uint64_t x=heat_update(0,1,64); assert(x>0 && heat_bin(x)==0);
}
static void launch_case(void) {
    char *good[]={"/bin/sh","-c","exit 7",NULL}; pid_t pid=launch(good); assert(pid>0);
    int status; assert(waitpid(pid,&status,0)==pid && WIFEXITED(status) && WEXITSTATUS(status)==7);
    char *bad[]={"/hermit/nonexistent",NULL}; assert(launch(bad)<0 && errno==ENOENT);
}
int main(int argc,char **argv) {
    assert(argc>=2);
    switch(atoi(argv[1])) {
    case 0: history_case(); break; case 1: race_case(); break;
    case 2: denominator_case(); break; case 3: assert(argc==3); output_case(argv[2]); break;
    case 4: math_case(); break; case 5: launch_case(); break;
    case 6: classification_case(); break; case 7: output_failure_case(); break; default: abort();
    }
    return 0;
}
'''


class HotnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hermit-stage2-')
        cls.path = Path(cls.tmp.name)
        source = cls.path / 'sampler.c'
        source.write_text(HARNESS)
        cls.binary = cls.path / 'sampler'
        cls.tool = cls.path / 'tool'
        for c, binary, extra in ((source, cls.binary, ['-fsanitize=undefined']),
                                 (ROOT / 'tools/rswap_hotness.c', cls.tool, [])):
            subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                            '-I', str(ROOT / 'tools'), str(c), *extra, '-o', str(binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self, case, *args):
        subprocess.run([str(self.binary), str(case), *map(str, args)], check=True, timeout=15)

    def test_vpn_alias_swap_dontneed_unmap_remap_history(self):
        self.run_case(0)

    def test_scan_race_discards_access_and_stale_baseline(self):
        self.run_case(1)

    def test_new_page_uses_whole_group_denominator(self):
        self.run_case(2)

    def test_paired_reports_boundaries_actual_elapsed_and_zero_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_case(3, tmp)
            self.assertEqual({p.name for p in Path(tmp).iterdir()}, {'summary.tsv', 'hot_distribute.tsv'})
            rows, bins = read_vpn_reports(Path(tmp) / 'summary.tsv')
            self.assertEqual(rows[1]['EWMA_window'], '1(84.012s)')
            self.assertEqual(rows[1]['mean_hotness'], '0.5000')
            self.assertEqual(rows[0]['hot/cold'], '-/-')
            self.assertEqual(rows[1]['hot/cold'], '3(50.00%)/3(50.00%)')
            self.assertEqual(bins[1]['[0.8-1.0]'], '2(33.33%)')
            self.assertEqual(rows[2]['pages'], '0')
            self.assertEqual(rows[2]['hot/cold'], '-/-')
            self.assertEqual(rows[3]['mean_hotness'], '0.0000')
            self.assertEqual(rows[3]['hot/cold'], '2(66.67%)/1(33.33%)')
            self.assertEqual(hotness_summary(tmp)['completed_ewma_windows'], 3)
            with self.assertRaisesRegex(ValueError, 'VPN observation'):
                publications(tmp)
            result = subprocess.run([sys.executable, str(ROOT/'tools/rswap_hotness_check.py'),
                                     str(Path(tmp)/'summary.tsv')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_ewma_fixed_precision_and_bin_boundaries(self):
        self.run_case(4)

    def test_hot_cold_equality_zero_mean_and_excluded_history(self):
        self.run_case(6)

    def test_either_output_io_failure_is_reported(self):
        self.run_case(7)

    def test_launcher_waits_for_exec_and_propagates_exec_failure(self):
        self.run_case(5)

    def test_cli_defaults_and_invalid_legacy_policy_options(self):
        result = subprocess.run([str(self.tool), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('wait 5 seconds AFTER', result.stdout)
        for args in (('--ewma-window', '0'), ('--window', '65'), ('--a-bit-window', 'nan'),
                     ('--interval', '0'), ('--placement',), ('--pages',), ('--pid', '0'),
                     ('--pid','1','--out','/tmp/unused','--','/bin/true')):
            with self.subTest(args=args):
                self.assertEqual(subprocess.run([str(self.tool), *args], capture_output=True).returncode, 2)

    def test_summary_checker_rejects_bad_population_and_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_case(3, tmp)
            p = Path(tmp)/'summary.tsv'
            original = p.read_text()
            for text in (original.replace('3(50.00%)/3(50.00%)', '4(50.00%)/3(50.00%)'),
                         original.replace('3(50.00%)', '3(50.01%)'),
                         original.replace('0.5000', '0.50'),
                         original.replace('1(84.012s)', '3(84.012s)'),
                         original.replace('2(168.000s)', '2(1.000s)')):
                p.write_text(text)
                with self.assertRaises(ValueError):
                    read_vpn_summary(p)

    def test_reports_reject_mismatched_metadata_or_incomplete_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_case(3, tmp)
            summary, distribution = Path(tmp)/'summary.tsv', Path(tmp)/'hot_distribute.tsv'
            original = distribution.read_text()
            for text in (original.replace('0.5000', '0.5001'),
                         '\n'.join(original.splitlines()[:-1])+'\n',
                         original.replace('2(33.33%)', '3(33.33%)')):
                distribution.write_text(text)
                with self.assertRaises(ValueError):
                    read_vpn_reports(summary)
            distribution.write_text(original)
            distribution.unlink()
            with self.assertRaises(OSError):
                read_vpn_reports(summary)

    def test_live_reader_uses_only_the_visible_summary_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_case(3, tmp)
            summary = Path(tmp)/'summary.tsv'
            summary.write_text('\n'.join(summary.read_text().splitlines()[:-1])+'\n')
            with self.assertRaises(ValueError):
                read_vpn_reports(summary)
            rows, bins = read_vpn_reports(summary, allow_pending=True)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(bins), 3)

    def test_archived_single_file_histogram_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'summary.tsv'
            path.write_text('\t'.join(LEGACY_VPN_HEADER)+'\n'+
                            '42\t0(0s)\t0.00\t2\t2(100.0%)\t0(0.0%)\t0(0.0%)\t0(0.0%)\t0(0.0%)\n')
            rows, bins = read_vpn_reports(path)
            self.assertEqual(rows, bins)
            self.assertEqual(hotness_summary(tmp)['format'], 'vpn-ewma-v1')


if __name__ == '__main__':
    unittest.main()

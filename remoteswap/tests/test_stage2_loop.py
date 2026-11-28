"""Run the real CLI loop against harmless files and a simulated slow scan clock."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
from rswap_hotness_check import read_vpn_reports

HARNESS = r'''
#define _GNU_SOURCE
#include <stdio.h>
#include <fcntl.h>
#include <time.h>
#include <unistd.h>
#include <stdarg.h>
static FILE *fixture_fopen(const char *, const char *);
static int fixture_open(const char *, int, ...);
static int fixture_sleep(const struct timespec *, struct timespec *);
static int fixture_clock(clockid_t,struct timespec *);
static ssize_t fixture_pread(int,void *,size_t,off_t);
#define fopen fixture_fopen
#define open fixture_open
#define nanosleep fixture_sleep
#define clock_gettime fixture_clock
#define pread fixture_pread
#define main sampler_program_main
#include "rswap_hotness.c"
#undef main
#undef fopen
#undef open
#undef nanosleep
#undef clock_gettime
#undef pread
#include <assert.h>
static FILE *map_fixture,*pm_fixture,*flags_fixture,*idle_fixture;
static int sleeps;
static int pm_read_fd;
static uint64_t now=1000000000ULL;
static bool exec_case;
static FILE *fixture_fopen(const char *path,const char *mode) {
    if(strstr(path,"/maps")) return fdopen(dup(fileno(map_fixture)),"r");
    return fopen(path,mode);
}
static int fixture_open(const char *path,int flags,...) {
    if(strstr(path,"/pagemap")) return pm_read_fd=dup(fileno(pm_fixture));
    if(!strcmp(path,"/proc/kpageflags")) return dup(fileno(flags_fixture));
    if(strstr(path,"page_idle/bitmap")) return dup(fileno(idle_fixture));
    errno=ENOENT;return -1;
}
static int fixture_clock(clockid_t id,struct timespec *t) {
    t->tv_sec=now/1000000000; t->tv_nsec=now%1000000000; return 0;
}
static ssize_t fixture_pread(int fd,void *buf,size_t count,off_t offset) {
    /* Two pagemap reads each round, eight simulated seconds apiece. */
    if(fd==pm_read_fd) now+=8000000000ULL;
    return pread(fd,buf,count,offset);
}
static int fixture_sleep(const struct timespec *wait,struct timespec *remaining) {
    assert(wait->tv_sec==5 && !wait->tv_nsec);
    now+=5000000000ULL; sleeps++;
    uint64_t bits=0; assert(pwrite(fileno(idle_fixture),&bits,8,8)==8);
    if(exec_case && sleeps==1) {
        /* Held old maps fd returns EOF after its mm_users goes away. */
        assert(!ftruncate(fileno(map_fixture),0));
    }
    if(sleeps==9) interrupted=SIGTERM;
    return 0;
}
int main(int argc,char **argv) {
    assert(argc==3); exec_case=atoi(argv[2]);
    map_fixture=tmpfile(); pm_fixture=tmpfile(); flags_fixture=tmpfile(); idle_fixture=tmpfile();
    assert(map_fixture && pm_fixture && flags_fixture && idle_fixture);
    fputs("00001000-00003000 rw-p 00000000 00:00 0 [heap]\n",map_fixture);fflush(map_fixture);
    assert(!ftruncate(fileno(pm_fixture),4096*8)); assert(!ftruncate(fileno(flags_fixture),4096*8));
    assert(!ftruncate(fileno(idle_fixture),4096));
    uint64_t value=PM_PRESENT|65; assert(pwrite(fileno(pm_fixture),&value,8,8)==8);
    value=PM_SWAPPED|(200<<5)|2; assert(pwrite(fileno(pm_fixture),&value,8,16)==8);
    value=FLAG(5)|FLAG(12)|FLAG(14); assert(pwrite(fileno(flags_fixture),&value,8,65*8)==8);
    char pid[32]; snprintf(pid,sizeof(pid),"%ld",(long)getpid());
    char *args[]={"rswap_hotness","--pid",pid,"--out",argv[1],NULL};
    int result=sampler_program_main(5,args);
    if(exec_case) assert(result==1 && sleeps==1);
    else assert(result==143 && sleeps==9);
    fclose(map_fixture);fclose(pm_fixture);fclose(flags_fixture);fclose(idle_fixture);
    return 0;
}
'''


class LoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hermit-stage2-loop-')
        source = Path(cls.tmp.name)/'loop.c'
        source.write_text(HARNESS)
        cls.binary = source.with_suffix('')
        subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=undefined', '-I', str(ROOT/'tools'),
                        str(source), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_scan_then_sleep_and_nonoverlapping_groups_with_slow_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'out'
            result = subprocess.run([str(self.binary), str(out), '0'], check=True,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.stdout.strip(), 'START a_bit_window=5.000 EWMA_window=4')
            self.assertEqual({p.name for p in out.iterdir()}, {'summary.tsv', 'hot_distribute.tsv'})
            rows, bins = read_vpn_reports(out/'summary.tsv')
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(bins), 3)
            self.assertEqual([r['mean_hotness'] for r in rows], ['0.0000', '0.1000', '0.1800'])
            self.assertEqual(rows[0]['hot/cold'], '2(100.00%)/0(0.00%)')
            self.assertEqual(rows[1]['hot/cold'], '1(50.00%)/1(50.00%)')
            self.assertEqual([r['pages'] for r in rows], ['2','2','2'])
            self.assertEqual(rows[1]['EWMA_window'], '1(84.000s)')
            self.assertEqual(rows[2]['EWMA_window'], '2(168.000s)')

    def test_exec_mm_eof_fails_without_reopening_or_mixing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'out'
            result = subprocess.run([str(self.binary), str(out), '1'], check=True,
                                    capture_output=True, text=True, timeout=10)
            self.assertIn('Stale file handle', result.stderr)
            self.assertEqual(len(read_vpn_reports(out/'summary.tsv')[0]), 1)


if __name__ == '__main__':
    unittest.main()

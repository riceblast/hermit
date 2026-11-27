"""Run the production CLI through exit races at every publication boundary."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
from rswap_hotness_check import read_vpn_summary

HARNESS = r'''
#define _GNU_SOURCE
#include <stdio.h>
#include <fcntl.h>
#include <time.h>
#include <unistd.h>
#include <stdarg.h>
static FILE *fixture_fopen(const char *,const char *);
static int fixture_open(const char *,int,...);
static int fixture_sleep(const struct timespec *,struct timespec *);
static int fixture_clock(clockid_t,struct timespec *);
static ssize_t fixture_pread(int,void *,size_t,off_t);
static ssize_t fixture_write(int,const void *,size_t);
#define fopen fixture_fopen
#define open fixture_open
#define nanosleep fixture_sleep
#define clock_gettime fixture_clock
#define pread fixture_pread
#define write fixture_write
#define main sampler_program_main
#include "rswap_hotness.c"
#undef main
#undef fopen
#undef open
#undef nanosleep
#undef clock_gettime
#undef pread
#undef write
#include <assert.h>
#define IDENT_FD 900
#define HEAT_FD 901
static FILE *maps_file,*pm_file,*flags_file,*idle_file;
static int pm_fd,idle_fd,mode,sleeps,status_reads,commits;
static uint64_t now_ns=1000000000ULL,proc_flags,start_token=42;
static char proc_state='R';
static bool revoked,stat_denied;
static struct rswap_vpn_heat_command pending,published;
static bool fail_at(unsigned point) {
    if(mode!=(int)point) return false;
    if(mode==13) stat_denied=true;
    if(mode==12) start_token++;
    if(mode!=2 && mode!=3 && mode!=9 && mode!=12 && mode!=13 && mode!=14) proc_flags=TASK_EXITING;
    revoked=true; errno=mode==16 ? EPROTO : ESTALE; return true;
}
static FILE *fixture_fopen(const char *path,const char *access) {
    if(strstr(path,"/stat")) {
        if(stat_denied) { errno=EACCES; return NULL; }
        FILE *f=tmpfile(); assert(f);
        fprintf(f,"%ld (target ) with spaces) %c",(long)getpid(),proc_state);
        for(int field=4;field<=22;field++)
            fprintf(f," %" PRIu64,field==9 ? proc_flags : field==22 ? start_token : 0);
        fputc('\n',f);rewind(f);return f;
    }
    if(strstr(path,"/maps")) return fdopen(dup(fileno(maps_file)),"r");
    return fopen(path,access);
}
static int fixture_open(const char *path,int flags,...) {
    if(strstr(path,"/pagemap")) return pm_fd=dup(fileno(pm_file));
    if(!strcmp(path,"/proc/kpageflags")) return dup(fileno(flags_file));
    if(strstr(path,"page_idle/bitmap")) return idle_fd=dup(fileno(idle_file));
    if(strstr(path,"/rswap/identity")) return IDENT_FD;
    if(strstr(path,"/rswap/vpn_heat")) return HEAT_FD;
    errno=ENOENT;return -1;
}
static int fixture_clock(clockid_t id,struct timespec *t) {
    t->tv_sec=now_ns/1000000000; t->tv_nsec=now_ns%1000000000; return 0;
}
static int fixture_sleep(const struct timespec *t,struct timespec *remaining) {
    assert(t->tv_sec==5 && !t->tv_nsec);now_ns+=5000000000ULL;
    assert(++sleeps<=5);status_reads=0;
    uint64_t value=0;assert(pwrite(fileno(idle_file),&value,8,8)==8);
    if(mode==0 && sleeps==1) proc_flags=TASK_EXITING;
    if(mode==17 && sleeps==1) proc_state='Z';
    if(mode==18 && sleeps==1) proc_state='X';
    return 0;
}
static ssize_t fixture_pread(int fd,void *buf,size_t n,off_t offset) {
    if(fd==IDENT_FD) {
        status_reads++;
        if(sleeps==4 && status_reads==1 &&
           (fail_at(1) || fail_at(2) || fail_at(3) || fail_at(12) || fail_at(13))) {}
        if(sleeps==4 && status_reads==2) (void)fail_at(4);
        int length=snprintf(buf,n,"version=1 binding_id=7 attached=%d mm_current=%d\n",!revoked,!revoked);
        assert(length>0 && length<(int)n);return length;
    }
    if(fd==HEAT_FD) {
        if(published.window==1 && (fail_at(8) || fail_at(14))) return -1;
        struct rswap_vpn_heat_ack a={.binding_id=7,.window=published.window,.has_snapshot=1,
            .records=published.count,.pages=published.pages,.sum_heat=published.sum_heat};
        assert(n==sizeof(a) && !offset);memcpy(buf,&a,n);return n;
    }
    if(fd==pm_fd && sleeps==1 && mode==11) { proc_flags=TASK_EXITING;errno=EIO;return -1; }
    if(fd==idle_fd && sleeps==1 && mode==10) { errno=EIO;return -1; }
    return pread(fd,buf,n,offset);
}
static ssize_t fixture_write(int fd,const void *buf,size_t n) {
    if(fd==IDENT_FD) return n;
    if(fd==HEAT_FD) {
        struct rswap_vpn_heat_command c;assert(n>=sizeof(c));memcpy(&c,buf,sizeof(c));
        if(c.window==1) {
            if(c.op==2 && fail_at(5)) return -1;
            if(c.op==3 && (fail_at(6) || fail_at(9) || fail_at(16))) return -1;
            if(c.op==4 && fail_at(7)) return -1;
        }
        if(c.op==2) pending=c;
        if(c.op==4) { published=pending;commits++; }
        return n;
    }
    return write(fd,buf,n);
}
int main(int argc,char **argv) {
    assert(argc==3);mode=atoi(argv[2]);
    maps_file=tmpfile();pm_file=tmpfile();flags_file=tmpfile();idle_file=tmpfile();
    assert(maps_file && pm_file && flags_file && idle_file);
    fputs("00001000-00003000 rw-p 00000000 00:00 0 [heap]\n",maps_file);fflush(maps_file);
    assert(!ftruncate(fileno(pm_file),4096*8));assert(!ftruncate(fileno(flags_file),4096*8));
    assert(!ftruncate(fileno(idle_file),4096));
    uint64_t v=PM_PRESENT|65;assert(pwrite(fileno(pm_file),&v,8,8)==8);
    v=PM_SWAPPED|(200<<5)|2;assert(pwrite(fileno(pm_file),&v,8,16)==8);
    v=FLAG(5)|FLAG(12)|FLAG(14);assert(pwrite(fileno(flags_file),&v,8,65*8)==8);
    char pid[32];snprintf(pid,sizeof(pid),"%ld",(long)getpid());
    char *args[]={"sampler","--publish","--pid",pid,"--out",argv[1],NULL};
    int ret=sampler_program_main(6,args);
    bool error=mode==2 || mode==3 || mode==9 || mode==10 || mode==13 || mode==14 || mode==16;
    assert(ret==(error ? 1 : 0));
    assert(commits==(mode==8 || mode==14 ? 2 : 1));
    fclose(maps_file);fclose(pm_file);fclose(flags_file);fclose(idle_file);
    return 0;
}
'''


class ExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='hermit-stage2-exit-')
        source=Path(cls.tmp.name)/'exit.c'
        source.write_text(HARNESS)
        cls.binary=source.with_suffix('')
        subprocess.run(['gcc','-O1','-g','-std=gnu11','-Wall','-Wextra','-Werror',
                        '-Wno-unused-parameter','-fsanitize=undefined','-I',str(ROOT/'tools'),
                        str(source),'-o',str(cls.binary)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self,mode,error=None):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'heat'
            run=subprocess.run([str(self.binary),str(out),str(mode)],check=True,
                               capture_output=True,text=True,timeout=10)
            rows=read_vpn_summary(out/'summary.tsv')
            self.assertEqual(len(rows),1,'unfinished observations must not add a summary row')
            self.assertEqual(rows[0]['EWMA_window'],'0(0s)')
            if error:
                self.assertIn(error,run.stderr)
            else:
                self.assertEqual(run.stderr,'')

    def test_exiting_flag_before_zombie_is_normal_completion(self):
        self.run_case(0)

    def test_exit_between_alive_check_and_binding_check(self):
        self.run_case(1)

    def test_live_exec_still_reports_stale_binding(self):
        self.run_case(2,'Stale file handle')

    def test_live_revoke_or_swapoff_still_reports_stale_binding(self):
        self.run_case(3,'Stale file handle')

    def test_exit_before_begin_does_not_publish_incomplete_group(self):
        self.run_case(4)

    def test_exit_during_begin(self):
        self.run_case(5)

    def test_exit_during_data(self):
        self.run_case(6)

    def test_exit_during_commit(self):
        self.run_case(7)

    def test_exit_after_commit_before_ack(self):
        self.run_case(8)

    def test_live_data_failure_is_not_suppressed(self):
        self.run_case(9,'Stale file handle')

    def test_live_idle_io_error_is_not_suppressed(self):
        self.run_case(10,'Input/output error')

    def test_exit_during_pagemap_scan(self):
        self.run_case(11)

    def test_reused_pid_is_not_followed(self):
        self.run_case(12)

    def test_unreadable_exit_state_keeps_original_failure(self):
        self.run_case(13,'Stale file handle')

    def test_live_ack_failure_is_not_suppressed(self):
        self.run_case(14,'Stale file handle')

    def test_protocol_error_is_not_suppressed_even_if_target_exits(self):
        self.run_case(16,'Protocol error')

    def test_zombie_remains_normal_completion(self):
        self.run_case(17)

    def test_dead_state_remains_normal_completion(self):
        self.run_case(18)


if __name__=='__main__':
    unittest.main()

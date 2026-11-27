"""Production identity bridge with VM/XArray fixtures; no privileged operations."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import placement_harness

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent

SUPPORT = r'''
#include <limits.h>
#include <ctype.h>
#include "rswap_identity.h"
#define PAGE_SHIFT 12
#define GFP_ATOMIC 0
#define XA_FLAGS_LOCK_IRQ 1
#define cond_resched() sched_yield()
#define kmalloc zalloc
static int scnprintf(char *buf,size_t size,const char *fmt,...) {
    va_list ap; va_start(ap,fmt); int n=vsnprintf(buf,size,fmt,ap); va_end(ap);
    return n<0 ? n : ((size_t)n>=size ? (int)size-1 : n);
}
struct mm_struct { int users, count; };
struct task_struct { struct mm_struct *mm; int refs; };
struct pid { struct task_struct *task; int refs; };
static struct pid processes[8];
#define PIDTYPE_PID 0
static struct pid *find_get_pid(int nr) {
    if(nr<=0 || nr>=8 || !processes[nr].task) return NULL;
    processes[nr].refs++; return &processes[nr];
}
static struct task_struct *get_pid_task(struct pid *p,int type) {
    if(!p || !p->task) return NULL; p->task->refs++; return p->task;
}
static struct task_struct *pid_task(struct pid *p,int type) { return p ? p->task : NULL; }
static void put_pid(struct pid *p) { if(p) { assert(p->refs>0); p->refs--; } }
static void put_task_struct(struct task_struct *p) { assert(p->refs>0); p->refs--; }
static struct mm_struct *get_task_mm(struct task_struct *t) {
    if(t->mm) t->mm->users++; return t->mm;
}
static void mmgrab(struct mm_struct *m) { m->count++; }
static void mmdrop(struct mm_struct *m) { assert(m->count>1); m->count--; }
static void mmput(struct mm_struct *m) { assert(m->users>1); m->users--; }
struct fake_slot { unsigned long key; void *value; };
struct xarray { pthread_mutex_t lock; struct fake_slot slots[64]; };
static void xa_init_flags(struct xarray *a,int flags) { pthread_mutex_init(&a->lock,NULL); }
#define xa_lock_irqsave(a,f) do { (f)=0; pthread_mutex_lock(&(a)->lock); } while(0)
#define xa_unlock_irqrestore(a,f) do { (void)(f); pthread_mutex_unlock(&(a)->lock); } while(0)
static void *xa_load(struct xarray *a,unsigned long key) {
    for(int i=0;i<64;i++) if(a->slots[i].value && a->slots[i].key==key) return a->slots[i].value;
    return NULL;
}
static bool store_fail;
static void *__xa_store(struct xarray *a,unsigned long key,void *p,int gfp) {
    if(store_fail) return (void *)(intptr_t)-ENOMEM;
    for(int i=0;i<64;i++) if(a->slots[i].value && a->slots[i].key==key) {
        void *old=a->slots[i].value; a->slots[i].value=p; return old;
    }
    for(int i=0;i<64;i++) if(!a->slots[i].value) {
        a->slots[i]=(struct fake_slot){key,p}; return NULL;
    }
    return (void *)(intptr_t)-ENOMEM;
}
static bool xa_is_err(void *p) { return (uintptr_t)p>=(uintptr_t)-4095; }
static void *__xa_erase(struct xarray *a,unsigned long key) {
    for(int i=0;i<64;i++) if(a->slots[i].value && a->slots[i].key==key) {
        void *p=a->slots[i].value; a->slots[i].value=NULL; return p;
    }
    return NULL;
}
#define xa_for_each(a,index,p) for(int slot_i=0;slot_i<64;slot_i++) \
    if(((p)=(a)->slots[slot_i].value) && (((index)=(a)->slots[slot_i].key),1))
static void xa_destroy(struct xarray *a) { memset(a->slots,0,sizeof(a->slots)); }
static char *strim(char *s) {
    while(isspace((unsigned char)*s)) s++;
    size_t n=strlen(s); while(n && isspace((unsigned char)s[n-1])) s[--n]=0;
    return s;
}
static int kstrtoint(const char *s,int base,int *value) {
    char *end; errno=0; long n=strtol(s,&end,base);
    if(errno || end==s || *end || n>INT_MAX || n<INT_MIN) return -EINVAL;
    *value=n; return 0;
}
static int kstrtoull(const char *s,int base,u64 *value) {
    char *end; errno=0; unsigned long long n=strtoull(s,&end,base);
    if(errno || end==s || *end || *s=='-') return -EINVAL;
    *value=n; return 0;
}
static void (*bind_probe)(void *,struct mm_struct *,unsigned long,unsigned int,unsigned long);
static void (*free_probe)(void *,unsigned int,unsigned long);
static int register_fail, scan_error, scan_seed;
static int register_trace_rswap_swap_bind(
        void (*p)(void *,struct mm_struct *,unsigned long,unsigned int,unsigned long),void *data) {
    if(register_fail==1) return -ENOMEM; assert(!bind_probe); bind_probe=p; return 0;
}
static int register_trace_rswap_swap_free(void (*p)(void *,unsigned int,unsigned long),void *data) {
    if(register_fail==2) return -ENOMEM; assert(!free_probe); free_probe=p; return 0;
}
static void unregister_trace_rswap_swap_bind(
        void (*p)(void *,struct mm_struct *,unsigned long,unsigned int,unsigned long),void *data) {
    assert(bind_probe==p); bind_probe=NULL;
}
static void unregister_trace_rswap_swap_free(void (*p)(void *,unsigned int,unsigned long),void *data) {
    assert(free_probe==p); free_probe=NULL;
}
static void tracepoint_synchronize_unregister(void) { synchronize_rcu(); }
static int rswap_identity_scan_mm(struct mm_struct *mm) {
    assert(bind_probe);
    if(scan_seed) bind_probe(NULL,mm,0x12345000,2,7);
    return scan_error;
}
'''

CASES = r'''
static struct mm_struct mm={1,1}, other_mm={1,1};
static struct task_struct task={&mm,0}, outsider={&other_mm,0};
static struct file owner, other;
static loff_t pos;
static ssize_t send(struct file *f,const char *s) {
    return rswap_identity_write(f,s,strlen(s),&pos);
}
static void setup(void) {
    processes[1].task=&task; processes[2].task=&outsider;
    assert(!rswap_identity_init(1ULL<<30)); rswap_identity_area(true,2);
    assert(!rswap_identity_open(NULL,&owner)); assert(!rswap_identity_open(NULL,&other));
}
static void finish(void) {
    rswap_identity_release(NULL,&owner); rswap_identity_release(NULL,&other);
    rswap_identity_exit(); assert(!bind_probe && !free_probe);
    assert(!live_allocations && mm.users==1 && mm.count==1);
    assert(!processes[1].refs && !task.refs);
}
static void routing(void) {
    setup(); scan_seed=1; assert(send(&owner,"bind 1\n")>0);
    struct rswap_identity_result r;
    assert(!rswap_identity_lookup(2,7,&r) && r.vpn==0x12345 && r.aliases==1);
    struct rswap_identity_context *c=owner.private_data;
    assert(c->entries_count==1 && c->aliases==1 && mm.users==1 && mm.count==2);
    bind_probe(NULL,&other_mm,0x1000,2,8); bind_probe(NULL,&mm,0x1000,3,8);
    assert(rswap_identity_lookup(2,8,&r)==-ENOENT);
    bind_probe(NULL,&mm,0x12345000,2,7); /* duplicate scan/reclaim notification */
    assert(c->aliases==1);
    bind_probe(NULL,&mm,0x22000,2,7); /* preserve multiple monitored aliases */
    assert(!rswap_identity_lookup(2,7,&r) && r.vpn==0x22 && r.aliases==2);
    assert(send(&owner,"lookup 7\n")>0);
    char buf[256]={0}; loff_t p=0;
    assert(rswap_identity_read(&owner,buf,sizeof(buf)-1,&p)>0 && strstr(buf,"found=1") && strstr(buf,"aliases=2"));
    free_probe(NULL,3,7); assert(!rswap_identity_lookup(2,7,&r));
    free_probe(NULL,2,7); assert(rswap_identity_lookup(2,7,&r)==-ENOENT);
    assert(!c->entries_count && !c->aliases && c->frees==1);
    bind_probe(NULL,&mm,0x55000,2,7); /* offset reused by a different VPN */
    assert(!rswap_identity_lookup(2,7,&r) && r.vpn==0x55 && r.aliases==1);
    assert(rswap_identity_lookup(2,1ULL<<30,&r)==-ERANGE);
    finish();
}
static void lifecycle(void) {
    setup(); assert(send(&owner,"bind 1")>0);
    assert(send(&other,"bind 2")==-EBUSY && send(&owner,"bind 2")==-ESTALE);
    /* PID reuse/exec cannot silently redirect the bound mm. Stage 1 binds an
     * explicit live mm; the phase-2 launcher will refresh bindings at exec.
     */
    processes[1].task=&outsider;
    bind_probe(NULL,&other_mm,0x33000,2,9);
    struct rswap_identity_result r; assert(rswap_identity_lookup(2,9,&r)==-ENOENT);
    rswap_identity_area(false,2);
    assert(mm.count==1 && rswap_identity_lookup(2,9,&r)==-ENOENT);
    rswap_identity_area(true,2); assert(send(&owner,"bind 1")==-ESTALE);
    assert(send(&other,"bind 2")>0);
    rswap_identity_release(NULL,&owner); owner.private_data=NULL;
    assert(rcu_access_pointer(identity_target)==other.private_data);
    /* Avoid releasing the already closed descriptor twice. */
    assert(!rswap_identity_open(NULL,&owner));
    finish(); assert(other_mm.count==1 && !processes[2].refs);
}
static void failures(void) {
    register_fail=1; assert(rswap_identity_init(32)==-ENOMEM && !bind_probe);
    register_fail=2; assert(rswap_identity_init(32)==-ENOMEM && !bind_probe && !free_probe);
    register_fail=0; setup();
    assert(send(&owner,"bind 0")==-EINVAL && send(&owner,"bind 7")==-ESRCH);
    assert(send(&owner,"bind 1 trailing")==-EINVAL && send(&owner,"lookup -1")==-EINVAL);
    privileged=false; assert(send(&owner,"bind 1")==-EPERM); privileged=true;
    copy_fault=true; assert(send(&owner,"bind 1")==-EFAULT); copy_fault=false;
    scan_seed=1; store_fail=true;
    assert(send(&owner,"bind 1")==-ENOMEM && !rcu_access_pointer(identity_target));
    assert(mm.count==1 && mm.users==1 && !processes[1].refs);
    store_fail=false;
    rswap_identity_release(NULL,&owner); assert(!rswap_identity_open(NULL,&owner));
    scan_error=-EINTR; assert(send(&owner,"bind 1")==-EINTR && mm.count==1);
    scan_error=0; rswap_identity_release(NULL,&owner); assert(!rswap_identity_open(NULL,&owner));
    assert(send(&owner,"bind 1")>0);
    allocation_fail=0; bind_probe(NULL,&mm,0x1000,2,8); allocation_fail=-1;
    struct rswap_identity_context *c=owner.private_data;
    assert(c->alloc_errors==1);
    finish();
}
static atomic_bool done;
static void *reader(void *arg) {
    struct rswap_identity_result r;
    while(!atomic_load(&done)) {
        int ret=rswap_identity_lookup(2,7,&r);
        assert(ret==-ENOENT || (!ret && r.pid==1 && r.aliases && (r.vpn==1 || r.vpn==2)));
    }
    return NULL;
}
static void concurrent(void) {
    setup(); assert(send(&owner,"bind 1")>0);
    pthread_t workers[4];
    for(int i=0;i<4;i++) assert(!pthread_create(&workers[i],NULL,reader,NULL));
    for(int i=0;i<2000;i++) {
        bind_probe(NULL,&mm,0x1000,2,7); bind_probe(NULL,&mm,0x2000,2,7);
        free_probe(NULL,2,7);
    }
    rswap_identity_area(false,2);
    atomic_store(&done,true);
    for(int i=0;i<4;i++) pthread_join(workers[i],NULL);
    finish();
}
int main(int argc,char **argv) {
    assert(argc==2);
    switch(atoi(argv[1])) {
    case 0:routing();break; case 1:lifecycle();break;
    case 2:failures();break; case 3:concurrent();break; default:abort();
    }
    puts("PASS: production swap-entry/VPN identity bridge");
}
'''


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name)
        source = (ROOT / 'client/rswap_identity.c').read_text()
        production = '\n'.join(line for line in source.splitlines() if not line.startswith('#include'))
        code = path / 'identity.c'
        code.write_text(placement_harness.STUBS + SUPPORT + production + CASES)
        cls.binary = path / 'identity'
        subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-unused-but-set-variable',
                        '-Wno-misleading-indentation', '-I', str(ROOT / 'client'),
                        str(code), '-pthread', '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self, case):
        subprocess.run([str(self.binary), str(case)], check=True, timeout=20)

    def test_aliases_filtering_offset_free_and_reuse(self):
        self.run_case(0)

    def test_descriptor_ownership_and_swapoff(self):
        self.run_case(1)

    def test_allocation_registration_and_seed_failures(self):
        self.run_case(2)

    def test_concurrent_lookup_reuse_and_detach(self):
        self.run_case(3)

    def test_kernel_hook_scope_and_scan_guards(self):
        # Wiring is additionally compiled by the real kernel build. Keep the
        # placement of notifications tied to successful anonymous swap PTEs.
        source = (REPO / 'linux-5.14-rc5/mm/rmap.c').read_text()
        for name in ('try_to_unmap_one', 'hermit_try_to_unmap_one'):
            body = placement_harness.function(source, name)
            self.assertEqual(body.count('trace_rswap_swap_bind('), 1)
            self.assertLess(body.index('set_pte_at(mm, address,'), body.index('trace_rswap_swap_bind('))
            self.assertLess(body.index('trace_rswap_swap_bind('), body.rindex('page_remove_rmap('))
        pmd = placement_harness.function(source, 'rswap_identity_scan_pmd')
        self.assertIn('ACTION_CONTINUE', pmd)
        self.assertNotIn('split_huge', pmd)
        pte = placement_harness.function(source, 'rswap_identity_scan_pte')
        self.assertIn('!non_swap_entry(entry)', pte)
        free = placement_harness.function((REPO / 'linux-5.14-rc5/mm/swapfile.c').read_text(), 'swap_range_free')
        self.assertIn('trace_rswap_swap_free(si->type, offset)', free)


if __name__ == '__main__':
    unittest.main()

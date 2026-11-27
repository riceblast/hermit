"""Compile the actual memcg registry, allocator and RDMA Store/Load functions."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import mapping_harness
import placement_harness
from test_patch3_completion import ROOT, SUPPORT, client_code, definition


APP_STUBS = r'''
#include <stddef.h>
#include <sched.h>
#include "rswap_app.h"
struct cgroup_subsys_state { struct cgroup_subsys_state *parent; int refs; bool online; };
struct mem_cgroup { struct cgroup_subsys_state css; };
struct page { struct mem_cgroup *memcg; };
static struct mem_cgroup *page_memcg(struct page *p) { return p->memcg; }
static bool memcg_disabled, unified;
static int memory_cgrp_subsys;
static bool mem_cgroup_disabled(void) { return memcg_disabled; }
static bool cgroup_subsys_on_dfl(int ignored) { return unified; }
static bool css_tryget_online(struct cgroup_subsys_state *css) {
    if (!css->online) return false;
    css->refs++; return true;
}
static void css_put(struct cgroup_subsys_state *css) { assert(css->refs > 1); css->refs--; }
struct task_struct { struct mem_cgroup *memcg; int refs; };
struct pid { struct task_struct *task; int refs; };
static struct pid processes[8];
#define PIDTYPE_PID 0
static struct pid *find_get_pid(int n) {
    if (n >= 8 || !processes[n].task) return NULL;
    processes[n].refs++; return &processes[n];
}
static struct task_struct *get_pid_task(struct pid *p, int type) {
    if (!p) return NULL;
    p->task->refs++; return p->task;
}
static void put_pid(struct pid *p) { if (p) { assert(p->refs > 0); p->refs--; } }
static void put_task_struct(struct task_struct *p) { assert(p->refs > 0); p->refs--; }
static struct mem_cgroup *mem_cgroup_from_task(struct task_struct *p) { return p->memcg; }
struct list_head { struct list_head *next, *prev; };
#define LIST_HEAD(n) struct list_head n = { &n, &n }
#define entry(p, member) ((struct rswap_app_binding *)((char *)(p) - offsetof(struct rswap_app_binding, member)))
#define list_for_each_entry(p, head, member) \
    for (struct list_head *it = READ_ONCE((head)->next); \
         it != (head) && ((p) = entry(it, member), 1); it = READ_ONCE(it->next))
#define list_for_each_entry_rcu list_for_each_entry
#define list_for_each_entry_safe(p, n, head, member) \
    for (struct list_head *it = (head)->next, *save; \
         it != (head) && ((p) = entry(it, member), save = it->next, \
                          (n) = entry(save, member), (void)(n), 1); it = save)
static void list_add_tail_rcu(struct list_head *item, struct list_head *head) {
    item->prev = head->prev; item->next = head;
    WRITE_ONCE(head->prev->next, item); head->prev = item;
}
static void list_del_rcu(struct list_head *item) {
    item->next->prev = item->prev; WRITE_ONCE(item->prev->next, item->next);
}
#define cond_resched() sched_yield()
static int warn_on(int value) { assert(!value); return value; }
#define WARN_ON(x) warn_on(!!(x))
#define spin_lock_irqsave(p, flags) do { (flags)=0; pthread_mutex_lock(p); } while (0)
#define spin_unlock_irqrestore(p, flags) do { (void)(flags); pthread_mutex_unlock(p); } while (0)
static void trace_mapping(u64 seq, unsigned int action, u64 offset, unsigned int s,
                          unsigned int t, u64 slot, unsigned int users, unsigned int live,
                          unsigned int fallback) {}
'''


APP_CASES = r'''
static struct mem_cgroup root = {.css={.refs=1,.online=true}};
static struct mem_cgroup a = {.css={.parent=&root.css,.refs=1,.online=true}};
static struct mem_cgroup b = {.css={.parent=&root.css,.refs=1,.online=true}};
static struct task_struct tasks[3];
static struct page pa={&a}, pb={&b}, outsider={&root};
static ssize_t bind(struct file *f, int pid, unsigned policy) {
    struct rswap_app_command cmd={1,pid,policy,0}; loff_t pos=0;
    return rswap_app_write(f,(void *)&cmd,sizeof(cmd),&pos);
}
static void open_file(struct file *f) { assert(!rswap_app_open(NULL,f)); }
static void close_file(struct file *f) { assert(!rswap_app_release(NULL,f)); }
static void setup(void) {
    tasks[0].memcg=&a; tasks[1].memcg=&b; tasks[2].memcg=&root;
    for (int i=0;i<3;i++) processes[i+1].task=&tasks[i];
    rswap_app_init(2); rswap_app_area(true);
}
static void registry(void) {
    struct file fa, fb, duplicate;
    struct rswap_app_decision d;
    struct rswap_app_ack ack; loff_t pos=0;
    open_file(&fa); open_file(&fb); open_file(&duplicate);
    assert(bind(&fa,1,RSWAP_APP_DRAM)==16);
    assert(bind(&fb,2,RSWAP_APP_PMEM)==16);
    assert(bind(&duplicate,1,RSWAP_APP_PMEM)==-EBUSY);
    assert(bind(&fa,1,RSWAP_APP_DRAM)==-ESTALE);
    for (unsigned int i=0;i<12;i++) {
        assert(rswap_app_choose(&pa,&d) && d.policy==1 && d.tier==0 && d.server==i%2 && d.rr==i);
        assert(rswap_app_choose(&pb,&d) && d.policy==2 && d.tier==1 && d.server==i%2 && d.rr==i);
        assert(!rswap_app_choose(&outsider,&d));
    }
    /* Membership, not PID: another process's page charge matches the same rule.
     * Reusing the registration PID for another cgroup cannot redirect a rule.
     */
    struct page alias={&a}; tasks[0].memcg=&b;
    assert(rswap_app_choose(&alias,&d) && d.policy==1);
    tasks[0].memcg=&a;
    assert(rswap_app_read(&fa,(void *)&ack,sizeof(ack),&pos)==16 && ack.active && ack.policy==1);
    close_file(&fa); assert(!rswap_app_choose(&pa,&d));
    assert(rswap_app_choose(&pb,&d) && d.policy==2);
    assert(bind(&duplicate,1,RSWAP_APP_DRAM)==16);
    /* swapoff removes rules, but retained owner fds cannot rejoin a later area. */
    rswap_app_area(false); rswap_app_area(true);
    assert(!rswap_app_choose(&pa,&d) && !rswap_app_choose(&pb,&d));
    assert(bind(&duplicate,1,RSWAP_APP_DRAM)==-ESTALE);
    pos=0; assert(rswap_app_read(&fb,(void *)&ack,sizeof(ack),&pos)==16 && !ack.active);
    close_file(&fb); close_file(&duplicate);
    assert(a.css.refs==1 && b.css.refs==1);
    rswap_app_init(1); open_file(&fa); assert(bind(&fa,1,1)==16);
    assert(rswap_app_choose(&pa,&d) && d.server==0 && d.rr==0);
    close_file(&fa); rswap_app_init(2);
}
static void invalid(void) {
    struct file f; struct rswap_app_command c={1,1,1,0}; loff_t pos=0;
    privileged=false; assert(rswap_app_open(NULL,&f)==-EPERM); privileged=true;
    open_file(&f);
    assert(bind(&f,0,1)==-EINVAL && bind(&f,1,3)==-EINVAL && bind(&f,7,1)==-ESRCH);
    assert(bind(&f,3,1)==-EINVAL);
    memcg_disabled=true; assert(bind(&f,1,1)==-EOPNOTSUPP); memcg_disabled=false;
    unified=true; assert(bind(&f,1,1)==-EOPNOTSUPP); unified=false;
    a.css.online=false; assert(bind(&f,1,1)==-ENODEV); a.css.online=true;
    rswap_app_area(false); assert(bind(&f,1,1)==-ENODEV); rswap_app_area(true);
    c.reserved=1; assert(rswap_app_write(&f,(void *)&c,sizeof(c),&pos)==-EINVAL); c.reserved=0;
    c.version=2; assert(rswap_app_write(&f,(void *)&c,sizeof(c),&pos)==-EINVAL); c.version=1;
    assert(rswap_app_write(&f,(void *)&c,sizeof(c)-1,&pos)==-EINVAL);
    copy_fault=true; assert(bind(&f,1,1)==-EFAULT); copy_fault=false;
    privileged=false; assert(bind(&f,1,1)==-EPERM); privileged=true;
    allocation_fail=0; struct file failed; assert(rswap_app_open(NULL,&failed)==-ENOMEM); allocation_fail=-1;
    assert(bind(&f,1,2)==16); close_file(&f);
}
static void fallback(void) {
    struct rswap_pool_layout layout={.nr_servers=2,.total_pages=8,
        .server_pages={4,4},.tier_pages={{2,2},{2,2}}};
    for (unsigned policy=1;policy<=2;policy++) {
        struct file f; struct rswap_app_decision d;
        struct rswap_location loc[8];
        assert(!rswap_mapping_init(&layout)); open_file(&f); assert(bind(&f,1,policy)==16);
        for (u64 off=0;off<8;off++) {
            assert(rswap_app_choose(&pa,&d));
            assert(!rswap_mapping_alloc_preferred(off,d.server,d.tier,&loc[off]));
            assert(loc[off].server==off%2);
            assert(loc[off].tier==(off<4 ? policy-1 : 2-policy));
            rswap_mapping_put(loc[off].id);
        }
        struct rswap_location pin;
        assert(!rswap_mapping_get(0,&pin));
        rswap_mapping_invalidate(0); /* The retired slot still has an in-flight user. */
        assert(rswap_app_choose(&pa,&d));
        assert(rswap_mapping_alloc_preferred(0,d.server,d.tier,&loc[0])==-ENOSPC);
        rswap_mapping_put(pin.id);
        assert(rswap_app_choose(&pa,&d));
        assert(!rswap_mapping_alloc_preferred(0,d.server,d.tier,&loc[0]));
        assert(loc[0].tier==policy-1); rswap_mapping_put(loc[0].id);
        rswap_mapping_reset(); rswap_mapping_destroy(); close_file(&f);
    }
    /* Same-Tier free space is exhausted before any cross-Tier allocation. */
    assert(!rswap_mapping_init(&layout));
    struct rswap_location loc;
    assert(!rswap_mapping_alloc_preferred(0,0,0,&loc)); rswap_mapping_put(loc.id);
    assert(!rswap_mapping_alloc_preferred(1,0,0,&loc)); rswap_mapping_put(loc.id);
    assert(!rswap_mapping_alloc_preferred(2,0,0,&loc) && loc.server==1 && loc.tier==0);
    rswap_mapping_put(loc.id); rswap_mapping_reset(); rswap_mapping_destroy();
}
static atomic_bool stop_readers;
static void *reader(void *unused) {
    while (!atomic_load(&stop_readers)) {
        struct rswap_app_decision d;
        if (rswap_app_choose(&pa,&d)) {
            assert(d.binding_id && d.server<2 && d.policy>=1 && d.policy<=2);
            assert(d.tier==d.policy-1);
        }
    }
    return NULL;
}
static void concurrent(void) {
    pthread_t readers[4];
    for (int i=0;i<4;i++) assert(!pthread_create(&readers[i],NULL,reader,NULL));
    for (int i=0;i<100;i++) {
        struct file f; open_file(&f); assert(bind(&f,1,1+i%2)==16); close_file(&f);
    }
    atomic_store(&stop_readers,true);
    for (int i=0;i<4;i++) assert(!pthread_join(readers[i],NULL));
}
int main(void) {
    setup(); registry(); invalid(); fallback(); concurrent(); rswap_app_area(false);
    assert(a.css.refs==1 && b.css.refs==1 && live_allocations==0);
    for (int i=0;i<3;i++) assert(tasks[i].refs==0 && processes[i+1].refs==0);
    puts("PASS: production memcg matching, independent RR, owner/reset, validation, fallback and concurrent removal");
}
'''


IO_CASES = r'''
int main(void) {
    struct page p, read;
    struct rswap_location saved;
    void *cookie;
    init();
    for (unsigned tier=0;tier<2;tier++) {
        app_override=tier; app_rr=0; policy_override=1-tier;
        for (unsigned off=0;off<8;off++) {
            reset(&p);
            trace_enabled=false;
            assert(!prepare(0,off,&p,0,&cookie));
            struct fs_rdma_req *req=cookie;
            assert(req->tier==tier && req->rdma_queue->rdma_session->server_id==off%2);
            assert(req->policy_reason==RSWAP_REASON_COUNT);
            struct ib_cq *cq=req->rdma_queue->cq;
            atomic_store(&p.valid,1); rswap_store_commit(cookie); complete_one(cq);
            check_tier_stats(off%2,tier,QP_STORE,1,0,0);
            assert(!rswap_mapping_get(off,&saved)); rswap_mapping_put(saved.id);
            /* Load follows an existing mapping after the application rule ends. */
            app_override=-1;
            read=(struct page){0}; atomic_store(&read.refs,1); atomic_store(&read.locked,1);
            assert(!rswap_rdma_submit(0,off,&read,QP_LOAD_SYNC,NULL));
            complete_one(&cqs[off%2][QP_LOAD_SYNC]);
            assert(atomic_load(&read.uptodate));
            check_tier_stats(off%2,tier,QP_LOAD_SYNC,1,0,0);
            rswap_invalidate_page(0,off); app_override=tier;
        }
        /* Pre-post errors retire the allocated slot just as the legacy path. */
        reset(&p); trace_enabled=false; rdma_sessions[0].error=-EIO; app_rr=0;
        assert(prepare(0,9,&p,0,&cookie)==-EIO);
        assert(rswap_mapping_get(9,&saved)==-ENOENT);
        rdma_sessions[0].error=0;
        /* Terminal WC failure uses the existing dirty-page/publication cleanup. */
        reset(&p); trace_enabled=false; app_rr=0; expected_offset=9;
        assert(!prepare(0,9,&p,0,&cookie));
        atomic_store(&p.valid,1); rswap_store_commit(cookie); wc_error=1;
        complete_one(&cqs[0][QP_STORE]);
        check_tier_stats(0,tier,QP_STORE,0,1,0);
        assert(atomic_load(&p.dirty) && !atomic_load(&p.locked) && !atomic_load(&p.valid));
        assert(rswap_mapping_get(9,&saved)==-ENOENT);
        rdma_sessions[0].error=0; wc_error=0;
    }
    assert(!legacy_calls);
    rswap_invalidate_area(0); rswap_mapping_destroy();
    puts("PASS: actual fixed Store, original Load, shared IO statistics and failed-post cleanup");
    return 0;
}
'''


class AppPolicyTests(unittest.TestCase):
    def compile_run(self, code):
        with tempfile.TemporaryDirectory(prefix='hermit-app-policy-') as tmp:
            source, binary = Path(tmp)/'app.c', Path(tmp)/'app'
            source.write_text(code)
            subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-pthread', '-Wall', '-Wextra',
                            '-Werror', '-Wno-unused-parameter', '-Wno-unused-function',
                            '-Wno-unused-const-variable', '-I', str(ROOT/'client'),
                            str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=30)

    def test_production_registry_allocator_and_concurrent_removal(self):
        source = (ROOT/'client/rswap_app.c').read_text()
        production = '\n'.join(line for line in source.splitlines() if not line.startswith('#include'))
        # The list test fixtures already define a trace sink.
        mapping_stubs = mapping_harness.STUBS.replace('#define trace_mapping mock_mapping_trace', '')
        mapping_stubs = mapping_stubs[:mapping_stubs.index('static void mock_mapping_trace')]
        mapping_stubs = '\n#undef kvfree\n' + mapping_stubs
        code = (placement_harness.STUBS + APP_STUBS + production + mapping_stubs
                + mapping_harness.production() + APP_CASES)
        self.compile_run(code)

    def test_production_rdma_fixed_store_and_load_lifetime(self):
        code=client_code(SUPPORT.replace('int main(void)', 'int legacy_main(void)'))
        for name in ('rswap_policy_record_alloc','rswap_policy_record_submit','rswap_policy_record_wc'):
            old=definition(code,name)
            code=code.replace(old,old.replace('{}','{ legacy_calls++; }'))
        self.compile_run('static unsigned int legacy_calls;\n'+code+IO_CASES)


if __name__ == '__main__':
    unittest.main()

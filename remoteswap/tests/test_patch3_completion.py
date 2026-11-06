#!/usr/bin/env python3
"""Exercise actual Client functions with userspace RDMA/page stubs, not hardware."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def definition(source, name, struct=False):
    if struct:
        start = source.index(f"struct {name} {{")
    else:
        match = re.search(r"^(?:(?:static|inline)\s+)*(?:void\s+|int\s+|struct page\s*\*\s*)"
                          + re.escape(name) + r"\(", source, re.MULTILINE)
        if match is None:
            raise ValueError(f"Function definition not found: {name}")
        start = match.start()
    start = source.rfind("\n", 0, start) + 1
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + (";\n" if struct else "\n")


STUBS = r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdatomic.h>
#include <pthread.h>
#include <sched.h>
typedef uint64_t u64;
typedef uint16_t u16;
typedef unsigned long pgoff_t;
typedef atomic_int atomic_t;
typedef atomic_int refcount_t;
typedef atomic_llong atomic64_t;
#define atomic64_inc(p) ((void)atomic_fetch_add(p, 1))
#define atomic64_set(p, value) atomic_store(p, value)
#define atomic64_read(p) atomic_load(p)
#include "rswap_mapping.h"
#define PAGE_SHIFT 12
#define PAGE_SIZE 4096ULL
#define CHUNK_SHIFT 33
#define CHUNK_MASK ((1ULL << CHUNK_SHIFT) - 1)
#define U64_MAX UINT64_MAX
#define RDMA_SEND_QUEUE_DEPTH 1024
#define IB_POLL_DIRECT 0
#define IB_POLL_SOFTIRQ 1
#define IB_WC_SUCCESS 0
#define IB_WR_RDMA_WRITE 1
#define IB_WR_RDMA_READ 2
#define IB_SEND_SIGNALED 1
#define GFP_ATOMIC 0
#define WARN_ON(x) (!!(x))
#define READ_ONCE(x) __atomic_load_n(&(x), __ATOMIC_ACQUIRE)
#define smp_store_release(p, v) __atomic_store_n(p, v, __ATOMIC_RELEASE)
#define smp_load_acquire(p) __atomic_load_n(p, __ATOMIC_ACQUIRE)
#define check_add_overflow(a, b, out) __builtin_add_overflow(a, b, out)
#define container_of(p, type, member) ((type *)((char *)(p) - offsetof(type, member)))
#define atomic_read(p) atomic_load(p)
#define atomic_dec(p) ((void)atomic_fetch_sub(p, 1))
#define atomic_inc(p) ((void)atomic_fetch_add(p, 1))
#define WRITE_ONCE(x, v) __atomic_store_n(&(x), v, __ATOMIC_RELEASE)
#define cond_resched() sched_yield()
static int atomic_cmpxchg(atomic_t *p, int old, int value) {
    atomic_compare_exchange_strong(p, &old, value); return old;
}
#define refcount_set(p, n) atomic_store(p, n)
#define refcount_dec_and_test(p) (atomic_fetch_sub(p, 1) == 1)
#define spin_lock_irqsave(p, flags) do { (flags) = 0; pthread_mutex_lock(p); } while (0)
#define spin_unlock_irqrestore(p, flags) do { (void)(flags); pthread_mutex_unlock(p); } while (0)
#define trace_printk(...) ((void)0)
#define pr_err_ratelimited(...) ((void)0)
#define wake_up_all(p) ((void)(p))
#define cpu_relax() sched_yield()
#define in_atomic() false
#define irqs_disabled() false
#define in_softirq() false
#define wait_event_timeout(sem, condition, timeout) ((condition) ? 0 : mock_softirq(queue))
enum dma_data_direction { DMA_TO_DEVICE, DMA_FROM_DEVICE };
enum rdma_queue_type { QP_STORE, QP_LOAD_SYNC, QP_LOAD_ASYNC, NUM_QP_TYPE };
enum chunk_mapping_state { EMPTY, MAPPED };
struct page {
    atomic_int refs, locked, valid, dirty, error, uptodate, dma, reclaim;
    bool compound;
};
#define PageCompound(p) ((p)->compound)
#define SetPageUptodate(p) atomic_store(&(p)->uptodate, 1)
#define ClearPageUptodate(p) atomic_store(&(p)->uptodate, 0)
#define SetPageError(p) atomic_store(&(p)->error, 1)
#define ClearPageError(p) atomic_store(&(p)->error, 0)
#define ClearPageReclaim(p) atomic_store(&(p)->reclaim, 0)
static void set_page_dirty(struct page *p) { atomic_store(&p->dirty, 1); }
static void get_page(struct page *p) { atomic_fetch_add(&p->refs, 1); }
static void put_page(struct page *p) { assert(atomic_fetch_sub(&p->refs, 1) > 1); }
static void unlock_page(struct page *p) {
    assert(!atomic_load(&p->dma)); assert(atomic_exchange(&p->locked, 0) == 1);
}
struct ib_device { int id; };
struct ib_pd { unsigned int local_dma_lkey; };
struct rswap_rdma_dev { struct ib_device *dev; struct ib_pd *pd; };
struct ib_cq;
struct ib_wc;
struct ib_cqe { void (*done)(struct ib_cq *, struct ib_wc *); };
struct ib_sge { u64 addr; unsigned int length, lkey; };
struct ib_send_wr {
    struct ib_send_wr *next;
    struct ib_cqe *wr_cqe;
    struct ib_sge *sg_list;
    int num_sge, opcode, send_flags;
};
struct ib_rdma_wr { struct ib_send_wr wr; u64 remote_addr; unsigned int rkey; };
struct ib_wc { struct ib_cqe *wr_cqe; int status; };
struct fs_rdma_req;
struct rswap_rdma_queue;
struct ib_cq { int poll_ctx; bool hold; struct fs_rdma_req *queued; };
struct ib_qp { struct rswap_rdma_queue *queue; };
struct remote_chunk { uint32_t remote_rkey; uint64_t remote_addr, mapped_size; enum chunk_mapping_state chunk_state; };
struct chunk_list { struct remote_chunk *chunks; unsigned int chunk_num; };
/* STATS_STRUCT */
struct rdma_session_context {
    unsigned int server_id;
    struct rswap_io_stats io_stats[NUM_QP_TYPE];
    bool stopping;
    int error;
    struct rswap_rdma_dev *rdma_dev;
    struct rswap_rdma_queue *rdma_queues;
    struct chunk_list remote_mem_pool;
};
struct rswap_rdma_queue {
    struct rdma_session_context *rdma_session;
    struct ib_cq *cq;
    struct ib_qp *qp;
    pthread_mutex_t cq_lock;
    atomic_t rdma_post_counter;
    atomic_t store_pending;
    enum rdma_queue_type type;
    int q_index, sem;
    void *fs_rdma_req_cache;
};
static struct rdma_session_context rdma_sessions[2];
static struct rswap_pool_layout pool_layout;
static unsigned int active_servers = 2, area_type;
static bool area_active = true;
static pthread_mutex_t area_lock = PTHREAD_MUTEX_INITIALIZER;
static atomic_t area_submissions;
static atomic64_t unrouted_error;
static int online_cores = 1, num_queues = 3;
static atomic_int frees, maps, unmaps;
static int alloc_error, map_error, post_error, wc_error, immediate, pins;
static struct page *current_page;
static void fs_rdma_callback(struct ib_cq *, struct ib_wc *);
static void *kmem_cache_zalloc(void *cache, int flags);
static void kmem_cache_free(void *cache, void *ptr) { atomic_fetch_add(&frees, 1); free(ptr); }
static int get_cpu(void) { pins++; return 0; }
static void put_cpu(void) { assert(pins-- > 0); }
static u64 ktime_get_ns(void) { return 1; }
static void rswap_rdma_error(struct rswap_rdma_queue *q, int error) { q->rdma_session->error = error; }
static void frontswap_invalidate_page(unsigned int type, pgoff_t offset) {
    assert(type == 0 && offset == 1);
    assert(atomic_load(&current_page->dirty));
    assert(atomic_exchange(&current_page->valid, 0) == 1);
}
static struct rswap_rdma_queue *get_rdma_queue(struct rdma_session_context *s, unsigned int cpu, enum rdma_queue_type type) {
    assert(cpu == 0); return &s->rdma_queues[type];
}
static u64 ib_dma_map_page(struct ib_device *dev, struct page *p, int offset, u64 size, enum dma_data_direction dir) {
    if (map_error) return 0;
    assert(size == PAGE_SIZE && !atomic_exchange(&p->dma, 1));
    atomic_fetch_add(&maps, 1); return (uintptr_t)p;
}
static bool ib_dma_mapping_error(struct ib_device *dev, u64 addr) { return addr == 0; }
static void ib_dma_unmap_page(struct ib_device *dev, u64 addr, u64 size, enum dma_data_direction dir) {
    struct page *p = (void *)(uintptr_t)addr;
    assert(size == PAGE_SIZE && atomic_exchange(&p->dma, 0) == 1);
    atomic_fetch_add(&unmaps, 1);
}
static int ib_process_cq_direct(struct ib_cq *cq, int budget);
static int mock_softirq(struct rswap_rdma_queue *queue);
static int ib_post_send(struct ib_qp *qp, struct ib_send_wr *wr, const struct ib_send_wr **bad);
"""

SUPPORT = r"""
static void *kmem_cache_zalloc(void *cache, int flags) {
    return alloc_error ? NULL : calloc(1, sizeof(struct fs_rdma_req));
}
static void complete_one(struct ib_cq *cq) {
    struct fs_rdma_req *req = cq->queued;
    struct ib_wc wc = {.wr_cqe = &req->cqe, .status = wc_error};
    cq->queued = NULL;
    fs_rdma_callback(cq, &wc);
}
static int ib_process_cq_direct(struct ib_cq *cq, int budget) {
    assert(cq->poll_ctx == IB_POLL_DIRECT);
    if (!cq->queued || cq->hold) return 0;
    complete_one(cq); return 1;
}
static int mock_softirq(struct rswap_rdma_queue *queue) {
    assert(queue->cq->poll_ctx == IB_POLL_SOFTIRQ && queue->cq->queued);
    complete_one(queue->cq); return 1;
}
static int ib_post_send(struct ib_qp *qp, struct ib_send_wr *wr, const struct ib_send_wr **bad) {
    struct fs_rdma_req *req = container_of(wr, struct fs_rdma_req, rdma_wr.wr);
    struct rswap_rdma_queue *q = qp->queue;
    assert(req->rdma_queue == q);
    assert(req->sge.lkey == q->rdma_session->rdma_dev->pd->local_dma_lkey);
    assert(req->rdma_wr.rkey == 1000 + q->rdma_session->server_id);
    if (post_error) return post_error;
    assert(!q->cq->queued);
    q->cq->queued = req;
    if (immediate) complete_one(q->cq);
    return 0;
}
static struct rswap_rdma_queue queues[2][3];
static struct ib_cq cqs[2][3];
static struct ib_qp qps[2][3];
static struct remote_chunk chunks[2][2];
static struct ib_device devices[2];
static struct ib_pd pds[2];
static struct rswap_rdma_dev devs[2];
static void init(void) {
    for (int s = 0; s < 2; s++) {
        devices[s].id = s; pds[s].local_dma_lkey = 100 + s;
        devs[s].dev = &devices[s]; devs[s].pd = &pds[s];
        rdma_sessions[s].server_id = s;
        rdma_sessions[s].rdma_dev = &devs[s];
        rdma_sessions[s].rdma_queues = queues[s];
        rdma_sessions[s].remote_mem_pool.chunks = chunks[s];
        rdma_sessions[s].remote_mem_pool.chunk_num = s + 1;
        for (int c = 0; c < 2; c++)
            chunks[s][c] = (struct remote_chunk){1000 + s, 0x100000ULL + (s * 2ULL + c) * (1ULL << 34), 1ULL << 33, MAPPED};
        for (int t = 0; t < 3; t++) {
            queues[s][t].rdma_session = &rdma_sessions[s];
            queues[s][t].cq = &cqs[s][t]; queues[s][t].qp = &qps[s][t];
            queues[s][t].type = t; queues[s][t].q_index = t;
            cqs[s][t].poll_ctx = t == QP_LOAD_ASYNC ? IB_POLL_SOFTIRQ : IB_POLL_DIRECT;
            qps[s][t].queue = &queues[s][t];
            pthread_mutex_init(&queues[s][t].cq_lock, NULL);
        }
    }
    pool_layout = (struct rswap_pool_layout){{1ULL << 21, 1ULL << 22}, 3ULL << 21, 2};
}
static void reset(struct page *p) {
    *p = (struct page){0};
    atomic_store(&p->refs, 1); atomic_store(&p->locked, 1);
    atomic_store(&p->reclaim, 1); current_page = p;
    alloc_error = map_error = post_error = wc_error = immediate = pins = 0;
    atomic_store(&frees, 0); atomic_store(&maps, 0); atomic_store(&unmaps, 0);
    atomic64_set(&unrouted_error, 0);
    for (int s = 0; s < 2; s++) {
        rdma_sessions[s].error = 0; rdma_sessions[s].stopping = false;
        for (int t = 0; t < 3; t++) {
            atomic64_set(&rdma_sessions[s].io_stats[t].ok, 0);
            atomic64_set(&rdma_sessions[s].io_stats[t].wc_error, 0);
            atomic64_set(&rdma_sessions[s].io_stats[t].submit_error, 0);
            assert(!atomic_load(&queues[s][t].rdma_post_counter));
            assert(!atomic_load(&queues[s][t].store_pending));
            assert(!cqs[s][t].queued); cqs[s][t].hold = false;
        }
    }
}
static void retired(struct page *p, bool unlocked) {
    assert(atomic_load(&p->refs) == 1);
    assert(atomic_load(&p->locked) == !unlocked);
    assert(atomic_load(&frees) == 1);
    assert(atomic_load(&maps) == atomic_load(&unmaps));
    assert(!atomic_load(&queues[1][0].rdma_post_counter));
    assert(!pins);
}
static void check_stats(int server, int type, int ok, int wc_error_count, int submit_error) {
    struct rswap_io_stats *s = &rdma_sessions[server].io_stats[type];
    assert(atomic64_read(&s->ok) == ok);
    assert(atomic64_read(&s->wc_error) == wc_error_count);
    assert(atomic64_read(&s->submit_error) == submit_error);
}
static void *callback_thread(void *arg) { complete_one(&cqs[1][0]); return NULL; }
static void *commit_thread(void *arg) {
    atomic_store(&current_page->valid, 1);
    rswap_store_commit(arg); return NULL;
}
static void *invalidate_thread(void *arg) { rswap_invalidate_area(0); return NULL; }
int main(void) {
    struct page p;
    void *cookie;
    struct rswap_remote_page remote;
    init();
    /* Actual resolver: tail, descriptor limits and address overflow. */
    assert(!rswap_resolve_remote_page(1ULL << 22, &remote));
    assert(remote.session == &rdma_sessions[1] && remote.chunk == &chunks[1][1] && !remote.chunk_offset);
    assert(rswap_resolve_remote_page(pool_layout.total_pages, &remote) == -ERANGE);
    chunks[1][0].chunk_state = EMPTY;
    assert(rswap_resolve_remote_page(1, &remote) == -ENXIO);
    chunks[1][0].chunk_state = MAPPED;
    chunks[1][0].mapped_size = PAGE_SIZE - 1;
    assert(rswap_resolve_remote_page(1, &remote) == -ERANGE);
    chunks[1][0].mapped_size = 1ULL << 33;
    chunks[1][0].remote_addr = UINT64_MAX - PAGE_SIZE + 1;
    assert(rswap_resolve_remote_page(1, &remote) == -EOVERFLOW);
    chunks[1][0].remote_addr = 0x100000ULL + 2 * (1ULL << 34);

    for (int before = 0; before < 2; before++) {
        for (int error = 0; error < 2; error++) {
            reset(&p); wc_error = error; immediate = before;
            assert(!rswap_store_prepare(0, 1, &p, 0, &cookie));
            assert(atomic_load(&p.locked) && !atomic_load(&frees));
            if (before) {
                assert(!atomic_load(&queues[1][0].rdma_post_counter));
                assert(atomic_load(&queues[1][0].store_pending) == 1);
            }
            atomic_store(&p.valid, 1); rswap_store_commit(cookie);
            if (!before) complete_one(&cqs[1][0]);
            retired(&p, true);
            check_stats(1, QP_STORE, !error, error, 0);
            check_stats(0, QP_STORE, 0, 0, 0);
            assert(atomic_load(&p.valid) == !error);
            assert(atomic_load(&p.dirty) == error);
        }
    }
    /* Real simultaneous publication/completion, both success and error. */
    for (int i = 0; i < 200; i++) {
        pthread_t cb, commit;
        reset(&p); wc_error = i & 1;
        assert(!rswap_store_prepare(0, 1, &p, 0, &cookie));
        assert(!pthread_create(&cb, NULL, callback_thread, NULL));
        assert(!pthread_create(&commit, NULL, commit_thread, cookie));
        pthread_join(cb, NULL); pthread_join(commit, NULL);
        retired(&p, true);
        check_stats(1, QP_STORE, !(i & 1), i & 1, 0);
    }
    {
        pthread_t drain;
        reset(&p);
        assert(!rswap_store_prepare(0, 1, &p, 0, &cookie));
        assert(!pthread_create(&drain, NULL, invalidate_thread, NULL));
        while (READ_ONCE(area_active)) sched_yield();
        assert(rswap_rdma_submit(0, 2, &p, QP_LOAD_SYNC, NULL) == -ESHUTDOWN);
        atomic_store(&p.valid, 1); rswap_store_commit(cookie);
        pthread_join(drain, NULL);
        assert(!atomic_load(&area_submissions));
        retired(&p, true);
        assert(atomic64_read(&unrouted_error) == 1);
        check_stats(1, QP_STORE, 1, 0, 0);
        rswap_frontswap_init(0);
        check_stats(1, QP_STORE, 1, 0, 0); /* swapon must not reset counters */
    }
    for (int error = 0; error < 2; error++) {
        reset(&p); wc_error = error;
        assert(rswap_store_prepare(0, 1, &p, -1, &cookie) == (error ? -EIO : 0));
        assert(atomic_load(&p.locked));
        if (!error) { atomic_store(&p.valid, 1); rswap_store_commit(cookie); }
        retired(&p, !error);
        check_stats(1, QP_STORE, !error, error, 0); /* WC failure is not submit failure */
    }
    for (int failure = 0; failure < 4; failure++) {
        reset(&p);
        if (failure == 0) alloc_error = 1;
        if (failure == 1) map_error = 1;
        if (failure == 2) post_error = -EIO;
        if (failure == 3) p.compound = true;
        assert(rswap_store_prepare(0, 1, &p, -1, &cookie) < 0 && !cookie);
        assert(atomic_load(&p.locked) && atomic_load(&p.refs) == 1 && !pins);
        assert(!atomic_load(&queues[1][0].rdma_post_counter));
        assert(atomic_load(&maps) == atomic_load(&unmaps));
        assert(atomic_load(&frees) == (failure == 1 || failure == 2));
        check_stats(1, QP_STORE, 0, 0, failure != 3);
        assert(atomic64_read(&unrouted_error) == (failure == 3));
    }
    /* Node 0 empty, node 1 pending: CPU-only polling must still complete it. */
    reset(&p);
    assert(!rswap_store_prepare(0, 1, &p, 0, &cookie));
    atomic_store(&p.valid, 1); rswap_store_commit(cookie);
    cqs[1][0].hold = true;
    assert(rswap_poll_cpu(0, QP_STORE, true) == 1);
    cqs[1][0].hold = false;
    assert(rswap_poll_cpu(0, QP_STORE, false) == 0);
    retired(&p, true);
    {
        struct page other = {0};
        void *second;
        reset(&p);
        atomic_store(&other.refs, 1); atomic_store(&other.locked, 1);
        assert(!rswap_store_prepare(0, 1, &p, 0, &cookie));
        assert(!rswap_store_prepare(0, 2, &other, 0, &second));
        atomic_store(&p.valid, 1); rswap_store_commit(cookie);
        atomic_store(&other.valid, 1); rswap_store_commit(second);
        cqs[0][0].hold = true;
        assert(rswap_poll_cpu(0, QP_STORE, true) == 1);
        assert(!atomic_load(&p.locked) && atomic_load(&other.locked));
        cqs[0][0].hold = false;
        assert(!rswap_poll_cpu(0, QP_STORE, true));
        assert(!atomic_load(&other.locked) && atomic_load(&frees) == 2);
        assert(atomic_load(&p.refs) == 1 && atomic_load(&other.refs) == 1);
        check_stats(0, QP_STORE, 1, 0, 0);
        check_stats(1, QP_STORE, 1, 0, 0);
    }
    /* Fast demand completion keeps the lock; asynchronous CQ owns its unlock. */
    for (int type = QP_LOAD_SYNC; type <= QP_LOAD_ASYNC; type++) {
        for (int error = 0; error < 2; error++) {
            reset(&p); wc_error = error;
            immediate = type == QP_LOAD_SYNC;
            assert(!rswap_rdma_submit(0, 1, &p, type, NULL));
            if (type == QP_LOAD_ASYNC) drain_all_rdma_queues(1);
            assert(atomic_load(&p.uptodate) == !error);
            assert(atomic_load(&p.error) == error);
            retired(&p, type == QP_LOAD_ASYNC);
            check_stats(1, type, !error, error, 0);
        }
    }
    /* All three operations: rejected sends count once, without a WC. */
    for (int type = QP_STORE; type <= QP_LOAD_ASYNC; type++) {
        for (int failure = 0; failure < 4; failure++) {
            reset(&p);
            if (failure == 0) alloc_error = 1;
            if (failure == 1) map_error = 1;
            if (failure == 2) post_error = -EIO;
            if (failure == 3) rdma_sessions[1].error = -ECONNRESET;
            int ret = type == QP_STORE ? rswap_store_prepare(0, 1, &p, 0, &cookie) :
                                        rswap_rdma_submit(0, 1, &p, type, NULL);
            assert(ret < 0 && atomic_load(&p.locked) && atomic_load(&p.refs) == 1);
            assert(atomic_load(&maps) == atomic_load(&unmaps));
            check_stats(1, type, 0, 0, 1);
            check_stats(0, type, 0, 0, 0);
            assert(!atomic64_read(&unrouted_error));
        }
    }
    reset(&p);
    assert(rswap_rdma_submit(0, pool_layout.total_pages, &p, QP_LOAD_SYNC, NULL) == -ERANGE);
    assert(atomic64_read(&unrouted_error) == 1);
    check_stats(0, QP_LOAD_SYNC, 0, 0, 0);
    check_stats(1, QP_LOAD_SYNC, 0, 0, 0);
    puts("PASS: actual resolver, publication races, failures, polling, read completion and per-server stats");
    return 0;
}
"""

KERNEL_STUBS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <errno.h>
typedef unsigned long pgoff_t;
typedef uint64_t swp_entry_val_t;
typedef struct { swp_entry_val_t val; } swp_entry_t;
struct page { unsigned long private; int locked, dirty, compound, nr; };
struct swap_info_struct { bool map[64]; };
struct frontswap_ops {
    void (*invalidate_page)(unsigned int, pgoff_t);
    int (*store)(unsigned int, pgoff_t, struct page *);
    int (*store_on_core)(unsigned int, pgoff_t, struct page *, int);
    int (*store_prepare)(unsigned int, pgoff_t, struct page *, int, void **);
    void (*store_commit)(void *);
    int (*load)(unsigned int, pgoff_t, struct page *);
    int (*load_async)(unsigned int, pgoff_t, struct page *);
    struct frontswap_ops *next;
};
#define FRONTSWAP_LOAD_ERROR (-EREMOTEIO)
#define VM_BUG_ON(x) assert(!(x))
#define BUG_ON(x) assert(!(x))
#define PageLocked(p) ((p)->locked)
#define PageCompound(p) ((p)->compound)
#define thp_nr_pages(p) ((unsigned long)(p)->nr)
#define inline
#define SetPageDirty(p) ((p)->dirty = 1)
#define page_private(p) ((p)->private)
#define swp_type(e) 0
#define swp_offset(e) ((e).val)
#define for_each_frontswap_ops(p) for ((p) = frontswap_ops; (p); (p) = (p)->next)
#define EXPORT_SYMBOL(x)
#define adc_pf_breakdown_stt(...) ((void)0)
#define adc_pf_breakdown_end(...) ((void)0)
static bool managed_owner = true, frontswap_writethrough_enabled;
static bool frontswap_tmem_exclusive_gets_enabled;
static int frontswap_srcu, srcu_depth;
static struct swap_info_struct sis;
static struct swap_info_struct *swap_info[] = {&sis};
static struct frontswap_ops ops;
static struct frontswap_ops *frontswap_ops = &ops;
static int store_result, load_result, commits, invalidates, loads, polls;
static int succ_stores, failed_stores;
static int srcu_read_lock(int *s) { return srcu_depth++; }
static void srcu_read_unlock(int *s, int idx) { assert(srcu_depth-- > 0); }
static bool __frontswap_test(struct swap_info_struct *s, pgoff_t offset) { return s->map[offset]; }
static void __frontswap_set(struct swap_info_struct *s, pgoff_t offset) { s->map[offset] = true; }
static void __frontswap_clear(struct swap_info_struct *s, pgoff_t offset) { s->map[offset] = false; }
static void inc_frontswap_invalidates(void) { invalidates++; }
static void inc_frontswap_succ_stores(void) { succ_stores++; }
static void inc_frontswap_failed_stores(void) { failed_stores++; }
static void inc_frontswap_loads(void) { loads++; }
static void frontswap_poll_load(int cpu) { assert(cpu >= 0); polls++; }
static int prepare(unsigned int type, pgoff_t offset, struct page *p, int core, void **cookie) {
    assert(srcu_depth && p->locked && !sis.map[offset]);
    if (!store_result) *cookie = p;
    return store_result;
}
static void commit(void *cookie) {
    struct page *p = cookie;
    assert(srcu_depth && sis.map[p->private] && p->locked);
    commits++; p->locked = 0;
}
static int load(unsigned int type, pgoff_t offset, struct page *p) { return load_result; }
static void invalidate(unsigned int type, pgoff_t offset) {}
static void unlock_page(struct page *p) { assert(p->locked); p->locked = 0; }
"""

KERNEL_CASES = r"""
int main(void) {
    struct page p = {.private = 4, .locked = 1, .nr = 1};
    ops.store_prepare = prepare; ops.store_commit = commit;
    ops.load = ops.load_async = load; ops.invalidate_page = invalidate;
    assert(__frontswap_store(&p, -1) == 0);
    assert(commits == 1 && sis.map[4] && !p.locked && !srcu_depth);
    p.locked = 1; store_result = -ENOMEM;
    assert(__frontswap_store(&p, 0) == -ENOMEM);
    assert(commits == 1 && !sis.map[4] && p.locked && !srcu_depth);
    assert(__frontswap_load(&p, true) == -1); /* a real disk-backed miss */
    sis.map[4] = true; load_result = -ENOMEM;
    assert(__frontswap_load(&p, true) == FRONTSWAP_LOAD_ERROR);
    assert(__frontswap_load(&p, false) == FRONTSWAP_LOAD_ERROR);
    assert(sis.map[4] && !loads && p.locked && !srcu_depth);
    load_result = 0;
    assert(!__frontswap_load(&p, true) && loads == 1 && sis.map[4]);
    /* A THP cannot fall back to disk if only a tail constituent is remote. */
    p.compound = 1; p.nr = 4; sis.map[4] = false; sis.map[6] = true;
    assert(__frontswap_load(&p, true) == FRONTSWAP_LOAD_ERROR);
    sis.map[6] = false;
    assert(__frontswap_load(&p, true) == -1);
    for (int i = 4; i < 8; i++) sis.map[i] = true;
    assert(__frontswap_store(&p, 0) == -EOPNOTSUPP);
    for (int i = 4; i < 8; i++) assert(!sis.map[i]);
    assert(p.locked && commits == 1 && !srcu_depth);
    /* A disk read keeps its own completion lock; an RDMA read is polled. */
    assert(!hermit_poll_read(-1, &p, true, NULL));
    assert(p.locked && !polls);
    assert(!hermit_poll_read(0, &p, false, NULL));
    assert(p.locked && polls == 1);
    assert(!hermit_poll_read(0, &p, true, NULL));
    assert(!p.locked && polls == 2);
    puts("PASS: actual Frontswap publication, owned-copy errors, THP and disk/RDMA lock split");
    return 0;
}
"""


SPECULATIVE_STUBS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <errno.h>
typedef unsigned int gfp_t;
typedef struct { unsigned long val; } swp_entry_t;
struct page { int locked, pending, refs; };
struct vm_area_struct { void *vm_mm; };
#define ADC_PF_MAJOR_BIT 0
#define ADC_ONDEMAND_SWAPIN 0
#define ADC_OPTIM_FAILED 0
#define GFP_RECLAIM_MASK 0
#define set_adc_pf_bits(...) ((void)0)
#define adc_profile_counter_inc(...) ((void)0)
#define adc_pf_breakdown_stt(...) ((void)0)
#define adc_pf_breakdown_end(...) ((void)0)
#define BUG_ON(x) assert(!(x))
#define cond_resched() ((void)0)
#define swap_address_space(e) NULL
#define swp_offset(e) ((e).val)
#define inline
static struct page fresh, cached;
/* 0 = RDMA, 1 = already-completed disk, 2 = pending disk. */
static int mode, scenario, polls, locks, unlocks, page_puts, insertions, releases;
static int lookups, prepares, swap_slot_cache_enabled = 1;
static struct page *alloc_page_vma(gfp_t g, struct vm_area_struct *v, unsigned long a) {
    return scenario == 7 ? NULL : &fresh;
}
static struct page *find_get_page(void *mapping, unsigned long offset) {
    lookups++;
    return scenario == 3 || scenario == 7 ? &cached : NULL;
}
static int hermit_issue_read(struct page *p, swp_entry_t entry) {
    assert(!p->locked); p->locked = 1;
    if (mode == 1) p->locked = 0;
    else p->pending = 1;
    return mode == 0 ? 0 : -1;
}
static void lock_page(struct page *p) {
    /* A filesystem readpage may return before its unlock completion. */
    assert(mode != 0);
    if (p->pending) { assert(p->locked); p->pending = 0; p->locked = 0; }
    assert(!p->locked); p->locked = 1; locks++;
}
static void unlock_page(struct page *p) {
    assert(p->locked && !p->pending); p->locked = 0; unlocks++;
}
static void frontswap_poll_load(int cpu) {
    assert(mode == 0 && cpu == 0 && fresh.locked && fresh.pending);
    fresh.pending = 0; polls++;
}
static void put_page(struct page *p) {
    assert(p == &fresh && p->refs == 1 && !p->locked && !p->pending);
    p->refs--; page_puts++;
}
static int __swp_swapcount(swp_entry_t entry) { return scenario != 4; }
static int swapcache_prepare(swp_entry_t entry) {
    prepares++;
    if (scenario == 5) return -ENOMEM;
    if (scenario == 6 && prepares == 1) return -EEXIST;
    return 0;
}
static int hermit_mem_cgroup_swapin_charge_page(struct page *p, void *mm, gfp_t g,
                                                int *bits, uint64_t *breakdown) {
    assert(p->locked); return scenario == 1 ? -ENOMEM : 0;
}
static int add_to_swap_cache(struct page *p, swp_entry_t entry, gfp_t g, void **shadow) {
    assert(p->locked); insertions++; return scenario == 2 ? -ENOMEM : 0;
}
static unsigned long hermit_workingset_refault(struct page *p, void *shadow) { return 0; }
static void hmt_update_rft_dist(struct page *p, unsigned long dist) {}
static void lru_cache_add(struct page *p) { assert(p->locked); }
static void put_swap_page(struct page *p, swp_entry_t entry) {
    assert(p->locked && !p->pending); releases++;
}
"""

SPECULATIVE_CASES = r"""
int main(void) {
    struct vm_area_struct vma = {0};
    swp_entry_t entry = {1};
    for (mode = 0; mode < 3; mode++) {
        for (scenario = 0; scenario < 8; scenario++) {
            int cpu = -1;
            bool allocated = true;
            fresh = (struct page){.refs = 1};
            polls = locks = unlocks = page_puts = insertions = releases = lookups = prepares = 0;
            struct page *p = __read_swap_cache_speculative(entry, 0, &vma, 0,
                                                          &allocated, &cpu, NULL, NULL);
            if (scenario == 7) {
                assert(p == &cached && !allocated && cpu == -1 && !page_puts && !locks);
                continue;
            }
            assert(cpu == (mode == 0 ? 0 : -1));
            if (scenario == 0 || scenario == 6) {
                assert(p == &fresh && allocated && insertions == 1 && !page_puts && !releases);
                assert(fresh.locked == (mode == 0));
                hermit_poll_read(cpu, p, true, NULL);
                assert(!fresh.locked && !fresh.pending);
                if (scenario == 6) assert(prepares == 2);
            } else {
                assert(p == (scenario == 3 ? &cached : NULL));
                assert(!allocated && page_puts == 1 && fresh.refs == 0);
                assert(releases == (scenario == 1 || scenario == 2));
            }
            assert(polls == (mode == 0));
            assert(locks == (mode != 0) && unlocks == 1);
        }
    }
    puts("PASS: actual speculative helper, RDMA/disk locks and all cleanup paths (24 cases)");
    return 0;
}
"""


class CompletionTests(unittest.TestCase):
    def test_actual_client_functions(self):
        source = (ROOT / "client/rswap_rdma_ops.c").read_text()
        header = (ROOT / "client/rswap_rdma.h").read_text()
        stats = (ROOT / "client/rswap_stats.c").read_text()
        names = ["rswap_poll_queue", "rswap_wait_progress", "drain_rdma_queue",
                 "drain_all_rdma_queues", "rswap_request_done", "rswap_store_put",
                 "fs_rdma_callback", "fs_enqueue_send_wr", "fs_build_rdma_wr",
                 "rswap_resolve_remote_page", "rswap_rdma_submit_active", "rswap_rdma_submit",
                 "rswap_store_prepare", "rswap_store_commit", "rswap_poll_cpu",
                 "rswap_invalidate_area", "rswap_frontswap_init"]
        stubs = STUBS.replace("/* STATS_STRUCT */", definition(header, "rswap_io_stats", struct=True))
        code = (stubs + definition(header, "fs_rdma_req", struct=True)
                + definition(source, "rswap_remote_page", struct=True)
                + definition(stats, "rswap_stats_wc") + definition(stats, "rswap_stats_submit_error")
                + "\n".join(definition(source, name) for name in names) + SUPPORT)
        with tempfile.TemporaryDirectory(prefix="hermit-patch3-completion-") as tmp:
            c = Path(tmp) / "completion.c"
            binary = Path(tmp) / "completion"
            c.write_text(code)
            subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-pthread",
                            "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                            "-I", str(ROOT / "client"), str(c), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=20)

    def test_actual_frontswap_contract(self):
        kernel = ROOT.parent / "linux-5.14-rc5/mm"
        fs = (kernel / "frontswap.c").read_text()
        io = (kernel / "page_io.c").read_text()
        code = (KERNEL_STUBS + definition(fs, "__frontswap_invalidate_page")
                + definition(fs, "__frontswap_store") + definition(fs, "__frontswap_load")
                + definition(io, "hermit_poll_read") + KERNEL_CASES)
        with tempfile.TemporaryDirectory(prefix="hermit-patch3-frontswap-") as tmp:
            c = Path(tmp) / "frontswap.c"
            binary = Path(tmp) / "frontswap"
            c.write_text(code)
            subprocess.run(["gcc", "-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", str(c), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_actual_speculative_disk_fallback(self):
        kernel = ROOT.parent / "linux-5.14-rc5/mm"
        state = (kernel / "swap_state.c").read_text()
        io = (kernel / "page_io.c").read_text()
        code = (SPECULATIVE_STUBS + definition(io, "hermit_poll_read")
                + definition(state, "__read_swap_cache_speculative") + SPECULATIVE_CASES)
        with tempfile.TemporaryDirectory(prefix="hermit-patch3-speculative-") as tmp:
            c = Path(tmp) / "speculative.c"
            binary = Path(tmp) / "speculative"
            c.write_text(code)
            subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", str(c), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()

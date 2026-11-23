"""Exercise the production dense allocator, including full-pool and late I/O."""
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

import mapping_harness

PREFIX = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <sched.h>
typedef uint64_t u64;
typedef uint16_t u16;
#include "rswap_mapping.h"
static int warn_on(int x) { assert(!x); return x; }
#define WARN_ON(x) warn_on(!!(x))
#define spin_lock_irqsave(p, flags) do { (flags) = 0; pthread_mutex_lock(p); } while (0)
#define spin_unlock_irqrestore(p, flags) do { (void)(flags); pthread_mutex_unlock(p); } while (0)
#define cond_resched() sched_yield()
"""

CASES = r"""
static int emit;
static u64 last_seq;
static void mock_mapping_trace(u64 seq, unsigned int action, u64 offset,
                               unsigned int server, unsigned int tier, u64 slot,
                               unsigned int users, unsigned int live, unsigned int fallback) {
    const char *names[] = {"alloc", "get", "put", "invalidate", "free"};
    assert(seq == ++last_seq);
    if (emit) printf("mapping: seq=%llu action=%s offset=%llu server=%u tier=%s slot=%llu users=%u live=%u fallback=%u\n",
        (unsigned long long)seq, names[action], (unsigned long long)offset,
        server, tier ? "pmem" : "dram", (unsigned long long)slot, users, live, fallback);
}
static struct rswap_pool_layout layout(u64 a, u64 b, u64 c, u64 d) {
    return (struct rswap_pool_layout){.server_pages={a+b,c+d}, .total_pages=a+b+c+d,
        .nr_servers=c ? 2 : 1, .tier_pages={{a,b},{c,d}}};
}
static void start(struct rswap_pool_layout p) {
    last_seq = 0;
    assert(!rswap_mapping_init(&p));
}
static void check(unsigned int empty) {
    struct rswap_mapping_stats stats;
    rswap_mapping_snapshot(&stats);
    assert(stats.metadata_bytes == stats.pages * 12);
    for (unsigned int s = 0; s < stats.nr_servers; s++)
        for (unsigned int t = 0; t < 2; t++) {
            struct rswap_mapping_pool_stats p = stats.pools[s][t];
            assert(p.capacity == p.live + p.retired + p.free);
            if (empty) assert(p.free == p.capacity && !p.live && !p.retired);
        }
}
static void exhaustive(void) {
    for (u64 a = 1; a <= 4; a++) for (u64 b = 1; b <= 4; b++)
        for (u64 c = 0; c <= 4; c++) for (u64 d = c ? 1 : 0; d <= (c ? 4 : 0); d++) {
            struct rswap_pool_layout p = layout(a,b,c,d);
            unsigned char seen[16] = {0};
            start(p);
            for (u64 off = 0; off < p.total_pages; off++) {
                struct rswap_location loc, got;
                unsigned int s,t;
                u64 slot;
                assert(!rswap_mapping_alloc(off, &loc));
                assert(!rswap_map_page(&p, off, &s, &t, &slot));
                assert(loc.server == s && loc.tier == t && !seen[loc.id]++);
                assert(!rswap_mapping_get(off, &got) && got.id == loc.id);
                rswap_mapping_put(got.id);
                assert(rswap_mapping_alloc(off, &got) == -EEXIST);
                rswap_mapping_put(loc.id);
                check(0);
            }
            struct rswap_location got;
            assert(rswap_mapping_get(p.total_pages, &got) == -ERANGE);
            rswap_mapping_reset(); check(1);
            /* Same connected pool is usable after swapoff, with no reset of stats v2. */
            assert(!rswap_mapping_alloc(1, &got));
            rswap_mapping_put(got.id);
            rswap_mapping_reset(); check(1);
            rswap_mapping_destroy();
        }
}
static void fallback(void) {
    struct rswap_location loc[4], got;
    start(layout(1,1,1,1));
    for (unsigned int i = 0; i < 4; i++) {
        assert(!rswap_mapping_alloc(0, &loc[i]));
        assert(loc[i].server == (i & 1) && loc[i].tier == (i >> 1));
        rswap_mapping_invalidate(0); /* All four old requests remain in flight. */
        assert(rswap_mapping_get(0, &got) == -ENOENT);
        check(0);
    }
    assert(rswap_mapping_alloc(0, &got) == -ENOSPC);
    for (unsigned int i = 0; i < 4; i++) rswap_mapping_put(loc[i].id);
    check(1); rswap_mapping_destroy();
    start(layout(1,1,0,0));
    assert(!rswap_mapping_alloc(0, &loc[0]) && loc[0].tier == 0);
    rswap_mapping_invalidate(0);
    assert(!rswap_mapping_alloc(0, &loc[1]) && loc[1].tier == 1);
    rswap_mapping_invalidate(0);
    assert(rswap_mapping_alloc(0, &got) == -ENOSPC);
    rswap_mapping_put(loc[0].id); rswap_mapping_put(loc[1].id);
    check(1); rswap_mapping_destroy();
}
static void generations(void) {
    struct rswap_location old, fresh, read;
    start(layout(2,2,2,2));
    assert(!rswap_mapping_alloc(0, &old));
    rswap_mapping_put(old.id); /* Store retired, mapping persists. */
    assert(!rswap_mapping_get(0, &read));
    rswap_mapping_invalidate(0);
    assert(!rswap_mapping_alloc(0, &fresh) && fresh.id != old.id);
    assert(!rswap_mapping_abort(0, old.id)); /* Stale completion cannot erase fresh. */
    assert(rswap_mapping_current(0, fresh.id));
    rswap_mapping_put(read.id);
    assert(!rswap_mapping_get(0, &read) && read.id == fresh.id);
    rswap_mapping_put(read.id);
    assert(rswap_mapping_abort(0, fresh.id));
    assert(!rswap_mapping_abort(0, fresh.id));
    rswap_mapping_put(fresh.id);
    check(1); rswap_mapping_destroy();
}
static void *worker(void *arg) {
    u64 off = (uintptr_t)arg;
    for (unsigned int i = 0; i < 3000; i++) {
        struct rswap_location old, fresh, got;
        assert(!rswap_mapping_alloc(off, &old));
        assert(!rswap_mapping_get(off, &got) && got.id == old.id);
        rswap_mapping_invalidate(off);
        assert(!rswap_mapping_alloc(off, &fresh) && fresh.id != old.id);
        assert(!rswap_mapping_abort(off, old.id));
        rswap_mapping_put(got.id); rswap_mapping_put(old.id);
        rswap_mapping_invalidate(off); rswap_mapping_put(fresh.id);
    }
    return NULL;
}
static void concurrent(void) {
    pthread_t threads[8];
    start(layout(32,32,32,32));
    for (uintptr_t i = 0; i < 8; i++) assert(!pthread_create(&threads[i], NULL, worker, (void *)i));
    for (unsigned int i = 0; i < 8; i++) pthread_join(threads[i], NULL);
    check(1); rswap_mapping_destroy();
}
static void random_full(void) {
    struct rswap_location loc[64], held[64];
    unsigned char live[64] = {0}, pin[64] = {0};
    unsigned int exhausted = 0;
    start(layout(8,8,16,32));
    for (unsigned int off = 0; off < 64; off++) {
        assert(!rswap_mapping_alloc(off, &loc[off]));
        live[off] = 1; rswap_mapping_put(loc[off].id);
    }
    srand(123);
    for (unsigned int i = 0; i < 100000; i++) {
        unsigned int off = (unsigned int)rand() % 64;
        if (!live[off]) {
            int ret = rswap_mapping_alloc(off, &loc[off]);
            if (!ret) { live[off] = 1; rswap_mapping_put(loc[off].id); }
            else { assert(ret == -ENOSPC); exhausted++; }
        } else if (rand() & 1) {
            if (!pin[off]) {
                assert(!rswap_mapping_get(off, &held[off]));
                pin[off] = 1;
            }
            rswap_mapping_invalidate(off); live[off] = 0;
        }
        if (pin[off] && (rand() & 1)) { rswap_mapping_put(held[off].id); pin[off] = 0; }
        check(0);
        /* Independent uniqueness check of all indexed mappings. */
        for (unsigned int a = 0; a < 64; a++) if (live[a])
            for (unsigned int b = a + 1; b < 64; b++)
                assert(!live[b] || loc[a].id != loc[b].id);
    }
    assert(exhausted); /* Actually exercise capacity rejection, not only sparse use. */
    for (unsigned int off = 0; off < 64; off++) {
        rswap_mapping_invalidate(off);
        if (pin[off]) rswap_mapping_put(held[off].id);
    }
    check(1); rswap_mapping_destroy();
}
static void initialization(void) {
    struct rswap_pool_layout p = layout(2,2,0,0);
    for (int failure = 0; failure < 2; failure++) {
        mapping_alloc_fail_after = failure;
        assert(rswap_mapping_init(&p) == -ENOMEM);
        assert(!mapping.index && !mapping.slots && !mapping.layout.total_pages);
    }
    mapping_alloc_fail_after = -1;
    start(p);
    assert(rswap_mapping_init(&p) == -EBUSY);
    rswap_mapping_destroy();
    p.total_pages++;
    assert(rswap_mapping_init(&p) == -EINVAL);
    p = layout(1,1,0,0); p.total_pages = (u64)UINT32_MAX + 1;
    assert(rswap_mapping_init(&p) == -EINVAL);
    /* Production validation accepts 3.5 TiB and protocol maximum; force allocation
     * failure to avoid actually allocating 10.5/18.75 GiB in this local test. */
    mapping_alloc_fail_after = 0;
    p = layout(32ULL<<21,192ULL<<21,32ULL<<21,192ULL<<21);
    assert(rswap_mapping_init(&p) == -ENOMEM);
    assert(p.total_pages * 12 == 11274289152ULL);
    p = layout(200ULL<<21,200ULL<<21,200ULL<<21,200ULL<<21);
    assert(rswap_mapping_init(&p) == -ENOMEM);
    mapping_alloc_fail_after = -1;
}
static void explicit_preference(void) {
    struct rswap_location loc, read;
    start(layout(2,2,2,2));
    assert(!rswap_mapping_alloc_preferred(1, 0, 1, &loc));
    assert(loc.server == 0 && loc.tier == 1); /* differs from offset's Interleave */
    rswap_mapping_put(loc.id);
    assert(!rswap_mapping_get(1, &read) && read.id == loc.id);
    rswap_mapping_put(read.id);
    assert(rswap_mapping_alloc_preferred(2, 2, 0, &loc) == -EINVAL);
    assert(rswap_mapping_alloc_preferred(2, 0, 2, &loc) == -EINVAL);
    assert(rswap_mapping_alloc_preferred(8, 0, 0, &loc) == -ERANGE);
    rswap_mapping_reset(); check(1); rswap_mapping_destroy();
}
int main(int argc, char **argv) {
    assert(argc == 2);
    switch (atoi(argv[1])) {
    case 0: initialization(); break;
    case 1: exhaustive(); break;
    case 2: fallback(); break;
    case 3: generations(); break;
    case 4: concurrent(); break;
    case 5: random_full(); break;
    case 6: emit = 1; generations(); break;
    case 7: explicit_preference(); break;
    default: abort();
    }
    return 0;
}
"""


class MappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="hermit-p9-mapping-")
        cls.binary = Path(cls.temp.name) / "mapping"
        source = Path(cls.temp.name) / "mapping.c"
        source.write_text(PREFIX + mapping_harness.STUBS + mapping_harness.production() + CASES)
        subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-pthread", "-I", str(mapping_harness.ROOT / "client"),
                        str(source), "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, number):
        return subprocess.run([str(self.binary), str(number)], check=True, capture_output=True,
                              text=True, timeout=30).stdout

    def test_initialization_and_large_pool_budget(self):
        self.run_case(0)

    def test_exhaustive_interleave_full_pool_and_reuse(self):
        self.run_case(1)

    def test_fallback_and_all_slots_pinned(self):
        self.run_case(2)

    def test_old_read_and_new_generation(self):
        self.run_case(3)

    def test_concurrent_allocate_get_invalidate_retire(self):
        self.run_case(4)

    def test_random_nearly_full_pool(self):
        self.run_case(5)

    def test_actual_production_trace_replays(self):
        sys.path.insert(0, str(mapping_harness.ROOT / "tools"))
        import rswap_trace
        result = rswap_trace.check_mapping_records(self.run_case(6).splitlines(), ((2, 2), (2, 2)), True)
        self.assertEqual((result["allocations"], result["frees"], result["live"]), (2, 2, 0))

    def test_explicit_preference_and_dynamic_lookup(self):
        self.run_case(7)


if __name__ == "__main__":
    unittest.main()

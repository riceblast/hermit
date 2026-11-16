#!/usr/bin/env python3
"""Compile and check the actual mapping helper without RDMA or root access."""

from pathlib import Path
import subprocess
import tempfile
import unittest


CLIENT = Path(__file__).resolve().parents[1] / "client"

HARNESS = r"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
typedef uint64_t u64;
typedef uint16_t u16;
#include "rswap_mapping.h"

static struct rswap_pool_layout layout(u64 a, u64 b, u64 c, u64 d)
{
    struct rswap_pool_layout p = {
        .server_pages = {a + b, c + d}, .total_pages = a + b + c + d,
        .nr_servers = c || d ? 2 : 1, .tier_pages = {{a, b}, {c, d}},
    };
    return p;
}

static void expect(const struct rswap_pool_layout *p, u64 offset,
                   unsigned int server, unsigned int tier, u64 slot)
{
    unsigned int s, t;
    u64 v;
    assert(!rswap_map_page(p, offset, &s, &t, &v));
    assert(s == server && t == tier && v == slot);
}

static void exhaustive(void)
{
    unsigned int a, b, c, d, s, t;
    u64 offset, slot;
    /* Both layers' independent tails, in either direction. */
    for (a = 1; a <= 8; a++)
        for (b = 1; b <= 8; b++)
            for (c = 0; c <= 8; c++)
                for (d = c ? 1 : 0; d <= (c ? 8 : 0); d++) {
                    struct rswap_pool_layout p = layout(a, b, c, d);
                    unsigned char seen[2][2][8] = {{{0}}};
                    for (offset = 0; offset < p.total_pages; offset++) {
                        assert(!rswap_map_page(&p, offset, &s, &t, &slot));
                        assert(s < p.nr_servers && t < 2);
                        assert(slot < p.tier_pages[s][t]);
                        assert(!seen[s][t][slot]);
                        seen[s][t][slot] = 1;
                    }
                    for (s = 0; s < p.nr_servers; s++)
                        for (t = 0; t < 2; t++)
                            for (slot = 0; slot < p.tier_pages[s][t]; slot++)
                                assert(seen[s][t][slot]);
                    assert(rswap_map_page(&p, p.total_pages, &s, &t, &slot) == -ERANGE);
                }
}

/* Independent inverse: locate a particular target Slot in the logical pool. */
static u64 inverse(unsigned int target, u64 slot, u64 a, u64 b)
{
    u64 common = a < b ? a : b;
    return slot < common ? 2 * slot + target : common + slot;
}

static void real_pool(u64 a, u64 b, u64 c, u64 d)
{
    const u64 gib_pages = 1ULL << 18, chunk_pages = 1ULL << 21;
    struct rswap_pool_layout p = layout(a * gib_pages, b * gib_pages,
                                        c * gib_pages, d * gib_pages);
    unsigned int s, t;
    u64 slot;
    for (s = 0; s < p.nr_servers; s++) {
        for (t = 0; t < 2; t++) {
            u64 n = p.tier_pages[s][t], boundary;
            /* Every Chunk boundary, including Tier endpoints. */
            for (boundary = 0; boundary <= n; boundary += chunk_pages) {
                int delta;
                for (delta = -1; delta <= 1; delta++) {
                    if ((!boundary && delta < 0) ||
                        (boundary == n && delta >= 0))
                        continue;
                    slot = boundary + delta;
                    u64 local = inverse(t, slot, p.tier_pages[s][0], p.tier_pages[s][1]);
                    u64 offset = p.nr_servers == 1 ? local :
                        inverse(s, local, p.server_pages[0], p.server_pages[1]);
                    assert(offset < p.total_pages);
                    expect(&p, offset, s, t, slot);
                }
            }
        }
    }
    assert(rswap_map_page(&p, p.total_pages, &s, &t, &slot) == -ERANGE);
    assert(rswap_map_page(&p, UINT64_MAX, &s, &t, &slot) == -ERANGE);
}

int main(void)
{
    const u64 gib = 1ULL << 18;
    unsigned int s, t;
    u64 slot;
    struct rswap_pool_layout empty = {0};
    struct rswap_pool_layout p = layout(16 * gib, 32 * gib, 16 * gib, 32 * gib);
    assert(rswap_map_page(&empty, 0, &s, &t, &slot) == -ENODEV);
    expect(&p, 0, 0, 0, 0); /* Original offset, no subtraction of header. */
    expect(&p, 1, 1, 0, 0);
    expect(&p, 2, 0, 1, 0);
    expect(&p, 3, 1, 1, 0);
    expect(&p, 4, 0, 0, 1);
    expect(&p, 64 * gib - 2, 0, 1, 16 * gib - 1);
    expect(&p, 64 * gib - 1, 1, 1, 16 * gib - 1);
    expect(&p, 64 * gib, 0, 1, 16 * gib);
    expect(&p, 96 * gib - 1, 1, 1, 32 * gib - 1);
    p.nr_servers = 3;
    assert(rswap_map_page(&p, 0, &s, &t, &slot) == -ENODEV);
    p = layout(1, 0, 0, 0);
    assert(rswap_map_page(&p, 0, &s, &t, &slot) == -ENODEV);
    p = layout(1, 1, 0, 0);
    p.server_pages[0] = 3; p.total_pages = 3;
    assert(rswap_map_page(&p, 2, &s, &t, &slot) == -ERANGE);
    exhaustive();
    real_pool(8, 8, 0, 0);
    real_pool(16, 32, 0, 0);
    real_pool(32, 16, 0, 0);
    real_pool(16, 32, 16, 32);
    real_pool(8, 16, 16, 8); /* Equal Server totals, opposite Tier ratios. */
    real_pool(16, 16, 16, 32);
    real_pool(16, 32, 16, 16);
    real_pool(256, 1280, 256, 1280); /* 3 TiB. */
    real_pool(256, 1280, 256, 1288); /* Above 3 TiB. */
    real_pool(256, 1536, 512, 1024); /* Both layers have tails; 3.25 TiB. */
    real_pool(256, 1536, 256, 1536); /* 3.5 TiB total. */
    real_pool(8, 3192, 8, 376); /* One Server at 400 Chunks; 3.5 TiB total. */
    real_pool(8, 376, 8, 3192);
    real_pool(8, 3192, 3192, 8); /* Existing protocol ceiling, 6400 GiB. */
    puts("PASS: two-level bijection, all Chunk/tail boundaries, 3--3.5 TiB and protocol ceiling");
    return 0;
}
"""


class MappingTests(unittest.TestCase):
    def test_actual_mapping_helper(self):
        with tempfile.TemporaryDirectory(prefix="hermit-patch2-mapping-") as tmp:
            source = Path(tmp) / "mapping.c"
            binary = Path(tmp) / "mapping"
            source.write_text(HARNESS)
            subprocess.run(
                ["gcc", "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
                 "-I", str(CLIENT), str(source), "-o", str(binary)],
                check=True,
            )
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()

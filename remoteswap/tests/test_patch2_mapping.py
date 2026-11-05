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

/* Kernel scalar aliases used by the header; no alternative mapping code. */
typedef uint64_t u64;
typedef uint16_t u16;
#include "rswap_mapping.h"

static void expect(const struct rswap_pool_layout *pool, u64 offset,
                   unsigned int expected_server, u64 expected_slot)
{
    unsigned int server;
    u64 slot;

    assert(rswap_map_page(pool, offset, &server, &slot) == 0);
    assert(server == expected_server);
    assert(slot == expected_slot);
}

static void small_pools(void)
{
    unsigned int a, b, server;
    u64 offset, slot;

    for (a = 1; a <= 64; a++) {
        struct rswap_pool_layout single = {{a, 0}, a, 1};

        for (offset = 0; offset < a; offset++)
            expect(&single, offset, 0, offset);
        assert(rswap_map_page(&single, a, &server, &slot) == -ERANGE);

        for (b = 1; b <= 64; b++) {
            struct rswap_pool_layout pool = {{a, b}, a + b, 2};
            unsigned char seen[2][64] = {{0}};

            for (offset = 0; offset < pool.total_pages; offset++) {
                assert(rswap_map_page(&pool, offset, &server, &slot) == 0);
                assert(server < 2 && slot < pool.server_pages[server]);
                assert(!seen[server][slot]);
                seen[server][slot] = 1;
            }
            for (server = 0; server < 2; server++)
                for (slot = 0; slot < pool.server_pages[server]; slot++)
                    assert(seen[server][slot]);
            assert(rswap_map_page(&pool, a + b, &server, &slot) == -ERANGE);
        }
    }
}

static void real_pool(u64 a, u64 b)
{
    const u64 chunk_pages = 8ULL << (30 - 12);
    const u64 common = a < b ? a : b;
    struct rswap_pool_layout pool = {{a, b}, a + b, b ? 2 : 1};
    unsigned int server;
    u64 slot;

    expect(&pool, 0, 0, 0);
    expect(&pool, 1, b ? 1 : 0, b ? 0 : 1);
    if (b) {
        expect(&pool, 2 * common - 2, 0, common - 1);
        expect(&pool, 2 * common - 1, 1, common - 1);
        if (a != b)
            expect(&pool, 2 * common, a > b ? 0 : 1, common);
        expect(&pool, pool.total_pages - 1, a > b ? 0 : 1,
               (a > b ? a : b) - 1);
    } else {
        expect(&pool, a - 1, 0, a - 1);
    }

    /* Check both sides of every server-local 8-GiB chunk boundary. */
    for (server = 0; server < pool.nr_servers; server++) {
        u64 boundary;

        for (boundary = chunk_pages;
             boundary < pool.server_pages[server]; boundary += chunk_pages) {
            for (slot = boundary - 1; slot <= boundary; slot++) {
                u64 offset = !b ? slot : slot < common ?
                             2 * slot + server : common + slot;

                expect(&pool, offset, server, slot);
            }
        }
    }
    assert(rswap_map_page(&pool, pool.total_pages, &server, &slot) == -ERANGE);
    assert(rswap_map_page(&pool, UINT64_MAX, &server, &slot) == -ERANGE);
}

int main(void)
{
    const u64 gib_pages = 1ULL << (30 - 12);
    struct rswap_pool_layout empty = {0};
    struct rswap_pool_layout too_many = {{1, 1}, 2, 3};
    unsigned int server;
    u64 slot;

    assert(rswap_map_page(&empty, 0, &server, &slot) == -ENODEV);
    assert(rswap_map_page(&too_many, 0, &server, &slot) == -ENODEV);
    small_pools();
    real_pool(8 * gib_pages, 0);
    real_pool(64 * gib_pages, 0);
    real_pool(8 * gib_pages, 8 * gib_pages);
    real_pool(8 * gib_pages, 16 * gib_pages);
    real_pool(16 * gib_pages, 8 * gib_pages);
    real_pool(3200 * gib_pages, 0);
    real_pool(3200 * gib_pages, 3200 * gib_pages);
    real_pool(8 * gib_pages, 3200 * gib_pages);
    real_pool(3200 * gib_pages, 8 * gib_pages);
    puts("PASS: exhaustive small pools and real-capacity mapping boundaries");
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

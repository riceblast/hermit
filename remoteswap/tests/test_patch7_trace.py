#!/usr/bin/env python3
"""Offline placement checker and comparison with the production C mapping."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import rswap_trace as trace


def event(offset, server, tier, op="write", status=0, area=0):
    return (f"worker-1 [001] .... 123.000001: placement: swap_type={area} "
            f"offset={offset} server={server} tier={tier} op={op} status={status}\n")


class TraceTests(unittest.TestCase):
    def test_counts_successful_io_not_unique_pages(self):
        lines = ["# tracer: nop\n", event(0, 0, "dram"), event(1, 1, "dram", "read_sync"),
                 event(2, 0, "pmem"), event(2, 0, "pmem"), event(3, 1, "pmem", "read_async")]
        counts, errors = trace.check_trace(lines, trace.parse_layout("16:32,16:32"))
        self.assertEqual(errors, 0)
        self.assertEqual(sum(counts.values()), 5)
        self.assertEqual(counts[(0, "pmem", "write")], 2)

    def test_bad_routes_records_and_losses_are_rejected(self):
        layout = trace.parse_layout("16:32,16:32")
        cases = [[], [event(1, 0, "dram")], [event(2, 0, "dram")],
                 [event(96 << 18, 0, "pmem")], [event(0, 0, "dram", "unknown")],
                 [event(0, 0, "dram"), event(1, 1, "dram", area=1)],
                 ["CPU: 1 [LOST 123 EVENTS]\n"]]
        for lines in cases:
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                trace.check_trace(lines, layout)
        counts, errors = trace.check_trace([event(3, 1, "pmem", status=-5)], layout)
        self.assertFalse(counts)
        self.assertEqual(errors, 1)

    def test_layout_validation(self):
        self.assertEqual(trace.parse_layout("16:32"), ((16, 32),))
        self.assertEqual(trace.parse_layout("8:3192,8:376"), ((8, 3192), (8, 376)))
        for value in ("", "0:8", "8:0", "8:9", "8:3200", "8", "x:8", "8:8,8:8,8:8"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                trace.parse_layout(value)

    def test_checker_matches_actual_c_mapping_including_large_tails(self):
        cases = ((16, 32, 0, 0), (16, 32, 16, 32), (16, 16, 16, 32),
                 (32, 16, 16, 16), (8, 3192, 8, 376))
        source = r"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
typedef uint64_t u64;
typedef uint16_t u16;
#include "rswap_mapping.h"
int main(void) {
    const u64 capacities[][4] = {{16,32,0,0}, {16,32,16,32}, {16,16,16,32},
                                 {32,16,16,16}, {8,3192,8,376}};
    for (int c = 0; c < 5; c++) {
        const u64 *v = capacities[c];
        struct rswap_pool_layout p = {
            .server_pages = {(v[0] + v[1]) << 18, (v[2] + v[3]) << 18},
            .total_pages = (v[0] + v[1] + v[2] + v[3]) << 18,
            .nr_servers = v[2] ? 2 : 1,
            .tier_pages = {{v[0] << 18, v[1] << 18}, {v[2] << 18, v[3] << 18}},
        };
        for (u64 boundary = 0; boundary <= p.total_pages; boundary += 1ULL << 21) {
            for (int delta = -1; delta <= 1; delta++) {
                if ((!boundary && delta < 0) || (boundary == p.total_pages && delta >= 0))
                    continue;
                u64 offset = boundary + delta, slot;
                unsigned int server, tier;
                assert(!rswap_map_page(&p, offset, &server, &tier, &slot));
                printf("%d %llu %u %u\n", c, (unsigned long long)offset, server, tier);
            }
        }
    }
    return 0;
}
"""
        with tempfile.TemporaryDirectory(prefix="hermit-patch7-trace-") as tmp:
            c, binary = Path(tmp) / "mapping.c", Path(tmp) / "mapping"
            c.write_text(source)
            subprocess.run(["gcc", "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "client"), str(c), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], text=True, capture_output=True, check=True)
        for line in result.stdout.splitlines():
            index, offset, server, tier = map(int, line.split())
            values = cases[index]
            layout = (values[:2], values[2:]) if values[2] else (values[:2],)
            self.assertEqual(trace.expected_location(layout, offset), (server, trace.TIERS[tier]))

    def test_cli_success_and_failed_wc(self):
        tool = ROOT / "tools/rswap_trace.py"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.log"
            path.write_text(event(3, 1, "pmem"))
            command = [sys.executable, str(tool), str(path), "--layout", "16:32,16:32"]
            result = subprocess.run(command, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PASS: 1 successful I/O", result.stdout)
            path.write_text(event(3, 1, "pmem", status=-5))
            result = subprocess.run(command, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("failed WC", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()

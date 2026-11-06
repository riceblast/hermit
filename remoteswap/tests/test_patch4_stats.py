#!/usr/bin/env python3
"""Unprivileged counter-format and payload-bandwidth checks."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_patch3_completion import definition

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rswap_stats_tool", ROOT / "tools/rswap_stats.py")
stats = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = stats
spec.loader.exec_module(stats)


def snapshot(instance=10, ns=1_000_000_000, rows=None, unrouted=0):
    values = {(server, op): (0, 0, 0) for server in (0, 1) for op in stats.OPS}
    values.update(rows or {})
    text = "version=1 instance_ns={} sample_ns={} page_size=4096\n".format(instance, ns)
    text += "server op ok wc_error submit_error\n"
    for (server, op), counts in values.items():
        text += "{} {} {} {} {}\n".format(server, op, *counts)
    return text + "unrouted_error={}\n".format(unrouted)


class StatsTests(unittest.TestCase):
    def test_actual_kernel_formatter_matches_reader(self):
        source = (ROOT / "client/rswap_stats.c").read_text()
        header = (ROOT / "client/rswap_rdma.h").read_text()
        stubs = r"""
#include <stdio.h>
#include <stdarg.h>
#include <stdint.h>
#include <assert.h>
#include <errno.h>
typedef uint64_t u64;
typedef long long atomic64_t;
#define PAGE_SIZE 4096UL
#define NUM_QP_TYPE 3
#define atomic64_read(p) (*(p))
struct seq_file { int unused; };
static void seq_printf(struct seq_file *m, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt); vprintf(fmt, ap); va_end(ap);
}
static void seq_puts(struct seq_file *m, const char *s) { fputs(s, stdout); }
static u64 ktime_get_ns(void) { return 1000000000ULL; }
struct inode { int unused; };
struct file { int unused; };
struct module { int state; };
enum { MODULE_STATE_LIVE, MODULE_STATE_COMING, MODULE_STATE_GOING };
static struct module module = {.state = MODULE_STATE_COMING};
static int opens;
#define THIS_MODULE (&module)
#define READ_ONCE(x) (x)
static int single_open(struct file *f, int (*show)(struct seq_file *, void *), void *p) {
    opens++; return 0;
}
"""
        support = r"""
struct rdma_session_context { struct rswap_io_stats io_stats[3]; };
static struct rdma_session_context rdma_sessions[2];
static unsigned int stats_nr_servers = 2;
static u64 stats_instance_ns = 42;
static atomic64_t unrouted_error = 7;
static const char * const stats_op_names[] = {"write", "read_sync", "read_async"};
"""
        cases = r"""
int main(void) {
    struct seq_file m = {0};
    assert(rswap_stats_open(NULL, NULL) == -ENODEV && !opens);
    module.state = MODULE_STATE_LIVE;
    assert(rswap_stats_open(NULL, NULL) == 0 && opens == 1);
    module.state = MODULE_STATE_GOING;
    assert(rswap_stats_open(NULL, NULL) == -ENODEV && opens == 1);
    for (int s = 0; s < 2; s++)
        for (int op = 0; op < 3; op++)
            rdma_sessions[s].io_stats[op] = (struct rswap_io_stats){100 * s + op, op, s};
    return rswap_stats_show(&m, NULL);
}
"""
        code = (stubs + definition(header, "rswap_io_stats", struct=True) + support
                + definition(source, "rswap_stats_show")
                + definition(source, "rswap_stats_open") + cases)
        with tempfile.TemporaryDirectory(prefix="hermit-patch4-stats-") as tmp:
            c = Path(tmp) / "stats.c"
            binary = Path(tmp) / "stats"
            c.write_text(code)
            subprocess.run(["gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", str(c), "-o", str(binary)], check=True)
            output = subprocess.run([str(binary)], capture_output=True, text=True, check=True)
        result = stats.parse_snapshot(output.stdout)
        self.assertEqual((result.instance_ns, result.sample_ns, result.page_size),
                         (42, 1_000_000_000, 4096))
        self.assertEqual(result.unrouted_error, 7)
        for server in (0, 1):
            for op, name in enumerate(stats.OPS):
                self.assertEqual(result.rows[(server, name)], (100 * server + op, op, server))

    def test_successful_payload_only_and_error_deltas(self):
        before = stats.parse_snapshot(snapshot(rows={(0, "write"): (100, 2, 4)}))
        after = stats.parse_snapshot(snapshot(ns=3_000_000_000, unrouted=3, rows={
            (0, "write"): (51300, 7, 10),
            (0, "read_sync"): (25600, 2, 1),
            (0, "read_async"): (12800, 3, 2),
            (1, "write"): (25600, 1, 1),
        }))
        seconds, rates, unrouted = stats.rates_between(before, after)
        self.assertEqual(seconds, 2)
        self.assertEqual(unrouted, 3)
        self.assertEqual(rates[0], dict(write=100, read_sync=50, read_async=25,
                                      read_total=75, total=175, wc_error=10, submit_error=9))
        self.assertEqual(rates[1]["write"], 50)
        self.assertEqual(rates[1]["read_total"], 0)

    def test_idle_interval(self):
        before = stats.parse_snapshot(snapshot())
        after = stats.parse_snapshot(snapshot(ns=2_000_000_000))
        _, rates, unrouted = stats.rates_between(before, after)
        self.assertFalse(unrouted)
        self.assertTrue(all(value == 0 for row in rates.values() for value in row.values()))

    def test_module_reload_even_when_new_counts_are_higher(self):
        before = stats.parse_snapshot(snapshot())
        after = stats.parse_snapshot(snapshot(instance=20, ns=2_000_000_000,
                                               rows={(0, "write"): (99999, 0, 0)}))
        with self.assertRaises(stats.BaselineChanged):
            stats.rates_between(before, after)

    def test_counter_decrease_and_invalid_time(self):
        before = stats.parse_snapshot(snapshot(rows={(0, "write"): (10, 0, 0)}))
        after = stats.parse_snapshot(snapshot(ns=2_000_000_000))
        with self.assertRaises(stats.BaselineChanged):
            stats.rates_between(before, after)
        with self.assertRaisesRegex(ValueError, "time did not advance"):
            stats.rates_between(before, before)

    def test_malformed_snapshots_are_rejected(self):
        good = snapshot()
        cases = ["", good.replace("version=1", "version=2"),
                 good.replace("version=1", "version=1 version=1"),
                 good.replace("0 write 0 0 0\n", ""),
                 good.replace("0 write 0 0 0", "0 write -1 0 0"),
                 good.replace("0 write 0 0 0", "0 write 0 0 0\n0 write 1 0 0"),
                 good.replace("unrouted_error=0", "unrouted_error=-1")]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                stats.parse_snapshot(text)

    def test_monitor_rebaselines_without_printing_reload_as_traffic(self):
        first = stats.parse_snapshot(snapshot())
        second = stats.parse_snapshot(snapshot(instance=20, ns=2_000_000_000,
                                                rows={(0, "write"): (99999, 0, 0)}))
        third = stats.parse_snapshot(snapshot(instance=20, ns=3_000_000_000,
                                               rows={(0, "write"): (99999 + 256, 0, 0)}))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(stats, "read_snapshot", side_effect=[first, second, third]), \
                patch.object(stats.time, "sleep", side_effect=[None, None, KeyboardInterrupt]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(KeyboardInterrupt):
                stats.monitor("unused", 1)
        self.assertIn("New baseline", err.getvalue())
        self.assertEqual(out.getvalue().count("interval="), 1)
        self.assertIn("0 1.000 0.000", out.getvalue())

    def test_cli_reports_missing_interface_and_invalid_intervals(self):
        tool = ROOT / "tools/rswap_stats.py"
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(tool), "--path", str(Path(tmp) / "missing")],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("RDMA module loaded", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        for value in ("0", "-1", "nan", "inf"):
            result = subprocess.run([sys.executable, str(tool), "--interval", value],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()

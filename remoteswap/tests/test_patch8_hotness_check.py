#!/usr/bin/env python3
"""Offline associations must use past snapshots and honest Store denominators."""
import csv
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import rswap_hotness_check as check


def event(ns, pfn=65, ret=0, area=0, offset=17):
    return (f"kswapd-1 [001] .... 1.0: store_sample: start_ns={ns} pfn={pfn} "
            f"swap_type={area} offset={offset} submit_ret={ret}\n")


def page(va, pfn, active=1, valid=1, state="resident", alias=0, last_pfn=None):
    return (hex(va), pfn, pfn if last_pfn is None else last_pfn, state, active, valid,
            active * 1000000 // valid if valid else -1, alias,
            0 if state == "swapped" else -1, 17 if state == "swapped" else 0)


class AssociationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hermit-patch8-check-")
        self.path = Path(self.tmp.name)
        self.summary = self.path / "summary.tsv"
        self.write_run()

    def tearDown(self):
        self.tmp.cleanup()

    def write_run(self, status=0, error=0, signal=0):
        (self.path / "run.tsv").write_text(
            "version\tpid\tstart_ticks\tstart_ns\tend_ns\twindow\tinterval\twarmup\texit_status\tmonitor_error\tstop_signal\n"
            f"1\t123\t99\t100\t300\t20\t1\t20\t{status}\t{error}\t{signal}\n")

    def snapshots(self, tables):
        rows = []
        for generation, pages in enumerate(tables):
            path = self.path / f"pages-{generation:06d}.tsv"
            with path.open("w") as stream:
                writer = csv.writer(stream, delimiter="\t")
                writer.writerow(("va", "pfn", "last_pfn", "state", "active", "valid", "heat_ppm", "alias", "swap_type", "swap_offset"))
                writer.writerows(pages)
            values = [p[4] / p[5] for p in pages if p[5] and not p[7]]
            begin = 110 + generation * 90
            rows.append((1, 123, generation, 100, begin, begin + 10, begin + 20,
                         "warmup" if generation == 0 else "observe", len(values),
                         sum(values) / len(values) if values else -1, path.name))
        with self.summary.open("w") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(("version", "pid", "generation", "start_ns", "begin_ns", "end_ns",
                             "ready_ns", "phase", "known", "mean", "pages_file"))
            writer.writerows(rows)
        return check.read_summary(self.summary)

    def test_past_only_matching_unknowns_failures_and_run_bounds(self):
        snapshots = self.snapshots([
            [page(4096, 65), page(8192, 66, active=0), page(12288, 68, active=0, valid=0)],
            [page(4096, 69, active=0)]])
        trace = [event(140), event(125), event(150, 66), event(160, 67), event(170, 68),
                 event(180, ret=-12), event(230), event(240, 69), event(90), event(301)]
        detail = io.StringIO()
        result = check.correlate(snapshots, check.read_stores(reversed(trace)), detail)
        self.assertEqual(result["accepted"], 9)
        self.assertEqual(result["accepted_in_run"], 7)
        self.assertEqual(result["matched"], 3)
        self.assertEqual(result["matched_hot"], 1)
        self.assertEqual(result["matched_cold"], 2)  # h == mean goes cold
        self.assertEqual(result["no_prior_snapshot"], 1)
        self.assertEqual(result["pfn_absent"], 2)
        self.assertEqual(result["unknown_heat"], 1)
        self.assertEqual(result["submit_failed"], 1)
        self.assertEqual(result["outside_run"], 2)
        self.assertAlmostEqual(result["in_run_accepted_store_match_ratio"], 3 / 7)
        self.assertIn("no_prior_snapshot", detail.getvalue())

    def test_snapshot_must_be_ready_not_just_scanned(self):
        snapshots = self.snapshots([[page(4096, 65)]])
        result = check.correlate(snapshots, check.read_stores([event(120), event(129), event(130)]))
        self.assertEqual(result["no_prior_snapshot"], 2)
        self.assertEqual(result["matched_cold"], 1)

    def test_swapped_history_is_not_a_current_pfn_and_aliases_do_not_weight_mean(self):
        snapshots = self.snapshots([[page(4096, 0, state="swapped", last_pfn=65),
                                     page(8192, 66, active=0), page(12288, 66, active=0, alias=1)]])
        result = check.correlate(snapshots, check.read_stores([event(150), event(160, 66)]))
        self.assertEqual(result["pfn_absent"], 1)
        self.assertEqual(result["matched_cold"], 1)
        self.assertEqual(snapshots[0].mean, 0.5)

    def test_malformed_events_and_loss_rejected(self):
        for lines in ([], [event(150).replace("pfn=65", "pfn=x")], [event(150, ret=1)],
                      [event(150, pfn=0)], [event(150, area=32)], ["CPU: 1 [LOST 12 EVENTS]\n"]):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                check.read_stores(lines)

    def test_summary_and_pages_consistency(self):
        snapshots = self.snapshots([[page(4096, 65)]])
        snapshots[0].path.write_text(snapshots[0].path.read_text().replace("1000000", "0"))
        with self.assertRaises(ValueError):
            check.read_pages(snapshots[0])
        self.summary.write_text(self.summary.read_text().replace("pages-000000.tsv", "-"))
        with self.assertRaises(ValueError):
            check.read_summary(self.summary)

    def test_failed_monitor_rejected_and_clean_signal_stop_accepted(self):
        self.snapshots([[page(4096, 65)]])
        self.write_run(status=1, error=1)
        with self.assertRaises(ValueError):
            check.read_summary(self.summary)
        self.write_run(status=130, signal=2)
        self.assertEqual(len(check.read_summary(self.summary)), 1)

    def test_buffer_loss_stats(self):
        path = self.path / "buffers"
        good = "overrun: 0\ncommit overrun: 0\ndropped events: 0\n"
        path.write_text(good * 2)
        check.check_buffer_stats(path)
        for text in (good.replace("overrun: 0", "overrun: 1", 1), "entries: 0\n", good + "overrun: 0\n"):
            path.write_text(text)
            with self.assertRaises(ValueError):
                check.check_buffer_stats(path)

    def test_cli_report_and_require_match(self):
        self.snapshots([[page(4096, 65)]])
        path = self.path / "trace"
        path.write_text(event(150))
        command = [sys.executable, str(ROOT / "tools/rswap_hotness_check.py"),
                   str(self.summary), str(path), "--require-match"]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"matched": 1', result.stdout)
        self.assertIn("not exact target-PID", result.stdout)
        path.write_text(event(150, 70))
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no accepted Store matched", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()

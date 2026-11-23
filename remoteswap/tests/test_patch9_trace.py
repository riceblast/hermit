"""Validate dynamic ownership replay and reject early reuse/lost events."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import rswap_trace as trace


def event(seq, action, offset=0, server=0, tier="dram", slot=0, users=1, live=1, fallback=0):
    return (f"mapping: seq={seq} action={action} offset={offset} server={server} "
            f"tier={tier} slot={slot} users={users} live={live} fallback={fallback}\n")


class DynamicTraceTests(unittest.TestCase):
    def test_old_and_new_slot_lifecycles(self):
        lines = [event(1, "alloc"), event(2, "invalidate", live=0),
                 event(3, "alloc", slot=1), event(4, "put", users=0, live=0),
                 event(5, "free", users=0, live=0), event(6, "put", slot=1, users=0),
                 event(7, "get", slot=1), event(8, "put", slot=1, users=0),
                 event(9, "invalidate", slot=1, users=0, live=0),
                 event(10, "free", slot=1, users=0, live=0)]
        result = trace.check_mapping_records(lines, ((2, 2),), True)
        self.assertEqual((result["allocations"], result["frees"], result["references"]), (2, 2, 0))

    def test_reject_early_reuse_free_stale_lookup_and_gaps(self):
        cases = [
            [event(1, "alloc"), event(2, "alloc", offset=1)],
            [event(1, "alloc"), event(2, "free", users=0, live=0)],
            [event(1, "alloc"), event(2, "invalidate", live=0), event(3, "get", users=2, live=0)],
            [event(1, "alloc"), event(3, "put", users=0)],
            [event(1, "get")],
            [event(1, "alloc", slot=2)],
            [event(1, "alloc"), event(2, "put", users=2)],
            [event(1, "alloc"), "CPU:0 [LOST 1 EVENTS]\n"],
            ["mapping: malformed\n"],
            [],
        ]
        for lines in cases:
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                trace.check_mapping_records(lines, ((2, 2),))

    def test_all_fallbacks_and_capacity_accounting(self):
        lines = []
        for i in range(4):
            lines += [event(2*i+1, "alloc", server=i & 1,
                            tier="pmem" if i >> 1 else "dram", fallback=i),
                      event(2*i+2, "invalidate", server=i & 1,
                            tier="pmem" if i >> 1 else "dram", live=0)]
        result = trace.check_mapping_records(lines, ((1, 1), (1, 1)))
        self.assertEqual(result["retired"], 4)
        self.assertEqual(result["fallbacks"], {0: 1, 1: 1, 2: 1, 3: 1})
        with self.assertRaisesRegex(ValueError, "bypassed available"):
            trace.check_mapping_records([event(1, "alloc", server=1, fallback=1)], ((1, 1), (1, 1)))
        with self.assertRaises(ValueError):
            trace.check_mapping_records([event(1, "alloc", server=1, fallback=1)], ((1, 1),))

    def test_partial_end_reported_and_optional_empty_requirement(self):
        result = trace.check_mapping_records([event(1, "alloc")], ((1, 1),))
        self.assertEqual((result["live"], result["references"]), (1, 1))
        with self.assertRaisesRegex(ValueError, "still occupied"):
            trace.check_mapping_records([event(1, "alloc")], ((1, 1),), True)

    def test_dynamic_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.log"
            path.write_text(event(1, "alloc") + event(2, "put", users=0)
                            + event(3, "invalidate", users=0, live=0)
                            + event(4, "free", users=0, live=0))
            result = subprocess.run([sys.executable, str(ROOT / "tools/rswap_trace.py"), str(path),
                                     "--layout", "16:32,16:32", "--dynamic", "--require-empty"],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PASS: dynamic Slot lifecycle", result.stdout)


if __name__ == "__main__":
    unittest.main()

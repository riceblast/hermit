"""Adaptive decisions join allocation IDs without assuming static placement."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_patch9_trace import event
from test_patch7_trace import trace, ROOT


def decision(**changes):
    values = dict(alloc_seq=1, offset=1, pfn=65, run=1, generation=1,
                  decision_ns=100, publish_ns=50, state=2, reason=5, heat_ppm=1000000,
                  mean_ppm=500000, preferred_server=0, preferred_tier=0,
                  server=0, tier=0, slot=0, fallback=0, rr=0, alloc_ret=0)
    values.update(changes)
    return "decision: " + " ".join(f"{k}={v}" for k, v in values.items()) + "\n"


def current_decision(**changes):
    values = dict(threshold_ppm=500000, policy=0, threshold_source=0,
                  threshold_generation=1, threshold_status=0, placement_rule=1)
    values.update(changes)
    return decision(**values)


class AdaptiveTraceTests(unittest.TestCase):
    def test_unknown_dram_defaults_and_legacy_interleave(self):
        layout = ((16, 32), (16, 32))
        for reason in (2, 3, 4):
            changes = dict(reason=reason, heat_ppm=-1, offset=2, rr=3,
                           preferred_server=1, server=1)
            if reason in (2, 3):
                changes.update(mean_ppm=-1, threshold_ppm=-1, threshold_generation=0)
            if reason == 2:
                changes.update(generation=0, publish_ns=0)
            with self.subTest(reason=reason):
                line = current_decision(**changes)
                self.assertEqual(trace.read_decisions([line], layout)[1]['reason'], reason)
                for bad in (dict(preferred_tier=1, tier=1), dict(preferred_server=0),
                            dict(heat_ppm=0), dict(placement_rule=2)):
                    with self.assertRaises(ValueError):
                        trace.read_decisions([current_decision(**(changes | bad))], layout)
        # Historical 1.10/1.11 and 1.12 logs retain their original offset mapping.
        for line in (decision(reason=4, heat_ppm=-1, offset=2, preferred_tier=1, tier=1),
                     current_decision(reason=4, heat_ppm=-1, offset=2,
                                      preferred_tier=1, tier=1, placement_rule=0)):
            self.assertEqual(len(trace.read_decisions([line], layout)), 1)
        # A single Server's unknowns still prefer DRAM regardless of offset.
        trace.read_decisions([current_decision(reason=4, heat_ppm=-1, offset=1, rr=9)], ((16, 32),))

    def test_shared_dram_rr_and_zero_interleave(self):
        layout = ((16, 32), (16, 32))
        zero = current_decision(alloc_seq=2, offset=2, reason=7, heat_ppm=0,
                                preferred_tier=1, tier=1)
        lines = [current_decision(reason=4, heat_ppm=-1), zero,
                 current_decision(alloc_seq=3, rr=1, preferred_server=1, server=1)]
        self.assertEqual(len(trace.read_decisions(reversed(lines), layout)), 3)
        for duplicate in (current_decision(alloc_seq=3),
                          current_decision(alloc_seq=0, alloc_ret=-28),
                          current_decision(alloc_seq=3, reason=4, heat_ppm=-1)):
            with self.assertRaisesRegex(ValueError, 'duplicate RR ticket'):
                trace.read_decisions([lines[0], duplicate], layout)
        with self.assertRaisesRegex(ValueError, 'RR ticket'):
            trace.read_decisions([zero.replace('rr=0', 'rr=2')], layout)
        # A newer snapshot does not reset the run's shared sequence.
        with self.assertRaisesRegex(ValueError, 'duplicate RR ticket'):
            trace.read_decisions([lines[0], current_decision(alloc_seq=3, generation=2,
                                                           threshold_generation=2)], layout)

    def test_unknown_capacity_fallback_and_reclaim(self):
        lines, sequence = [], 0
        for i, (s, t, fallback) in enumerate(((0, 'dram', 0), (1, 'dram', 0), (0, 'pmem', 2))):
            sequence += 1
            lines += [event(sequence, 'alloc', offset=i, server=s, tier=t, fallback=fallback),
                      current_decision(alloc_seq=sequence, offset=i, reason=4, heat_ppm=-1,
                                       rr=i, preferred_server=i%2, server=s,
                                       tier=trace.TIERS.index(t), fallback=fallback)]
            sequence += 1
            lines.append(event(sequence, 'put', offset=i, server=s, tier=t, users=0))
        for i, (s, t) in enumerate(((0, 'dram'), (1, 'dram'), (0, 'pmem'))):
            sequence += 1
            lines.append(event(sequence, 'invalidate', offset=i, server=s, tier=t, users=0, live=0))
            sequence += 1
            lines.append(event(sequence, 'free', offset=i, server=s, tier=t, users=0, live=0))
        records = trace.read_decisions(lines, ((16, 32), (16, 32)))
        result = trace.check_mapping_records(lines, ((1, 1), (1, 1)), True, records)
        self.assertEqual(result['frees'], 3)

    def test_dynamic_join_and_cli(self):
        # Offset 1 normally prefers Server 1. Adaptive legitimately selects 0.
        lines = [event(1, "alloc", offset=1), decision(),
                 event(2, "put", offset=1, users=0),
                 event(3, "invalidate", offset=1, users=0, live=0),
                 event(4, "free", offset=1, users=0, live=0)]
        layout = ((16, 32), (16, 32))
        with self.assertRaises(ValueError):
            trace.check_dynamic_trace(lines, layout, True)
        records = trace.read_decisions(reversed(lines), layout)
        self.assertEqual(trace.check_dynamic_trace(lines, layout, True, records)["frees"], 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.log"
            path.write_text("".join(lines))
            result = subprocess.run([sys.executable, str(ROOT / "tools/rswap_trace.py"), str(path),
                                     "--layout", "16:32,16:32", "--dynamic", "--adaptive", "--require-empty"],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_bad_threshold_unknown_rr_generation_and_identity(self):
        layout = ((16, 32), (16, 32))
        for change in (dict(heat_ppm=500000), dict(mean_ppm=-1), dict(preferred_server=1),
                       dict(publish_ns=101), dict(generation=0), dict(state=1),
                       dict(reason=4), dict(run=0), dict(alloc_seq=0), dict(alloc_ret=1)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                trace.read_decisions([decision(**change)], layout)
        records = trace.read_decisions([decision()], layout)
        with self.assertRaises(ValueError):
            trace.check_dynamic_trace([event(1,"alloc")], layout, decisions=records)
        with self.assertRaises(ValueError):
            trace.read_decisions([decision(), decision(alloc_seq=2)], layout)

    def test_cold_equal_unknown_and_failed_attempts(self):
        layout = ((16, 32), (16, 32))
        for line in (decision(reason=6, heat_ppm=500000, preferred_tier=1, tier=1),
                     decision(reason=4, heat_ppm=-1, preferred_server=1, server=1),
                     decision(state=1, reason=1, heat_ppm=-1, preferred_server=1, server=1),
                     decision(state=0, reason=0, run=0, generation=0, publish_ns=0,
                              heat_ppm=-1, mean_ppm=-1, preferred_server=1, server=1)):
            self.assertEqual(len(trace.read_decisions([line], layout)), 1)
        records = trace.read_decisions([decision(), decision(alloc_seq=0, alloc_ret=-28, rr=1,
                                                            preferred_server=1)], layout)
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()

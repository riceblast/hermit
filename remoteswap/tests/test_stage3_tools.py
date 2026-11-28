"""Exact VPN evidence, allocator replay and display/monitor compatibility."""
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from placement_harness import ROOT, function
from test_patch3_completion import definition
from test_patch10_trace import decision as legacy_decision
from test_patch11_tools import bundle, result
from test_patch9_trace import event

sys.path.insert(0, str(ROOT/'tools'))
import rswap_pool as pool
import rswap_trace as trace

SCALE = 1 << 32
LAYOUT = ((16, 32), (16, 32))


def decision(**changes):
    d = dict(version=1, alloc_seq=1, offset=1, pfn=65, binding=1, vpn=200,
             aliases=2, window=5, publish_ns=50, decision_ns=100,
             heat_q32=0, pages=2, sum_heat=0, rr=0, state=2, reason=5,
             preferred_server=0, preferred_tier=0, server=0, tier=0,
             slot=0, fallback=0, alloc_ret=0)
    d.update(changes)
    return 'vpn_decision: ' + ' '.join(f'{k}={v}' for k, v in d.items()) + '\n'


class TraceTests(unittest.TestCase):
    def test_zero_equality_and_exact_rational_comparison(self):
        rows = trace.read_decisions([decision()], LAYOUT)
        self.assertEqual(rows[1]['reason'], 5)
        # Rounded displays would show both values as 0.50, but H is cold.
        cold = decision(heat_q32=SCALE//2, pages=3, sum_heat=3*(SCALE//2)+1,
                        reason=6, preferred_tier=1, tier=1)
        self.assertEqual(trace.read_decisions([cold], LAYOUT)[1]['reason'], 6)
        with self.assertRaises(ValueError):
            trace.read_decisions([cold.replace('reason=6', 'reason=5')], LAYOUT)
        rows = trace.read_decisions([decision(pages=0)], LAYOUT)
        self.assertEqual(rows[1]['reason'], 5)

    def test_warmup_non_target_failures_and_mixed_legacy_capture(self):
        warmup = decision(window=4, heat_q32=SCALE, pages=1, sum_heat=SCALE,
                          state=1, reason=1, preferred_tier=1, tier=1)
        self.assertEqual(trace.read_decisions([warmup], LAYOUT)[1]['state'], 1)
        initial = decision(window=0, publish_ns=0, pages=0, state=1, reason=1,
                           preferred_tier=1, tier=1)
        self.assertEqual(trace.read_decisions([initial], LAYOUT)[1]['publish_ns'], 0)
        unmatched = decision(binding=0, vpn=0, aliases=0, window=0, publish_ns=0,
                             pages=0, state=0, reason=0, offset=0)
        self.assertEqual(trace.read_decisions([unmatched], LAYOUT)[1]['state'], 0)
        summary = Counter()
        rows = trace.read_decisions([decision(alloc_ret=-28, alloc_seq=0),
                                    decision(alloc_seq=2, rr=1, preferred_server=1, server=1)],
                                   LAYOUT, summary)
        self.assertEqual(list(rows), [2])
        self.assertEqual(summary['allocation_error'], 1)
        # Independent RR namespaces even when legacy run ID == VPN binding ID.
        rows = trace.read_decisions([decision(), legacy_decision(alloc_seq=2)], LAYOUT)
        self.assertEqual(len(rows), 2)

    def test_reject_corrupted_metadata_duplicate_tickets_and_event_loss(self):
        bad = [dict(window=4), dict(reason=4), dict(preferred_tier=1),
               dict(pages=1 << 32), dict(sum_heat=2*SCALE+1),
               dict(publish_ns=101), dict(version=2), dict(binding=0),
               dict(window=0, publish_ns=0, pages=0, heat_q32=1, state=1, reason=1,
                    preferred_tier=1, tier=1),
               dict(aliases=0), dict(alloc_ret=-28, fallback=1, alloc_seq=0),
               dict(server=1, fallback=0), dict(alloc_seq=0)]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                trace.read_decisions([decision(**changes)], LAYOUT)
        for lines in ([decision(), decision(alloc_seq=2)],
                      [decision(), 'CPU:0 [LOST 1 EVENTS]\n']):
            with self.assertRaises(ValueError):
                trace.read_decisions(lines, LAYOUT)

    def test_cli_joins_mapping_and_store_results(self):
        lines = [event(1, 'alloc', offset=1), decision(), result(1), result(0),
                 event(2, 'put', offset=1, users=0),
                 event(3, 'invalidate', offset=1, users=0, live=0),
                 event(4, 'free', offset=1, users=0, live=0)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'trace.log'
            path.write_text(''.join(lines))
            run = subprocess.run([sys.executable, str(ROOT/'tools/rswap_trace.py'), str(path),
                                  '--layout', '16:32,16:32', '--dynamic', '--adaptive',
                                  '--require-empty', '--store-results', '--json'],
                                 text=True, capture_output=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(run.stdout)
            self.assertEqual(report['mapping']['live'], 0)
            self.assertEqual(report['decisions']['vpn_decisions'], 1)
            self.assertEqual(report['store_results']['wc_ok'], 1)


class DisplayTests(unittest.TestCase):
    def test_rounded_equal_display_preserves_cold_and_capacity_fallback(self):
        header = (ROOT/'client/rswap_placement.h').read_text()
        formatter = (ROOT/'client/rswap_trace.h').read_text()
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t u64;
typedef uint16_t u16;
#define scnprintf snprintf
#include "rswap_mapping.h"
#include "../rswap_vpn_heat_uapi.h"
#define RSWAP_INACTIVE 0
#define RSWAP_WARMUP 1
#define RSWAP_ADAPTIVE 2
#define RSWAP_REASON_HOT 5
#define RSWAP_APP_DRAM 1
'''
        code += definition(header, 'rswap_decision', struct=True)
        code += function(formatter, 'rswap_format_decision')
        code += function(formatter, 'rswap_format_vpn_decision')
        code += r'''
int main(void) {
    char text[256];
    struct rswap_decision d = {.state=RSWAP_ADAPTIVE,.reason=6,.tier=1,
        .heat_q32=RSWAP_VPN_HEAT_SCALE/2,.pages=3,.sum_heat=3*(RSWAP_VPN_HEAT_SCALE/2)+1};
    struct rswap_location loc={.server=1,.tier=0};
    rswap_format_vpn_decision(text,sizeof(text),~0ULL,&d,&loc);
    assert(strstr(text,"cold(0.50 < 0.50); capacity_fallback"));
    assert(strstr(text,"server: 1, target_mem: dram,"));
    assert(strlen(text)<sizeof(text)-1);
    d=(struct rswap_decision){.state=RSWAP_ADAPTIVE,.reason=RSWAP_REASON_HOT};
    loc=(struct rswap_location){0};
    rswap_format_vpn_decision(text,sizeof(text),1,&d,&loc);
    assert(strstr(text,"hot(0.00 >= 0.00)"));
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'display.c'; path.write_text(code)
            binary = path.with_suffix('')
            subprocess.run(['gcc', '-O1', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                            '-I', str(ROOT/'client'), str(path), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=5)

    def test_vpn_monitor_counts_classification_without_claiming_pfn_coverage(self):
        a, b = bundle(), bundle(10, ns=2_000_000_000)
        for raw in (a, b):
            raw['placement'] += 'source=vpn placement_rule=2\n'
        report = pool.window(a, b)
        self.assertEqual(report['policy']['classified_heat_coverage'], 1)
        self.assertIsNone(report['policy']['positive_heat_coverage'])
        self.assertIsNone(report['policy']['available_snapshot_pfn_hit_ratio'])
        self.assertEqual(report['current']['state']['source'], 'vpn')
        b['placement'] = b['placement'].replace('source=vpn', 'source=pfn')
        self.assertEqual(pool.window(a, b)['endpoint_state'], 'mixed')


if __name__ == '__main__':
    unittest.main()

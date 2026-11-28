"""Per-EWMA exact heat totals, occupancy attribution, ratios and review point."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from test_patch11_tools import bundle
from rswap_pool import parse_bundle
from stage3_bfs_report import EwmaReport, heat, occupancy


def sample(window=0, when=10_000_000_000, count=0, dram=20, pmem=40, active=True):
    raw=bundle(count,ns=when,servers=2)
    lines=['version=1 pages=25165824 metadata_bytes=0 sequence=0','server tier capacity live retired free']
    for s in range(2):
        for tier,cap,used in [('dram',16*262144,dram*262144//2),('pmem',32*262144,pmem*262144//2)]:
            lines.append(f'{s} {tier} {cap} {used} 0 {cap-used}')
    raw['mapping']='\n'.join(lines)+'\n'
    raw['placement']=(f'version=1 state={"adaptive" if window>=5 else "warmup"} area_active=1 source=vpn\n'
                      f'run_id=7 completed_windows={window} mean_ppm=500000\n') if active else 'version=1 state=inactive area_active=1\n'
    return raw


def audit(window=0,when=9_000_000_000):
    return dict(window=window,mono_ns=when,binding_id=7,unknown=0,pages=4,
                hot_pages=2,cold_pages=2,zero_pages=1,sum_heat=2*(1<<32),bins=[1,1,0,1,1])


class ReportTests(unittest.TestCase):
    def test_actual_occupancy_ratio_not_configured_capacity_or_io_total(self):
        view=occupancy(parse_bundle(sample(count=999999)))
        self.assertEqual((view['dram_gib'],view['pmem_gib']),(20,40))
        self.assertEqual((view['ratio'],view['ratio_exact']),('1:2','1:2'))
        self.assertAlmostEqual(view['dram_share'],1/3)
        self.assertAlmostEqual(view['pmem_share'],2/3)
        for d,p,ratio in [(0,40,'0:1'),(20,0,'1:0'),(0,0,'0:0')]:
            self.assertEqual(occupancy(parse_bundle(sample(dram=d,pmem=p)))['ratio'],ratio)

    def test_completed_window_observation_outputs_heat_usage_and_counter_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            report=EwmaReport(directory,parse_bundle(sample(count=3)),0)
            report.observe(sample(count=8))
            row=report.append(audit())
            self.assertEqual(row['mean'],.5)
            self.assertEqual((row['hot_fraction'],row['cold_fraction']),(.5,.5))
            self.assertEqual(row['ratio'],'1:2')
            self.assertEqual(row['new_hot_stores'],5)
            self.assertAlmostEqual(row['occupancy_lag_seconds'],1.000000001)
            saved=json.loads((directory/'bfs.ewma.jsonl').read_text())
            self.assertEqual(saved['bins'],[1,1,0,1,1])
            with (directory/'bfs.ewma.tsv').open() as stream:
                flat=next(csv.DictReader(stream,delimiter='\t'))
            self.assertEqual(flat['bin0'],'1(25.0%)')
            self.assertEqual(flat['ratio'],'1:2')

    def test_backlog_uses_each_windows_own_history_not_latest_pool_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=EwmaReport(Path(tmp),parse_bundle(sample()),0)
            report.observe(sample(0,10_000_000_000,dram=4,pmem=8))
            report.observe(sample(1,30_000_000_000,dram=10,pmem=20))
            first=report.append(audit(0,9_000_000_000))
            second=report.append(audit(1,29_000_000_000))
            self.assertEqual((first['dram_gib'],second['dram_gib']),(4,10))

    def test_target_exit_zero_usage_does_not_replace_missing_live_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=EwmaReport(Path(tmp),parse_bundle(sample()),0)
            report.observe(sample(active=False,dram=0,pmem=0))
            row=report.append(audit())
            self.assertFalse(row['occupancy_observed'])
            self.assertIsNone(row['dram_gib'])
            self.assertIsNone(row['ratio'])

    def test_collection_straddling_commit_is_not_attributed_to_new_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=EwmaReport(Path(tmp),parse_bundle(sample()),0)
            before=sample(when=8_999_999_999,dram=20,pmem=40)
            before['end_ns']=9_000_000_010
            report.observe(before)
            report.observe(sample(when=9_000_000_011,dram=4,pmem=8))
            row=report.append(audit())
            self.assertEqual((row['dram_gib'],row['pmem_gib']),(4,8))

    def test_five_warmup_groups_then_ten_completed_adaptive_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=EwmaReport(Path(tmp),parse_bundle(sample()),0)
            for window in range(16):
                when=(window+1)*20_000_000_000
                report.observe(sample(window,when))
                row=report.append(audit(window,when-1))
                self.assertEqual(row['phase'],'warmup' if window<5 else 'adaptive')
                self.assertEqual(row['adaptive_ewma'],max(0,window-5))
                self.assertEqual(report.ready_to_review(),window==15)
            self.assertFalse(report.ready_to_review(25))

    def test_corrupt_totals_unknown_missing_window_or_new_binding_are_rejected(self):
        for change in [dict(unknown=1),dict(hot_pages=3),dict(bins=[4,0,0,0,1])]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                heat(dict(audit(),**change))
        with tempfile.TemporaryDirectory() as tmp:
            report=EwmaReport(Path(tmp),parse_bundle(sample()),0)
            with self.assertRaises(ValueError): report.append(audit(1))
            report.append(audit())
            with self.assertRaises(ValueError): report.append(dict(audit(1),binding_id=8))


if __name__=='__main__': unittest.main()

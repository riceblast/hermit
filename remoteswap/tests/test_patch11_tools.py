"""Policy/window parsing, reload boundaries, and Store result association."""
import copy
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_patch4_stats import snapshot
from test_patch10_trace import decision
from test_patch7_trace import ROOT, trace
from test_patch9_trace import event

sys.path.insert(0,str(ROOT/'tools'))
import rswap_pool as pool
from rswap_analysis import check_store_results
from rswap_analysis import check_heat, hotness_summary
import test_patch8_hotness_check as heat_fixture
from test_patch8_hotness_check import page


def policy(instance=10,ns=1_000_000_000,count=0,servers=1):
    lines=[f'version=1 instance_ns={instance} sample_ns={ns} nr_servers={servers} page_size=4096']
    for reason in range(7):
        n=count if reason==5 else 0
        lines.append(f'reason {reason} {n} {n} 0 {n} 0')
    for s in range(servers):
        for t in range(2):
            n=count if (s,t)==(0,0) else 0
            lines.append(f'pool {s} {t} {n} {n}')
    for i in range(4):
        lines += [f'fallback {i} {count if i==0 else 0}',f'age {i} {count if i==0 else 0}']
    lines.append('allocation_error 0')
    return '\n'.join(lines)+'\n'


def bundle(count=0,instance=10,ns=1_000_000_000,servers=1):
    mapping='version=1 pages={} metadata_bytes=0 sequence=0\nserver tier capacity live retired free\n'.format(servers*16)
    for s in range(servers):
        for t in ('dram','pmem'):
            mapping+=f'{s} {t} 8 0 0 8\n'
    return dict(version=1,begin_ns=ns,end_ns=ns+1,
                stats=snapshot(instance=instance,ns=ns,servers=tuple(range(servers)),rows={(0,'dram','write'):(count,0,0)}),
                policy_stats=policy(instance,ns,count,servers),mapping=mapping,
                placement=f'version=1 state=adaptive area_active=1 sample_ns={ns}\nrun_id=1 mean_ppm=500000\n')


def result(stage,status=0,seq=1):
    return f'store_result: alloc_seq={seq} offset=1 server=0 tier=0 stage={stage} status={status}\n'


class PoolTests(unittest.TestCase):
    def test_window_metrics_and_zero_denominators(self):
        a,b=bundle(),bundle(100,ns=2_000_000_000)
        x=pool.window(a,b)
        self.assertEqual(x['policy']['adaptive_heat_coverage'],1)
        self.assertEqual(x['wc_ok']['0/dram/write'],100)
        self.assertEqual(x['current']['pools']['0/dram']['used'],0)
        self.assertAlmostEqual(x['bandwidth_MiB_s']['0/dram']['write'],100/256)
        pool.validate_settled(pool.parse_bundle(b)['policy'])
        self.assertIsNone(pool.status(a)['policy']['adaptive_heat_coverage'])
        # Running snapshots may see an immediate WC before post counter update.
        r=pool.parse_policy(policy(count=1).replace('reason 5 1 1 0 1 0','reason 5 1 0 0 1 0'))
        with self.assertRaises(ValueError): pool.validate_settled(r)

    def test_invalid_counters_capacity_and_module_mix(self):
        good=policy()
        for bad in (good.replace('version=1','version=2'),good.replace('reason 1 0 0 0 0 0\n',''),
                    good+'age 0 0\n',good.replace('allocation_error 0','allocation_error -1')):
            with self.subTest(bad=bad),self.assertRaises(ValueError): pool.parse_policy(bad)
        a=bundle();a['mapping']=a['mapping'].replace('8 0 0 8','8 1 0 8')
        with self.assertRaises(ValueError): pool.parse_bundle(a)
        a=bundle();a['policy_stats']=policy(instance=11)
        with self.assertRaises(pool.BaselineChanged): pool.parse_bundle(a)
        with self.assertRaises(pool.BaselineChanged):
            pool.counter_delta(pool.parse_policy(policy(count=2)),pool.parse_policy(policy(count=1)))

    def test_coverage_distinguishes_missing_snapshot_and_pfn_miss(self):
        text=policy()
        for r,n in ((2,2),(3,1),(4,3),(5,2),(6,2)):
            text=text.replace(f'reason {r} 0 0 0 0 0',f'reason {r} {n} 0 0 0 0')
        x=pool.policy_summary(pool.parse_policy(text)['counters'])
        self.assertEqual((x['adaptive_decisions'],x['known_decisions']),(10,4))
        self.assertEqual(x['adaptive_heat_coverage'],0.4)
        self.assertAlmostEqual(x['available_snapshot_pfn_hit_ratio'],4/7)

    def test_collection_close_report_and_reload_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a=bundle();b=bundle(10,ns=2_000_000_000)
            for name in ('stats','mapping','policy_stats','placement'):
                (root/name).write_text(a[name])
            pool.parse_bundle(pool.collect(root))
            path=root/'samples.jsonl'
            path.write_text('\n'.join(json.dumps(x) for x in (a,b,bundle(instance=20,ns=3_000_000_000),
                 bundle(20,instance=20,ns=4_000_000_000)))+'\n')
            r=pool.report(path,True)
            self.assertEqual(len(r['segments']),2)
            self.assertEqual(r['segments'][0]['window']['policy']['decisions'],10)
            self.assertEqual(r['segments'][1]['window']['policy']['decisions'],20)
            run=subprocess.run([sys.executable,str(ROOT/'tools/rswap_pool.py'),'report','--samples',str(path),'--settled'],
                               text=True,capture_output=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
            json.loads(run.stdout)


class StoreResultsTests(unittest.TestCase):
    def test_wc_before_post_and_missing_wrong_duplicate_results(self):
        decisions=trace.read_decisions([decision()],((16,32),(16,32)))
        self.assertEqual(check_store_results([result(1),result(0)],decisions)['wc_ok'],1)
        for lines in ([],[result(0)],[result(1)],[result(0),result(0),result(1)],
                      [result(0),result(1,seq=2)],[result(0,-5),result(1)]):
            with self.subTest(lines=lines),self.assertRaises(ValueError): check_store_results(lines,decisions)

    def test_full_numeric_trace_cli_and_loss_rejection(self):
        lines=[event(1,'alloc',offset=1),decision(),result(1),result(0),event(2,'put',offset=1,users=0),
               event(3,'invalidate',offset=1,users=0,live=0),event(4,'free',offset=1,users=0,live=0)]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'trace.log';path.write_text(''.join(lines))
            for layout in ('16:32','16:32,16:32'):
                args=[sys.executable,str(ROOT/'tools/rswap_trace.py'),str(path),'--layout',layout,
                      '--dynamic','--adaptive','--require-empty','--store-results','--json']
                r=subprocess.run(args,text=True,capture_output=True,timeout=5)
                self.assertEqual(r.returncode,0,r.stderr)
                self.assertEqual(json.loads(r.stdout)['store_results']['wc_ok'],1)
            path.write_text(''.join(lines)+'CPU:0 [LOST 1 EVENTS]\n')
            r=subprocess.run(args,text=True,capture_output=True,timeout=5)
            self.assertNotEqual(r.returncode,0)


class HeatPublicationTests(unittest.TestCase):
    def test_actual_generation_not_nearest_snapshot_and_stale_pfn_exclusion(self):
        fixture=heat_fixture.AssociationTests('test_past_only_matching_unknowns_failures_and_run_bounds')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.snapshots([[page(4096,65),page(8192,66,active=0),
                            page(12288,0,active=0,state='swapped',last_pfn=77)],
                           [page(4096,65,active=0)]])
        with fixture.summary.open() as stream:
            summaries=list(csv.DictReader(stream,delimiter='\t'))
        fields=list(summaries[0])+['scan_ms','overrun']
        with fixture.summary.open('w') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields,delimiter='\t');writer.writeheader()
            for row in summaries:
                row.update(scan_ms='0',overrun='0');writer.writerow(row)
        path=fixture.path/'placement.tsv'
        path.write_text('version\tsample_generation\trun_id\tstart_ns\tgeneration\tpublish_ns\trecords\tknown\tmean_ppm\tupload_ms\n'
                        '1\t0\t1\t100\t1\t125\t2\t3\t333333\t0\n'
                        '1\t1\t1\t100\t2\t215\t1\t1\t0\t0\n')
        ds=trace.read_decisions([decision(decision_ns=225,publish_ns=125,mean_ppm=333333),
              decision(alloc_seq=3,pfn=77,decision_ns=225,publish_ns=125,mean_ppm=333333,
                       reason=4,heat_ppm=-1,preferred_server=1,server=1),
              decision(alloc_seq=5,generation=0,publish_ns=0,decision_ns=110,reason=2,
                       heat_ppm=-1,mean_ppm=-1,preferred_server=1,server=1)],((16,32),(16,32)))
        r=check_heat(ds,fixture.path,fixture.path/'detail.tsv')
        self.assertEqual((r['known_pfn_consistent'],r['pfn_miss_consistent']),(1,1))
        self.assertEqual(r['no_generation'],1)
        with (fixture.path/'detail.tsv').open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream,delimiter='\t'))),3)
        self.assertEqual(hotness_summary(fixture.path)['publications'],2)
        bad=copy.deepcopy(ds);bad[1]['heat_ppm']=0
        with self.assertRaises(ValueError): check_heat(bad,fixture.path)
        bad=copy.deepcopy(ds);bad[1]['publish_ns']=126
        with self.assertRaises(ValueError): check_heat(bad,fixture.path)
        bad=copy.deepcopy(ds);bad[3]['pfn']=65
        with self.assertRaises(ValueError): check_heat(bad,fixture.path)


if __name__ == '__main__':
    unittest.main()

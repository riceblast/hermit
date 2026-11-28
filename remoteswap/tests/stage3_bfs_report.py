"""Per-committed-EWMA heat and remote occupancy evidence for stage-3 tests."""
from collections import deque
import csv
import json
import math

from rswap_pool import parse_bundle

SCALE = 1 << 32
WARMUP_GROUPS = 5
ADAPTIVE_REVIEW_GROUPS = 10


def occupancy(parsed):
    pools = parsed['pools']
    dram = sum(p['used'] for (s, tier), p in pools.items() if tier == 'dram')
    pmem = sum(p['used'] for (s, tier), p in pools.items() if tier == 'pmem')
    divisor = math.gcd(dram, pmem)
    ratio = f'{dram//divisor}:{pmem//divisor}' if divisor else '0:0'
    normalized = f'1:{pmem/dram:.4g}' if dram else ('0:1' if pmem else '0:0')
    return dict(dram_pages=dram, pmem_pages=pmem,
                dram_gib=dram/262144, pmem_gib=pmem/262144,
                ratio=normalized, ratio_exact=ratio,
                dram_share=dram/(dram+pmem) if dram+pmem else None,
                pmem_share=pmem/(dram+pmem) if dram+pmem else None,
                per_server={f'{s}/{t}': dict(p) for (s, t), p in pools.items()},
                basis='Client-wide occupied Slots = live + retired; not I/O volume')


def heat(audit):
    pages = audit['pages']
    counts = [audit[k] for k in ('hot_pages', 'cold_pages', 'zero_pages')]
    if (pages < 0 or not 0 <= audit['sum_heat'] <= pages*SCALE or audit['unknown'] or
            min(counts) < 0 or counts[0]+counts[1] != pages or
            counts[2] > pages or len(audit['bins']) != 5 or
            min(audit['bins']) < 0 or sum(audit['bins']) != pages):
        raise ValueError('Invalid audited heat population/classification')
    mean = audit['sum_heat']/SCALE/pages if pages else 0
    if not 0 <= mean <= 1:
        raise ValueError('Mean outside [0,1]')
    return dict(pages=pages, mean=mean, mean_display=f'{mean:.2f}',
                hot_pages=counts[0], cold_pages=counts[1], zero_pages=counts[2],
                hot_fraction=counts[0]/pages if pages else 0,
                cold_fraction=counts[1]/pages if pages else 0,
                bins=list(audit['bins']),
                bin_fractions=[n/pages if pages else 0 for n in audit['bins']])


class EwmaReport:
    columns = ('window', 'phase', 'adaptive_ewma', 'elapsed_seconds', 'mean', 'mean_delta',
               'pages', 'hot_pages', 'cold_pages', 'zero_pages', 'hot_fraction', 'cold_fraction',
               'bin0', 'bin1', 'bin2', 'bin3', 'bin4', 'occupancy_observed', 'occupancy_lag_seconds',
               'dram_gib', 'pmem_gib', 'ratio', 'dram_share', 'pmem_share',
               'new_hot_stores', 'new_cold_stores', 'new_tier_fallbacks', 'counter_since_window')

    def __init__(self, directory, baseline, started_ns):
        self.directory, self.started_ns = directory, started_ns
        self.instance = baseline['policy']['header']['instance_ns']
        self.previous_counts = baseline['policy']['counters']
        self.previous_window = None
        self.history = deque(maxlen=4096)
        self.rows = []
        self.binding = None
        with (directory/'bfs.ewma.tsv').open('x') as out:
            csv.writer(out, delimiter='\t').writerow(self.columns)
        (directory/'bfs.ewma.jsonl').touch(exist_ok=False)

    def observe(self, raw):
        parsed = parse_bundle(raw)
        if parsed['policy']['header']['instance_ns'] != self.instance:
            raise ValueError('Client instance changed during BFS observations')
        self.history.append((raw, parsed))
        return parsed

    def append(self, audit):
        window = audit['window']
        if window != len(self.rows):
            raise ValueError('Missing/out-of-order committed EWMA observation')
        if self.binding is not None and self.binding != audit['binding_id']:
            raise ValueError('BFS heat binding changed')
        self.binding = audit['binding_id']
        row = dict(window=window, phase='adaptive' if window >= WARMUP_GROUPS else 'warmup',
                   adaptive_ewma=max(0, window-WARMUP_GROUPS),
                   elapsed_seconds=(audit['mono_ns']-self.started_ns)/1e9,
                   audit_mono_ns=audit['mono_ns'], **heat(audit))
        row['mean_delta'] = row['mean']-self.rows[-1]['mean'] if self.rows else 0
        row.update(occupancy_observed=False, occupancy_lag_seconds=None,
                   dram_gib=None, pmem_gib=None, ratio=None, dram_share=None, pmem_share=None,
                   new_hot_stores=None, new_cold_stores=None, new_tier_fallbacks=None)
        for raw, parsed in self.history:
            state = parsed['state']
            if (raw['begin_ns'] < audit['mono_ns'] or state.get('source') != 'vpn' or
                    int(state.get('run_id', 0)) != self.binding or
                    int(state.get('completed_windows', -1)) != window):
                continue
            counts = parsed['policy']['counters']
            new_hot = counts[('reason', 5)][0]-self.previous_counts[('reason', 5)][0]
            new_cold = counts[('reason', 6)][0]-self.previous_counts[('reason', 6)][0]
            new_fallback = sum(counts[('fallback', i)][0]-self.previous_counts[('fallback', i)][0] for i in (2, 3))
            if min(new_hot, new_cold, new_fallback) < 0:
                raise ValueError('Policy counters decreased')
            row.update(occupancy_observed=True,
                       occupancy_lag_seconds=(raw['end_ns']-audit['mono_ns'])/1e9,
                       collection_begin_ns=raw['begin_ns'], collection_end_ns=raw['end_ns'],
                       occupancy=occupancy(parsed), new_hot_stores=new_hot,
                       new_cold_stores=new_cold, new_tier_fallbacks=new_fallback,
                       counter_since_window=self.previous_window)
            for k in ('dram_gib', 'pmem_gib', 'ratio', 'dram_share', 'pmem_share'):
                row[k] = row['occupancy'][k]
            self.previous_counts = counts
            self.previous_window = window
            break
        if not row['occupancy_observed']:
            row['observation_note'] = 'No active-binding pool sample for this committed window; capacity unavailable, not zero.'
        self.rows.append(row)
        with (self.directory/'bfs.ewma.jsonl').open('a') as out:
            out.write(json.dumps(row)+'\n')
        flat = {k: row.get(k) for k in self.columns}
        for i, count in enumerate(row['bins']):
            flat['bin'+str(i)] = f'{count}({100*row["bin_fractions"][i]:.1f}%)'
        with (self.directory/'bfs.ewma.tsv').open('a') as out:
            csv.DictWriter(out, self.columns, delimiter='\t').writerow(flat)
        return row

    def ready_to_review(self, checkpoint=WARMUP_GROUPS+ADAPTIVE_REVIEW_GROUPS):
        return bool(self.rows and self.rows[-1]['window'] >= checkpoint)

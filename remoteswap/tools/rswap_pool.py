#!/usr/bin/env python3
"""Read-only pool/status collector and module-window report. No swap control."""
import argparse
import json
from pathlib import Path
import sys
import time

from rswap_stats import BaselineChanged, parse_snapshot, positive_interval, rates_between

REASONS = ('inactive', 'warmup', 'no_snapshot', 'no_mean', 'pfn_miss', 'hot', 'cold', 'zero_interleave')
OUTCOMES = ('decisions', 'post_ok', 'post_error', 'wc_ok', 'wc_error')
TIERS = ('dram', 'pmem')


def parse_policy(text):
    try:
        lines = text.strip().splitlines()
        head = dict(x.split('=', 1) for x in lines[0].split())
        if len(lines[0].split()) != 5 or set(head) != {'version', 'instance_ns', 'sample_ns', 'nr_servers', 'page_size'}:
            raise ValueError('invalid header')
        head = {k: int(v) for k, v in head.items()}
        if head['version'] not in (1, 2) or head['nr_servers'] not in (1, 2) or min(head.values()) <= 0:
            raise ValueError('unsupported metadata')
        reason_count = 7 if head['version'] == 1 else 8
        counters = {}
        for line in lines[1:]:
            words = line.split()
            kind = words[0]
            values = tuple(map(int, words[1:]))
            if min(values) < 0:
                raise ValueError('negative counter')
            if kind == 'reason' and len(values) == 6 and values[0] < reason_count:
                key, row = ('reason', values[0]), values[1:]
            elif kind == 'pool' and len(values) == 4 and values[0] < head['nr_servers'] and values[1] < 2:
                key, row = ('pool', values[0], values[1]), values[2:]
            elif kind in ('age', 'fallback') and len(values) == 2 and values[0] < 4:
                key, row = (kind, values[0]), values[1:]
            elif kind == 'allocation_error' and len(values) == 1:
                key, row = (kind,), values
            else:
                raise ValueError('invalid row')
            if key in counters:
                raise ValueError('duplicate counter')
            counters[key] = row
        expected = ({('reason', r) for r in range(reason_count)} | {('pool', s, t) for s in range(head['nr_servers']) for t in range(2)} |
                    {(kind, b) for kind in ('age', 'fallback') for b in range(4)} | {('allocation_error',)})
        if counters.keys() != expected:
            raise ValueError('incomplete counters')
        return dict(header=head, counters=counters)
    except (ValueError, IndexError) as exc:
        raise ValueError('Invalid policy_stats: ' + str(exc)) from exc


def parse_mapping(text):
    try:
        lines = text.strip().splitlines()
        head = dict(x.split('=', 1) for x in lines[0].split())
        if set(head) != {'version', 'pages', 'metadata_bytes', 'sequence'} or int(head['version']) != 1:
            raise ValueError('unsupported header')
        if lines[1] != 'server tier capacity live retired free':
            raise ValueError('invalid columns')
        pools = {}
        for line in lines[2:]:
            s, tier, *numbers = line.split()
            s = int(s)
            cap, live, retired, free = map(int, numbers)
            if s not in (0, 1) or tier not in TIERS or min(cap, live, retired, free) < 0 or not cap or cap != live + retired + free:
                raise ValueError('invalid capacity')
            key = (s, tier)
            if key in pools:
                raise ValueError('duplicate pool')
            pools[key] = dict(capacity=cap, live=live, retired=retired, free=free, used=live+retired)
        servers = {s for s, _ in pools}
        if servers not in ({0}, {0, 1}) or pools.keys() != {(s, t) for s in servers for t in TIERS}:
            raise ValueError('incomplete topology')
        if sum(p['capacity'] for p in pools.values()) != int(head['pages']):
            raise ValueError('capacity sum mismatch')
        return pools
    except (ValueError, IndexError) as exc:
        raise ValueError('Invalid mapping: ' + str(exc)) from exc


def parse_bundle(raw):
    if raw.get('version') != 1 or not 0 < raw['begin_ns'] <= raw['end_ns']:
        raise ValueError('Invalid collection metadata')
    stats, policy = parse_snapshot(raw['stats']), parse_policy(raw['policy_stats'])
    pools = parse_mapping(raw['mapping'])
    state = dict(x.split('=', 1) for x in raw['placement'].split() if '=' in x)
    if state.get('version') != '1' or state.get('state') not in ('inactive', 'warmup', 'adaptive'):
        raise ValueError('Invalid placement state')
    if stats.instance_ns != policy['header']['instance_ns']:
        raise BaselineChanged('module changed during collection')
    expected = {(s, t) for s in range(policy['header']['nr_servers']) for t in TIERS}
    if pools.keys() != expected or {(s, t) for s, t, op in stats.rows} != expected or stats.page_size != policy['header']['page_size']:
        raise ValueError('Topology/page-size mismatch')
    return dict(raw=raw, stats=stats, policy=policy, pools=pools, state=state)


def collect(root):
    begin = time.monotonic_ns()
    # Each file is closed immediately. No open module reference while sleeping.
    raw = {name: (root / name).read_text() for name in ('stats', 'placement', 'policy_stats')}
    # Human-readable mapping rounds capacities; collectors need exact counts.
    # Old Clients only expose the original integer mapping file.
    try:
        raw['mapping'] = (root / 'mapping_raw').read_text()
    except FileNotFoundError:
        raw['mapping'] = (root / 'mapping').read_text()
    raw.update(version=1, begin_ns=begin, end_ns=time.monotonic_ns())
    parse_bundle(raw)
    return raw


def counter_delta(before, after):
    if before['header']['instance_ns'] != after['header']['instance_ns'] or before['counters'].keys() != after['counters'].keys():
        raise BaselineChanged('policy module/topology changed')
    result = {}
    for key, current in after['counters'].items():
        delta = tuple(a-b for a, b in zip(current, before['counters'][key]))
        if min(delta) < 0:
            raise BaselineChanged('policy counters decreased')
        result[key] = delta
    return result


def policy_summary(counts):
    reasons = {name: dict(zip(OUTCOMES, counts.get(('reason', i), (0, 0, 0, 0, 0)))) for i, name in enumerate(REASONS)}
    adaptive = sum(reasons[n]['decisions'] for n in REASONS[2:])
    classified = reasons['hot']['decisions'] + reasons['cold']['decisions']
    known = classified + reasons['zero_interleave']['decisions']
    lookup = known + reasons['pfn_miss']['decisions']
    return dict(reasons=reasons, decisions=sum(r['decisions'] for r in reasons.values()),
                adaptive_decisions=adaptive, known_decisions=known, classified_decisions=classified,
                positive_heat_coverage=(classified/adaptive if adaptive else None) if ('reason', 7) in counts else None,
                adaptive_heat_coverage=known/adaptive if adaptive else None,
                available_snapshot_pfn_hit_ratio=known/lookup if lookup else None,
                pools={str(s)+'/'+TIERS[t]: dict(preferred=v[0], allocated=v[1])
                       for (kind, *rest), v in counts.items() if kind == 'pool' for s, t in [rest]},
                fallback={str(i): counts[('fallback', i)][0] for i in range(4)},
                tier_fallback=sum(counts[('fallback', i)][0] for i in (2, 3)),
                snapshot_age_buckets={name: counts[('age', i)][0] for i, name in enumerate(('le_1s', '1_to_5s', '5_to_20s', 'gt_20s'))},
                allocation_error=counts[('allocation_error',)][0])


def validate_settled(policy):
    c = policy['counters']
    decisions = sum(c[('reason', r)][0] for r in range(7 if policy['header']['version'] == 1 else 8))
    allocated = sum(v[1] for k, v in c.items() if k[0] == 'pool')
    post = sum(c[('reason', r)][1] + c[('reason', r)][2] for r in range(7 if policy['header']['version'] == 1 else 8))
    if (decisions != allocated + c[('allocation_error',)][0] or allocated != post or
            sum(v[0] for k, v in c.items() if k[0] == 'pool') != decisions or
            sum(c[('fallback', i)][0] for i in range(4)) != allocated):
        raise ValueError('Policy counters not settled/consistent')
    for r in range(7 if policy['header']['version'] == 1 else 8):
        d, accepted, failed, ok, error = c[('reason', r)]
        if ok + error != accepted or accepted + failed > d:
            raise ValueError('Store outcomes not settled/consistent')


def status(raw):
    b = parse_bundle(raw)
    topology = {}
    for s in range(b['policy']['header']['nr_servers']):
        dram = b['pools'][s, 'dram']['capacity']
        pmem = b['pools'][s, 'pmem']['capacity']
        topology[str(s)] = dict(capacity_bytes=(dram+pmem)*b['stats'].page_size,
                                dram_fraction=dram/(dram+pmem), pmem_fraction=pmem/(dram+pmem))
    return dict(instance_ns=b['stats'].instance_ns, collection_begin_ns=raw['begin_ns'], collection_end_ns=raw['end_ns'],
                server_count=len(topology), topology=topology,
                state=b['state'], pools={str(s)+'/'+t: p for (s, t), p in b['pools'].items()},
                capacity_bytes=sum(p['capacity'] for p in b['pools'].values()) * b['stats'].page_size,
                io_counts={str(s)+'/'+t+'/'+op: dict(zip(('ok','wc_error','submit_error'), v))
                           for (s,t,op),v in b['stats'].rows.items()},
                policy=policy_summary(b['policy']['counters']),
                note='Module cumulative counters; used=live+retired, not exact Frontswap valid pages.')


def window(first, last):
    a, b = parse_bundle(first), parse_bundle(last)
    seconds, rates, unrouted = rates_between(a['stats'], b['stats'])
    choices = policy_summary(counter_delta(a['policy'], b['policy']))
    wc = {str(s)+'/'+t+'/'+op: b['stats'].rows[s, t, op][0]-value[0]
          for (s, t, op), value in a['stats'].rows.items()}
    stable = all(a['state'].get(k) == b['state'].get(k) for k in ('state', 'run_id'))
    return dict(instance_ns=a['stats'].instance_ns, seconds=seconds,
                endpoint_state=a['state']['state'] if stable else 'mixed', policy=choices,
                current=status(last),
                bandwidth_MiB_s={str(s)+'/'+t: row for (s, t), row in rates.items()},
                wc_ok=wc, unrouted_error_delta=unrouted,
                note='Client-wide window; bandwidth uses WC completion time. Endpoint state labels do not attribute all I/O to that phase.')


def report(path, settled=False):
    segments = []
    first = last = None
    samples = 0
    with path.open() as stream:
        for line in stream:
            raw = json.loads(line)
            current = parse_bundle(raw)
            if last is not None:
                previous = parse_bundle(last)
                if current['stats'].instance_ns != previous['stats'].instance_ns:
                    segments.append(dict(samples=samples, window=window(first, last) if samples > 1 else None,
                                         final=status(last), settled_checked=False))
                    first = last = None
                    samples = 0
                else:
                    counter_delta(previous['policy'], current['policy'])
                    rates_between(previous['stats'], current['stats'])
            if first is None:
                first = raw
            last = raw
            samples += 1
    if last is None:
        raise ValueError('No collected samples')
    if settled:
        validate_settled(parse_bundle(last)['policy'])
    segments.append(dict(samples=samples, window=window(first, last) if samples > 1 else None,
                         final=status(last), settled_checked=settled))
    return dict(version=1, segments=segments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'monitor', 'report'))
    parser.add_argument('--root', type=Path, default=Path('/sys/kernel/debug/rswap'))
    parser.add_argument('--interval', type=positive_interval, default=1)
    parser.add_argument('--out', type=Path, help='new collection directory, monitor only')
    parser.add_argument('--samples', type=Path, help='samples.jsonl, report only')
    parser.add_argument('--settled', action='store_true', help='check final cumulative equations after submissions/drain have stopped')
    parser.add_argument('--heat-dir', type=Path, help='optional sampler summary/publication directory, report only')
    args = parser.parse_args()
    try:
        if args.action == 'report':
            if args.samples is None:
                parser.error('report requires --samples')
            result = report(args.samples, args.settled)
            if args.heat_dir:
                from rswap_analysis import hotness_summary
                result['sampling'] = hotness_summary(args.heat_dir)
            print(json.dumps(result, indent=2))
            return 0
        if args.action == 'status':
            print(json.dumps(status(collect(args.root)), indent=2))
            return 0
        log = None
        if args.out:
            args.out.mkdir(mode=0o755)
            log = (args.out / 'samples.jsonl').open('x')
        try:
            before = None
            while True:
                raw = collect(args.root)
                if log:
                    log.write(json.dumps(raw)+'\n'); log.flush()
                try:
                    value = window(before, raw) if before else status(raw)
                except BaselineChanged:
                    value = status(raw)
                    value['new_baseline'] = True
                print(json.dumps(value), flush=True)
                before = raw
                time.sleep(args.interval)
        finally:
            if log:
                log.close()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('rswap pool: '+str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

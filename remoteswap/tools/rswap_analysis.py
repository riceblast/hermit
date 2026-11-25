#!/usr/bin/env python3
"""Publication-aware analysis for short detailed traces or summary-only runs."""
from collections import Counter, defaultdict
import csv
import math
from pathlib import Path

from rswap_hotness_check import read_pages, read_summary, raw_log_directory


def tsv(path):
    with Path(path).open() as stream:
        result = list(csv.DictReader(stream, delimiter='\t'))
    if not result or any(None in row.values() for row in result):
        raise ValueError('Empty/incomplete TSV: ' + str(path))
    return result


def publications(directory):
    directory = raw_log_directory(directory)
    runs = tsv(directory / 'run.tsv')
    if len(runs) != 1:
        raise ValueError('One completed monitor run required')
    run = runs[0]
    stopped, exit_code = int(run['stop_signal']), int(run['exit_status'])
    if (int(run['version']) != 1 or int(run['monitor_error']) or
            (exit_code and not (stopped in (2, 15) and exit_code == 128 + stopped))):
        raise ValueError('Monitor/workload did not finish successfully')
    start, end = int(run['start_ns']), int(run['end_ns'])
    if not 0 < start <= end or not 1 <= int(run['window']) <= 64:
        raise ValueError('Invalid run metadata')
    summaries = tsv(directory / 'summary.tsv')
    if summaries[0].get('version') != '1':
        raise ValueError('Full publication analysis requires --raw-logs; use the precise raw/ logs')
    for generation, row in enumerate(summaries):
        if (int(row['version']) != 1 or int(row['pid']) != int(run['pid']) or
                int(row['generation']) != generation or int(row['start_ns']) != start or
                not start <= int(row['begin_ns']) <= int(row['end_ns']) <= int(row['ready_ns']) <= end or
                row['phase'] not in ('warmup', 'observe') or
                int(row['overrun']) not in (0, 1) or
                not math.isfinite(float(row['scan_ms'])) or float(row['scan_ms']) < 0 or
                int(row['known']) < 0 or (not 0 <= float(row['mean']) <= 1 if int(row['known']) else float(row['mean']) != -1)):
            raise ValueError('Invalid sampling summary')
    result, previous, run_id = {}, 0, None
    for row in tsv(directory / 'placement.tsv'):
        gen, sample = int(row['generation']), int(row['sample_generation'])
        if sample < 0 or sample >= len(summaries):
            raise ValueError('Publication references absent sample')
        summary = summaries[sample]
        identity = int(row['run_id'])
        if (int(row['version']) not in (1, 2) or identity <= 0 or gen != sample+1 or gen <= previous or
                (run_id is not None and identity != run_id) or int(row['start_ns']) != start or
                int(row['known']) != int(summary['known']) or
                not 0 <= int(row['records']) <= int(row['known']) or
                not int(summary['end_ns']) <= int(row['publish_ns']) <= int(summary['ready_ns']) or
                not -1 <= int(row['mean_ppm']) <= 1000000 or
                not math.isfinite(float(row['upload_ms'])) or float(row['upload_ms']) < 0):
            raise ValueError('Invalid/inconsistent publication')
        mean = int(row['mean_ppm'])
        if (int(row['known']) == 0 and mean != -1) or (int(row['known']) and
                (mean < 0 or abs(mean/1000000-float(summary['mean'])) > 0.000001001)):
            raise ValueError('Publication/summary mean mismatch')
        previous, run_id = gen, identity
        result[gen] = row
    from rswap_threshold_check import validate_thresholds
    validate_thresholds(directory, result)
    return run, summaries, result


def display_threshold(row):
    if int(row['version']) != 2:
        return None
    result = {key: int(row[key]) for key in
              ('policy', 'threshold_source', 'threshold_generation', 'population_pages',
               'threshold_sample_pages', 'candidate_pages', 'local_pages')}
    ppm = int(row['threshold_ppm'])
    value = (ppm + 5000) // 10000
    result['threshold'] = '-' if ppm < 0 else f'{value // 100}.{value % 100:02d}'
    return result


def hotness_summary(directory):
    run, summaries, pubs = publications(directory)
    return dict(rounds=len(summaries), publications=len(pubs), run_id=int(next(iter(pubs.values()))['run_id']),
                phases=dict(Counter(r['phase'] for r in summaries)),
                monitor_seconds=(int(run['end_ns'])-int(run['start_ns']))/1e9,
                overrun=sum(int(r['overrun']) for r in summaries),
                max_scan_ms=max(float(r['scan_ms']) for r in summaries),
                max_upload_ms=max(float(r['upload_ms']) for r in pubs.values()),
                max_known=max(int(r['known']) for r in summaries),
                max_resident_records=max(int(r['records']) for r in pubs.values()),
                threshold=display_threshold(list(pubs.values())[-1]),
                note='Known history pages differ from uploaded resident PFNs; neither is a kernel Store hit rate.')


def check_heat(decisions, directory, detail=None):
    """Use the generation actually acquired by the kernel, not nearest ready_ns.

    Only one PFN dictionary at a time; all detailed analysis is for short traces.
    A matched PFN remains an approximate identity, not proof of page ownership.
    """
    directory = raw_log_directory(directory)
    run, summaries, pubs = publications(directory)
    snapshots = {s.generation: s for s in read_summary(directory / 'summary.tsv')}
    groups = defaultdict(list)
    for d in decisions.values():
        if d['generation']:
            groups[d['generation']].append(d)
    totals = Counter()
    from rswap_threshold_check import distribution
    model_histograms = distribution(directory) if any(int(p['version']) == 2 for p in pubs.values()) else {}
    writer = None
    stream = Path(detail).open('x') if detail else None
    try:
        if stream:
            fields = ['alloc_seq', 'run', 'generation', 'placement_rule', 'pfn', 'va', 'reason', 'heat_ppm', 'mean_ppm',
                      'snapshot_age_ns', 'threshold_ppm', 'policy', 'threshold_source', 'threshold_generation', 'threshold_status', 'preferred_server', 'preferred_tier', 'server', 'tier', 'slot', 'fallback']
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter='\t')
            writer.writeheader()
        for generation, records in sorted(groups.items()):
            if generation not in pubs:
                raise ValueError('Missing actual kernel generation')
            pub = pubs[generation]
            snapshot = snapshots[int(pub['sample_generation'])]
            # Reuse Patch 8's production validator for VA/history/aliases/mean.
            pfns = read_pages(snapshot)
            model = Counter()
            with snapshot.path.open() as src:
                for row in csv.DictReader(src, delimiter='\t'):
                    if row['state'] in ('resident', 'swapped') and row['alias'] == '0':
                        model[int(row['heat_ppm'])] += 1
                    if row['state'] == 'resident' and row['alias'] == '0':
                        pfn = int(row['pfn'])
                        if pfn not in pfns:
                            raise ValueError('Invalid representative PFN')
                        if int(row['heat_ppm']) >= 0:
                            pfns[pfn] = (int(row['va'], 16), int(row['heat_ppm']))
                        else:
                            del pfns[pfn]
            if int(pub['version']) == 2:
                if dict(model) != model_histograms.get(int(pub['sample_generation']), {}):
                    raise ValueError('Threshold histogram differs from page snapshot')
            if len(pfns) != int(pub['records']):
                raise ValueError('Upload record count differs from page snapshot')
            for d in records:
                if (d['run'] != int(pub['run_id']) or d['publish_ns'] != int(pub['publish_ns']) or
                        d['mean_ppm'] != int(pub['mean_ppm']) or d['decision_ns'] < d['publish_ns']):
                    raise ValueError('Decision/publication mismatch')
                if 'threshold_ppm' in d:
                    if int(pub['version']) != 2 or any(d[k] != int(pub[k]) for k in
                            ('threshold_ppm', 'policy', 'threshold_source', 'threshold_generation', 'threshold_status')):
                        raise ValueError('Decision/threshold publication mismatch')
                elif int(pub['version']) == 2:
                    raise ValueError('New publication paired with legacy decision')
                frame = pfns.get(d['pfn'])
                if d['reason'] in (5, 6, 7):
                    if frame is None or d['heat_ppm'] != frame[1]:
                        raise ValueError('Decision/PFN heat mismatch')
                    totals['known_pfn_consistent'] += 1
                    if d['reason'] == 7: totals['zero_interleave_consistent'] += 1
                elif d['reason'] == 4:
                    if frame is not None:
                        raise ValueError('PFN miss was present in uploaded table')
                    totals['pfn_miss_consistent'] += 1
                totals['publication_consistent'] += 1
                if writer:
                    row = {k: d[k] for k in writer.fieldnames if k in d}
                    row.update(va=hex(frame[0]) if frame else '', snapshot_age_ns=d['decision_ns']-d['publish_ns'])
                    writer.writerow(row)
        for d in decisions.values():
            if not d['generation']:
                totals['no_generation'] += 1
                if writer:
                    row = {k: d[k] for k in writer.fieldnames if k in d}
                    row.update(va='', snapshot_age_ns='')
                    writer.writerow(row)
    finally:
        if stream:
            stream.close()
    return dict(totals, note='Actual generation and integer ppm checked; PFN identity remains approximate.')


def check_store_results(lines, decisions):
    outcomes = {}
    for line in lines:
        if 'LOST' in line and 'EVENT' in line:
            raise ValueError('ftrace reports lost events')
        if 'store_result:' not in line:
            continue
        try:
            words = line.split('store_result:', 1)[1].split()
            fields = dict(x.split('=', 1) for x in words)
            if len(words) != 6 or set(fields) != {'alloc_seq', 'offset', 'server', 'tier', 'stage', 'status'}:
                raise ValueError('invalid columns')
            r = {k: int(v) for k, v in fields.items()}
            d = decisions.get(r['alloc_seq'])
            if (d is None or r['stage'] not in (0, 1) or r['status'] > 0 or
                    (r['offset'], r['server'], r['tier']) != (d['offset'], d['server'], d['tier'])):
                raise ValueError('identity/status mismatch')
            stages = outcomes.setdefault(r['alloc_seq'], {})
            if r['stage'] in stages:
                raise ValueError('duplicate result')
            stages[r['stage']] = r['status']
        except (ValueError, KeyError) as exc:
            raise ValueError('Malformed Store result: '+str(exc)) from exc
    totals = Counter()
    for key in decisions:
        stages = outcomes.get(key, {})
        if 0 not in stages or (stages[0] == 0 and 1 not in stages) or (stages[0] != 0 and 1 in stages):
            raise ValueError('Incomplete/inconsistent Store chain')
        totals['post_ok' if stages[0] == 0 else 'post_error'] += 1
        if 1 in stages:
            totals['wc_ok' if stages[1] == 0 else 'wc_error'] += 1
    return dict(totals, allocations=len(decisions), note='Post acceptance and WC result, not current entry validity.')

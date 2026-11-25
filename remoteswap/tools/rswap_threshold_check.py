#!/usr/bin/env python3
"""Recompute ABI-v2 thresholds from compact, exact heat histograms."""
from collections import defaultdict
import csv
from pathlib import Path

FIELDS = ('policy', 'threshold_ppm', 'threshold_source', 'threshold_generation',
          'threshold_status', 'population_pages', 'threshold_sample_pages',
          'candidate_pages', 'local_pages', 'min_pages', 'min_coverage_ppm')
HEATS = {a * 1000000 // v for v in range(1, 65) for a in range(v + 1)}


def distribution(directory):
    from rswap_hotness_check import raw_log_directory
    directory = raw_log_directory(directory)
    result = defaultdict(dict)
    previous = -1
    with (Path(directory) / 'distribution.tsv').open() as src:
        for row in csv.DictReader(src, delimiter='\t'):
            version, gen, heat, pages = (int(row[k]) for k in
                                        ('version', 'sample_generation', 'heat_ppm', 'pages'))
            if (version != 1 or gen < previous or gen < 0 or pages <= 0 or
                    (heat != -1 and heat not in HEATS) or heat in result[gen]):
                raise ValueError('Invalid threshold distribution')
            result[gen][heat] = pages
            previous = gen
    return result


def metadata(row):
    return {key: int(row[key]) for key in FIELDS}


def validate_thresholds(directory, pubs):
    modern = [p for p in pubs.values() if int(p['version']) == 2]
    if not modern:
        return
    if len(modern) != len(pubs):
        raise ValueError('Mixed publication versions in one run')
    histograms = distribution(directory)
    previous = None
    for row in modern:
        m = metadata(row)
        gen, mean = int(row['generation']), int(row['mean_ppm'])
        pop, known, local = m['population_pages'], m['threshold_sample_pages'], m['local_pages']
        hist = histograms.get(int(row['sample_generation']), {})
        if (m['policy'] not in (0, 1) or min(pop, known, local, m['candidate_pages'], m['min_pages']) < 0 or
                not 0 <= m['min_coverage_ppm'] <= 1000000 or
                known > pop or known > int(row['known']) or int(row['records']) > known or
                sum(hist.values()) != pop or sum(n for h, n in hist.items() if h >= 0) != known):
            raise ValueError('Threshold population/distribution mismatch')
        threshold, origin, source, status = mean, gen if mean >= 0 else 0, 0, 0
        candidate = known
        if m['policy'] == 1:
            if m['min_pages'] <= 0:
                raise ValueError('Invalid threshold minimum sample size')
            candidate = 0
            if pop <= local:
                status = 2
            elif not known or known * 1000000 < pop * m['min_coverage_ppm']:
                status = 1
            else:
                candidate = (known * (pop - local) + pop - 1) // pop
                status = 3 if min(known, candidate) < m['min_pages'] else 0
            if not status:
                remaining, weighted = candidate, 0
                for heat, count in sorted(hist.items()):
                    if heat < 0:
                        continue
                    take = min(count, remaining)
                    weighted += heat * take
                    remaining -= take
                if remaining:
                    raise ValueError('Incomplete threshold histogram')
                threshold, source = weighted // candidate, 1
            elif previous and previous['policy'] == 1 and previous['threshold_ppm'] >= 0:
                source, threshold, origin = 2, previous['threshold_ppm'], previous['threshold_generation']
            else:
                source = 3
        expected = (threshold, origin, source, status, candidate)
        actual = (m['threshold_ppm'], m['threshold_generation'], m['threshold_source'],
                  m['threshold_status'], m['candidate_pages'])
        if expected != actual:
            raise ValueError('Threshold calculation/source mismatch: expected %s, got %s' % (expected, actual))
        previous = m

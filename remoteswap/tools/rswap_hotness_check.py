#!/usr/bin/env python3
"""Match Store submissions to previously completed hotness snapshots (offline)."""
import argparse
from collections import Counter
import csv
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys


EVENT = re.compile(
    r"\bstore_sample:\s+start_ns=(\d+) pfn=(\d+) swap_type=(\d+) "
    r"offset=(\d+) submit_ret=(-?\d+)\s*$"
)


@dataclass(frozen=True)
class Store:
    ns: int
    pfn: int
    area: int
    offset: int
    ret: int


@dataclass(frozen=True)
class Snapshot:
    generation: int
    ready_ns: int
    begin_ns: int
    end_ns: int
    phase: str
    known: int
    mean: float
    path: Path
    run_start_ns: int
    run_end_ns: int


def raw_log_directory(directory):
    """Prefer optional exact sidecars; never certify rounded display records."""
    directory = Path(directory)
    raw = directory / 'raw'
    return raw if (raw / 'summary.tsv').is_file() else directory


VPN_HEADER = ['pid', 'EWMA_window', 'mean_hotness', 'pages',
              'dist[0 - 0.2]', 'dist[0.2 - 0.4]', 'dist[0.4 - 0.6]',
              'dist[0.6 - 0.8]', 'dist[0.8 - 1.0]']


def read_vpn_summary(path):
    """Validate phase-2 display statistics, without claiming PFN/Store matches."""
    result = []
    pid = None
    elapsed = -1.0
    with Path(path).open() as stream:
        reader = csv.reader(stream, delimiter='\t')
        if next(reader, None) != VPN_HEADER:
            raise ValueError('expected phase-2 VPN summary header')
        for fields in reader:
            if len(fields) != 9:
                raise ValueError('expected nine summary columns')
            row = dict(zip(VPN_HEADER, (f.strip() for f in fields)))
            current_pid, pages = int(row['pid']), int(row['pages'])
            mean = float(row['mean_hotness'])
            label = re.fullmatch(r'(\d+)\((\d+(?:\.\d+)?)s\)', row['EWMA_window'])
            if not label:
                raise ValueError('invalid EWMA window/time')
            window, seconds = int(label[1]), float(label[2])
            if (current_pid <= 0 or (pid is not None and pid != current_pid) or pages < 0 or
                    not 0 <= mean <= 1 or window != len(result) or seconds <= elapsed or
                    (window == 0 and (seconds != 0 or mean != 0)) or (not pages and mean != 0)):
                raise ValueError('invalid summary identity, sequence or mean')
            counts = []
            for key in VPN_HEADER[4:]:
                cell = re.fullmatch(r'(\d+)\((\d+\.\d)%\)', row[key])
                if not cell:
                    raise ValueError('invalid heat distribution cell')
                count, percent = int(cell[1]), float(cell[2])
                expected = count * 100 / pages if pages else 0
                if count > pages or abs(percent - expected) > .050001:
                    raise ValueError('heat distribution percentage mismatch')
                counts.append(count)
            if sum(counts) != pages or (window == 0 and counts[0] != pages):
                raise ValueError('heat distribution population mismatch')
            result.append(row)
            pid, elapsed = current_pid, seconds
    if not result:
        raise ValueError('no completed VPN snapshots')
    return result


def read_summary(path):
    path = Path(path)
    path = raw_log_directory(path.parent) / path.name
    snapshots = []
    identity = None
    with (path.parent / "run.tsv").open() as stream:
        runs = list(csv.DictReader(stream, delimiter="\t"))
    if len(runs) != 1:
        raise ValueError("one completed monitor run required")
    run = runs[0]
    run_start, run_end = int(run["start_ns"]), int(run["end_ns"])
    stopped = int(run["stop_signal"])
    status = int(run["exit_status"])
    if (int(run["version"]) != 1 or run_start <= 0 or run_end < run_start or
            not 1 <= int(run["window"]) <= 64 or int(run["monitor_error"]) != 0 or
            (status != 0 and not (stopped in (2, 15) and status == 128 + stopped))):
        raise ValueError("invalid or unsuccessful monitor run")
    with path.open() as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            if row.get('version') != '1':
                raise ValueError('Full heat validation requires --raw-logs; use the precise raw/ logs')
            version, pid, generation, start, begin, end, ready, known = (
                int(row[key]) for key in ("version", "pid", "generation", "start_ns",
                                         "begin_ns", "end_ns", "ready_ns", "known"))
            current = (pid, start)
            mean = float(row["mean"])
            name = row["pages_file"]
            if (version != 1 or pid != int(run["pid"]) or start != run_start or
                    not start <= begin <= end <= ready <= run_end or
                    generation != len(snapshots) or known < 0 or
                    (not 0 <= mean <= 1 if known else mean != -1) or
                    row["phase"] not in ("warmup", "observe") or
                    name != f"pages-{generation:06d}.tsv" or
                    (snapshots and begin < snapshots[-1].ready_ns) or
                    (identity is not None and identity != current)):
                raise ValueError("invalid/incomplete summary; --pages and one monitor run required")
            identity = current
            snapshots.append(Snapshot(generation, ready, begin, end, row["phase"],
                                      known, mean, path.parent / name, run_start, run_end))
    if not snapshots:
        raise ValueError("no completed snapshots")
    return snapshots


def read_pages(snapshot):
    """One current PFN table at a time; last_pfn is never treated as current."""
    pfns = {}
    known = 0
    heat_sum = 0.0
    previous_va = -1
    with snapshot.path.open() as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            va = int(row["va"], 16)
            pfn, active, valid, ppm, alias, area, offset = (
                int(row[key]) for key in ("pfn", "active", "valid", "heat_ppm", "alias",
                                         "swap_type", "swap_offset"))
            state = row["state"]
            if (va <= previous_va or va % 4096 or pfn < 0 or not 0 <= active <= valid <= 64 or
                    ppm != (active * 1000000 // valid if valid else -1) or alias not in (0, 1) or
                    state not in ("resident", "swapped", "changed") or
                    (state == "resident" and pfn == 0) or
                    (state != "resident" and pfn != 0) or
                    (state == "swapped" and not 0 <= area <= 31) or
                    (state != "swapped" and (area != -1 or offset != 0))):
                raise ValueError(f"invalid page row in {snapshot.path}")
            previous_va = va
            if alias:
                continue
            heat = active / valid if valid else None
            if heat is not None:
                known += 1
                heat_sum += heat
            if state == "resident":
                if pfn in pfns:
                    raise ValueError("duplicate representative PFN")
                pfns[pfn] = (va, heat)
    if known != snapshot.known or (known and abs(heat_sum / known - snapshot.mean) > 1e-8):
        raise ValueError("page snapshot and summary mean/count disagree")
    return pfns


def read_stores(lines):
    stores = []
    for number, line in enumerate(lines, 1):
        if "LOST" in line and "EVENT" in line:
            raise ValueError("ftrace reports lost events")
        if "store_sample:" not in line:
            continue
        match = EVENT.search(line)
        if not match:
            raise ValueError(f"line {number}: malformed store_sample event")
        store = Store(*map(int, match.groups()))
        if store.ns <= 0 or store.pfn <= 0 or store.area > 31 or store.ret > 0:
            raise ValueError(f"line {number}: invalid store_sample fields")
        stores.append(store)
    if not stores:
        raise ValueError("no Store observation events")
    # Per-CPU output order and time spent submitting need not match start_ns.
    return sorted(stores, key=lambda store: store.ns)


def check_buffer_stats(path):
    counts = Counter()
    for line in Path(path).read_text().splitlines():
        match = re.fullmatch(r"\s*(overrun|commit overrun|dropped events):\s*(\d+)\s*", line)
        if match:
            key, value = match.groups()
            counts[key] += 1
            if int(value):
                raise ValueError(f"ftrace buffer loss: {key}={value}")
    if set(counts) != {"overrun", "commit overrun", "dropped events"} or len(set(counts.values())) != 1:
        raise ValueError("incomplete per-CPU ftrace buffer statistics")


def correlate(snapshots, stores, detail=None):
    counts = Counter()
    index = -1
    pfns = {}
    age_sum = 0
    max_age = 0
    writer = csv.writer(detail, delimiter="\t") if detail else None
    if writer:
        writer.writerow(("start_ns", "pfn", "swap_type", "offset", "submit_ret", "reason",
                         "generation", "va", "heat", "mean", "snapshot_age_ms"))
    for store in stores:
        while index + 1 < len(snapshots) and snapshots[index + 1].ready_ns <= store.ns:
            index += 1
        snap = snapshots[index] if index >= 0 else None
        counts["store_events"] += 1
        va = heat = age = None
        if store.ret:
            reason = "submit_failed"
        else:
            counts["accepted"] += 1
            in_run = snapshots[0].run_start_ns <= store.ns <= snapshots[0].run_end_ns
            if in_run:
                counts["accepted_in_run"] += 1
            if not in_run:
                reason = "outside_run"
            elif snap is None:
                reason = "no_prior_snapshot"
            else:
                if counts["loaded_generation"] != index + 1:
                    pfns = read_pages(snap)
                    counts["loaded_generation"] = index + 1
                entry = pfns.get(store.pfn)
                if entry is None:
                    reason = "pfn_absent"
                else:
                    va, heat = entry
                    reason = "unknown_heat" if heat is None else (
                        "matched_hot" if heat > snap.mean else "matched_cold")
                counts[f"phase_{snap.phase}"] += 1
                age = store.ns - snap.ready_ns
        counts[reason] += 1
        if reason.startswith("matched_"):
            age_sum += age
            max_age = max(max_age, age)
        if writer:
            writer.writerow((store.ns, store.pfn, store.area, store.offset, store.ret, reason,
                             snap.generation if snap else "", hex(va) if va is not None else "",
                             heat if heat is not None else "", snap.mean if snap else "",
                             age / 1e6 if age is not None else ""))
    counts.pop("loaded_generation", None)
    matched = counts["matched_hot"] + counts["matched_cold"]
    result = dict(counts)
    result.update(matched=matched,
                  in_run_accepted_store_match_ratio=matched / counts["accepted_in_run"] if counts["accepted_in_run"] else None,
                  matched_mean_snapshot_age_ms=age_sum / matched / 1e6 if matched else None,
                  matched_max_snapshot_age_ms=max_age / 1e6 if matched else None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("trace", type=Path, nargs='?')
    parser.add_argument("--buffer-stats", type=Path, help="saved stats from every CPU in a fresh trace instance")
    parser.add_argument("--detail", type=Path, help="write per-Store associations to a new TSV file")
    parser.add_argument("--require-match", action="store_true", help="fail if no accepted Store matches known heat")
    args = parser.parse_args()
    try:
        with args.summary.open() as stream:
            vpn = stream.readline().rstrip('\n').split('\t') == VPN_HEADER
        if vpn:
            if args.trace or args.detail or args.require_match or args.buffer_stats:
                raise ValueError('VPN summary has no PFN/Store correlation; omit legacy trace options')
            rows = read_vpn_summary(args.summary)
            print(json.dumps({'snapshots': len(rows), 'last': rows[-1]}, indent=2))
            return 0
        if args.trace is None:
            raise ValueError('legacy PFN summary requires a Store trace')
        if args.buffer_stats:
            check_buffer_stats(args.buffer_stats)
        snapshots = read_summary(args.summary)
        with args.trace.open() as stream:
            stores = read_stores(stream)
        if args.detail:
            with args.detail.open("x") as detail:
                result = correlate(snapshots, stores, detail)
        else:
            result = correlate(snapshots, stores)
        print(json.dumps(result, indent=2, sort_keys=True))
        print("Ratio denominator: all captured accepted Client Stores during observation, not exact target-PID Stores.")
        print("PFN matches are approximate; submission success is not WC success. No placement policy was applied.")
        if not args.buffer_stats:
            print("Buffer loss not certified: supply --buffer-stats for hardware acceptance.")
        if args.require_match and not result["matched"]:
            raise ValueError("no accepted Store matched a prior known hotness snapshot")
        return 0
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
        print(f"rswap hotness check: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

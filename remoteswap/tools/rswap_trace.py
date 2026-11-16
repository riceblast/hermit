#!/usr/bin/env python3
"""Check rswap:placement completion records against the static pool layout."""
import argparse
from collections import Counter
from pathlib import Path
import re
import sys

from rswap_stats import OPS, TIERS

EVENT = re.compile(
    r"\bplacement:\s+swap_type=(\d+) offset=(\d+) server=(\d+) "
    r"tier=(dram|pmem) op=(write|read_sync|read_async) status=(-?\d+)\s*$"
)
GIB_PAGES = 1 << 18


def parse_layout(value):
    try:
        layout = tuple(tuple(int(n) for n in server.split(":")) for server in value.split(","))
        if (len(layout) not in (1, 2) or
                any(len(server) != 2 or any(n <= 0 or n % 8 for n in server) or
                    sum(server) > 3200 for server in layout)):
            raise ValueError("invalid capacities")
        return layout
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "layout must be DRAM_GiB:PMem_GiB[,DRAM_GiB:PMem_GiB]; "
            "positive multiples of 8, at most 3200 GiB per Server") from error


def interleave(offset, a, b):
    common = min(a, b)
    if offset < 2 * common:
        return offset & 1, offset >> 1
    return (0 if a > b else 1), offset - common


def expected_location(layout, offset):
    pages = tuple(tuple(n * GIB_PAGES for n in server) for server in layout)
    totals = tuple(sum(server) for server in pages)
    if offset < 0 or offset >= sum(totals):
        raise ValueError("offset outside pool")
    server, local = (0, offset) if len(pages) == 1 else interleave(offset, *totals)
    tier, _ = interleave(local, *pages[server])
    return server, TIERS[tier]


def check_trace(lines, layout):
    """Streaming counts of I/O records, not counts of live or unique pages."""
    counts = Counter()
    errors = 0
    swap_type = None
    for number, line in enumerate(lines, 1):
        if "LOST" in line and "EVENT" in line:
            raise ValueError(f"line {number}: ftrace reports lost events")
        if "placement:" not in line:
            continue
        match = EVENT.search(line)
        if not match:
            raise ValueError(f"line {number}: malformed placement event")
        area, offset, server, tier, op, status = match.groups()
        area, offset, server, status = map(int, (area, offset, server, status))
        if swap_type is None:
            swap_type = area
        if area != swap_type:
            raise ValueError(f"line {number}: mixed swap areas; use one capture window")
        if (server, tier) != expected_location(layout, offset):
            raise ValueError(f"line {number}: offset={offset} routes to wrong Server/Tier")
        if status:
            errors += 1
        else:
            counts[(server, tier, op)] += 1
    if not counts and not errors:
        raise ValueError("no placement events found")
    return counts, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="saved text from ftrace trace_pipe/trace")
    parser.add_argument("--layout", required=True, type=parse_layout,
                        help="actual discovered GiB layout, e.g. 16:32,16:32")
    args = parser.parse_args()
    try:
        with args.trace.open() as stream:
            counts, errors = check_trace(stream, args.layout)
        print("server tier write_ok read_sync_ok read_async_ok")
        for server in range(len(args.layout)):
            for tier in TIERS:
                print(server, tier, *(counts[(server, tier, op)] for op in OPS))
        if errors:
            raise ValueError(f"{errors} failed WC events in capture")
        print(f"PASS: {sum(counts.values())} successful I/O records match static placement.")
        print("Counts describe the captured I/O window, not live page occupancy.")
        return 0
    except (OSError, ValueError) as error:
        print(f"rswap trace: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

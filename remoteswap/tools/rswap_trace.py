#!/usr/bin/env python3
"""Check static WC placement or dynamic Slot lifecycle records."""
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
MAPPING = re.compile(
    r"\bmapping:\s+seq=(\d+) action=(alloc|get|put|invalidate|free) "
    r"offset=(\d+) server=(\d+) tier=(dram|pmem) slot=(\d+) "
    r"users=(\d+) live=([01]) fallback=([0-3])\s*$"
)


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


def check_mapping_records(lines, pages, require_empty=False):
    """Replay from an empty pool; stream in mapping sequence order.

    A trace must start before the first allocation, use the global trace clock,
    and have no losses. A partial ending is allowed unless require_empty is set.
    This checks ownership/ref transitions, not page data or terminal WC success.
    """
    totals = tuple(sum(server) for server in pages)
    slots, owners, occupied = {}, {}, Counter()
    actions, fallbacks = Counter(), Counter()
    previous = None
    for number, line in enumerate(lines, 1):
        if "LOST" in line and "EVENT" in line:
            raise ValueError(f"line {number}: ftrace reports lost events")
        if "mapping:" not in line:
            continue
        match = MAPPING.search(line)
        if not match:
            raise ValueError(f"line {number}: malformed mapping event")
        seq, action, offset, server, tier, slot, users, live, fallback = match.groups()
        seq, offset, server, slot, users, live, fallback = map(
            int, (seq, offset, server, slot, users, live, fallback))
        if previous is not None and seq != previous + 1:
            raise ValueError(f"line {number}: mapping sequence gap/out of order; use global trace clock")
        previous = seq
        t = TIERS.index(tier)
        if (server >= len(pages) or offset >= sum(totals) or slot >= pages[server][t]
                or users >= 1 << 31):
            raise ValueError(f"line {number}: mapping outside pool or invalid reference count")
        key = (server, t, slot)
        pool = (server, t)
        if action == "alloc":
            if key in slots or offset in owners or (users, live) != (1, 1):
                raise ValueError(f"line {number}: duplicate Slot/offset allocation or invalid initial refs")
            s, local = (0, offset) if len(pages) == 1 else interleave(offset, *totals)
            target_t, _ = interleave(local, *pages[s])
            candidates = ((s, target_t), (s ^ 1, target_t),
                          (s, target_t ^ 1), (s ^ 1, target_t ^ 1))
            if pool != candidates[fallback]:
                raise ValueError(f"line {number}: wrong interleave/fallback target")
            for earlier_s, earlier_t in candidates[:fallback]:
                if earlier_s < len(pages) and occupied[earlier_s, earlier_t] < pages[earlier_s][earlier_t]:
                    raise ValueError(f"line {number}: fallback bypassed available capacity")
            slots[key] = [offset, users, live]
            owners[offset] = key
            occupied[pool] += 1
            fallbacks[fallback] += 1
        else:
            if key not in slots or slots[key][0] != offset:
                raise ValueError(f"line {number}: unknown Slot; capture before first Store")
            old_offset, old_users, old_live = slots[key]
            if action == "get":
                expected = (old_users + 1, old_live)
                if not old_live or owners.get(offset) != key:
                    raise ValueError(f"line {number}: lookup of invalidated mapping")
            elif action == "put":
                expected = (old_users - 1, old_live)
                if not old_users:
                    raise ValueError(f"line {number}: reference underflow")
            elif action == "invalidate":
                expected = (old_users, 0)
                if not old_live or owners.get(offset) != key:
                    raise ValueError(f"line {number}: repeated/stale invalidation")
                del owners[offset]
            else:  # free
                expected = (0, 0)
                if old_users or old_live:
                    raise ValueError(f"line {number}: Slot freed while still referenced")
            if (users, live) != expected:
                raise ValueError(f"line {number}: invalid mapping reference transition")
            if action == "free":
                del slots[key]
                occupied[pool] -= 1
            else:
                slots[key] = [old_offset, users, live]
        actions[action] += 1
    if not actions:
        raise ValueError("no mapping events found")
    if require_empty and slots:
        raise ValueError(f"{len(slots)} Slots still occupied at end of capture")
    return dict(events=sum(actions.values()), allocations=actions["alloc"],
                frees=actions["free"], live=len(owners), retired=len(slots) - len(owners),
                references=sum(state[1] for state in slots.values()), fallbacks=dict(fallbacks))


def check_dynamic_trace(lines, layout, require_empty=False):
    return check_mapping_records(lines,
                                 tuple(tuple(n * GIB_PAGES for n in server) for server in layout),
                                 require_empty)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="saved text from ftrace trace_pipe/trace")
    parser.add_argument("--layout", required=True, type=parse_layout,
                        help="actual discovered GiB layout, e.g. 16:32,16:32")
    parser.add_argument("--dynamic", action="store_true", help="replay mapping lifecycle from an empty pool")
    parser.add_argument("--require-empty", action="store_true", help="require every captured Slot to be freed")
    args = parser.parse_args()
    if args.require_empty and not args.dynamic:
        parser.error("--require-empty requires --dynamic")
    try:
        with args.trace.open() as stream:
            if args.dynamic:
                result = check_dynamic_trace(stream, args.layout, args.require_empty)
                print("PASS: dynamic Slot lifecycle", result)
                print("Requires capture from an empty pool and zero buffer losses; verify data and WC stats separately.")
                return 0
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

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


def check_mapping_records(lines, pages, require_empty=False, decisions=None):
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
            if decisions is not None:
                decision = decisions.pop(seq, None)
                if decision is None or (decision["offset"], decision["server"],
                        decision["tier"], decision["slot"], decision["fallback"]) != (
                        offset, server, t, slot, fallback):
                    raise ValueError(f"line {number}: missing/mismatched allocation decision")
                s, target_t = decision["preferred_server"], decision["preferred_tier"]
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
    if decisions:
        raise ValueError("decision has no corresponding mapping allocation")
    return dict(events=sum(actions.values()), allocations=actions["alloc"],
                frees=actions["free"], live=len(owners), retired=len(slots) - len(owners),
                references=sum(state[1] for state in slots.values()), fallbacks=dict(fallbacks))


def check_dynamic_trace(lines, layout, require_empty=False, decisions=None):
    return check_mapping_records(lines,
                                 tuple(tuple(n * GIB_PAGES for n in server) for server in layout),
                                 require_empty, decisions)


def read_decisions(lines, layout):
    """Short-window policy checks; successful records join allocator sequence IDs.

    This validates the kernel's recorded inputs/choice, not PFN identity or
    snapshot contents. Full publication/hotness analysis belongs to Patch 11.
    """
    result, tickets = {}, set()
    fields = {"alloc_seq", "offset", "pfn", "run", "generation", "decision_ns",
              "publish_ns", "state", "reason", "heat_ppm", "mean_ppm", "preferred_server",
              "preferred_tier", "server", "tier", "slot", "fallback", "rr", "alloc_ret"}
    for number, line in enumerate(lines, 1):
        if "LOST" in line and "EVENT" in line:
            raise ValueError("ftrace reports lost events")
        if "decision:" not in line:
            continue
        payload = line.split("decision:", 1)[1].split()
        if len(payload) != len(fields) or any(not re.fullmatch(r"\w+=-?\d+", x) for x in payload):
            raise ValueError(f"line {number}: malformed decision")
        d = {k: int(v) for k, v in (x.split("=", 1) for x in payload)}
        if (set(d) != fields or any(v < 0 for k, v in d.items()
                if k not in ("heat_ppm", "mean_ppm", "alloc_ret")) or
                d["heat_ppm"] not in range(-1, 1000001) or
                d["mean_ppm"] not in range(-1, 1000001) or d["alloc_ret"] > 0 or
                not d["pfn"] or not d["decision_ns"] or
                d["publish_ns"] > d["decision_ns"] or
                bool(d["generation"]) != bool(d["publish_ns"])):
            raise ValueError(f"line {number}: invalid decision fields")
        state, reason = d["state"], d["reason"]
        if state not in (0, 1, 2) or bool(d["run"]) != (state != 0):
            raise ValueError("invalid policy state/run")
        if reason in (5, 6):
            if (state != 2 or not d["generation"] or d["heat_ppm"] < 0 or
                    d["mean_ppm"] < 0 or (reason == 5) != (d["heat_ppm"] > d["mean_ppm"])):
                raise ValueError("hot/cold threshold mismatch")
            expected = (d["rr"] % len(layout), "dram" if reason == 5 else "pmem")
            ticket = (d["run"], expected[1], d["rr"])
            if ticket in tickets:
                raise ValueError("duplicate RR ticket")
            tickets.add(ticket)
        else:
            valid = ((state == 0 and reason == 0 and not d["generation"]) or
                     (state == 1 and reason == 1) or
                     (state == 2 and reason == 2 and not d["generation"]) or
                     (state == 2 and reason == 3 and d["generation"] and d["mean_ppm"] == -1) or
                     (state == 2 and reason == 4 and d["generation"] and d["mean_ppm"] >= 0))
            if not valid or d["heat_ppm"] != -1:
                raise ValueError("invalid interleave reason")
            expected = expected_location(layout, d["offset"])
        if (d["preferred_server"], d["preferred_tier"]) != (expected[0], TIERS.index(expected[1])):
            raise ValueError("wrong policy preference")
        expected_location(layout, d["offset"])
        if d["alloc_ret"]:
            if d["alloc_seq"]:
                raise ValueError("failed allocation has a mapping identity")
            continue
        if not d["alloc_seq"] or d["alloc_seq"] in result:
            raise ValueError("missing/duplicate decision identity")
        result[d["alloc_seq"]] = d
    if not result:
        raise ValueError("no successful allocation decisions")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="saved text from ftrace trace_pipe/trace")
    parser.add_argument("--layout", required=True, type=parse_layout,
                        help="actual discovered GiB layout, e.g. 16:32,16:32")
    parser.add_argument("--dynamic", action="store_true", help="replay mapping lifecycle from an empty pool")
    parser.add_argument("--adaptive", action="store_true", help="join decision events instead of assuming interleave")
    parser.add_argument("--require-empty", action="store_true", help="require every captured Slot to be freed")
    args = parser.parse_args()
    if args.require_empty and not args.dynamic:
        parser.error("--require-empty requires --dynamic")
    if args.adaptive and not args.dynamic:
        parser.error("--adaptive requires --dynamic")
    try:
        decisions = None
        if args.adaptive:
            with args.trace.open() as stream:
                decisions = read_decisions(stream, args.layout)
        with args.trace.open() as stream:
            if args.dynamic:
                result = check_dynamic_trace(stream, args.layout, args.require_empty, decisions)
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

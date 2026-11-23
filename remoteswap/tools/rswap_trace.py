#!/usr/bin/env python3
"""Check static WC placement or dynamic Slot lifecycle records."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

from rswap_stats import OPS, TIERS

EVENT = re.compile(
    r"\bplacement:\s+swap_type=(\d+) offset=(\d+) server=(\d+) "
    r"tier=(dram|pmem|[01]) op=(write|read_sync|read_async|[0-2]) status=(-?\d+)\s*$"
)
GIB_PAGES = 1 << 18
MAPPING = re.compile(
    r"\bmapping:\s+seq=(\d+) action=(alloc|get|put|invalidate|free|[0-4]) "
    r"offset=(\d+) server=(\d+) tier=(dram|pmem|[01]) slot=(\d+) "
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
        tier = TIERS[int(tier)] if tier.isdigit() else tier
        op = OPS[int(op)] if op.isdigit() else op
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
        action = ("alloc", "get", "put", "invalidate", "free")[int(action)] if action.isdigit() else action
        tier = TIERS[int(tier)] if tier.isdigit() else tier
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


def read_decisions(lines, layout, summary=None):
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
        extra = {'threshold_ppm', 'policy', 'threshold_source', 'threshold_generation', 'threshold_status'}
        modern = any(x.startswith('threshold_ppm=') for x in payload)
        expected_fields = fields | extra if modern else fields
        has_rule = any(x.startswith('placement_rule=') for x in payload)
        if has_rule:
            expected_fields = expected_fields | {'placement_rule'}
        if len(payload) != len(expected_fields) or any(not re.fullmatch(r"\w+=-?\d+", x) for x in payload):
            raise ValueError(f"line {number}: malformed decision")
        d = {k: int(v) for k, v in (x.split("=", 1) for x in payload)}
        if (set(d) != expected_fields or any(v < 0 for k, v in d.items()
                if k not in ("heat_ppm", "mean_ppm", "threshold_ppm", "alloc_ret")) or
                d["heat_ppm"] not in range(-1, 1000001) or
                d["mean_ppm"] not in range(-1, 1000001) or d["alloc_ret"] > 0 or
                not d["pfn"] or not d["decision_ns"] or
                d["publish_ns"] > d["decision_ns"] or
                bool(d["generation"]) != bool(d["publish_ns"])):
            raise ValueError(f"line {number}: invalid decision fields")
        rule = d.get('placement_rule', 0)
        if rule not in (0, 1) or (has_rule and not modern):
            raise ValueError('unsupported placement rule')
        if modern:
            if (d['threshold_ppm'] not in range(-1, 1000001) or d['policy'] not in (0, 1) or
                    d['threshold_source'] not in range(4) or d['threshold_status'] not in range(4) or
                    bool(d['threshold_generation']) != (d['threshold_ppm'] >= 0) or
                    d['threshold_generation'] > d['generation']):
                raise ValueError('invalid threshold metadata')
            if d['generation'] and d['threshold_source'] in (0, 3):
                if d['threshold_ppm'] != d['mean_ppm']:
                    raise ValueError('global threshold differs from mean')
            if d['generation']:
                source, status, origin = d['threshold_source'], d['threshold_status'], d['threshold_generation']
                expected_origin = d['generation'] if d['threshold_ppm'] >= 0 else 0
                if d['policy'] == 0:
                    valid_meta = source == 0 and status == 0 and origin == expected_origin
                elif source == 1:
                    valid_meta = status == 0 and d['threshold_ppm'] >= 0 and origin == d['generation']
                elif source == 2:
                    valid_meta = status != 0 and d['threshold_ppm'] >= 0 and 0 < origin < d['generation']
                else:
                    valid_meta = source == 3 and status != 0 and origin == expected_origin
                if not valid_meta:
                    raise ValueError('inconsistent threshold provenance')
        threshold = d['threshold_ppm'] if modern else d['mean_ppm']
        state, reason = d["state"], d["reason"]
        if state not in (0, 1, 2) or bool(d["run"]) != (state != 0):
            raise ValueError("invalid policy state/run")
        if reason in (5, 6):
            if (state != 2 or not d["generation"] or d["heat_ppm"] < 0 or
                    threshold < 0 or (modern and d["heat_ppm"] == 0) or
                    (reason == 5) != (d["heat_ppm"] >= threshold if modern else d["heat_ppm"] > threshold)):
                raise ValueError("hot/cold threshold mismatch")
            expected = (d["rr"] % len(layout), "dram" if reason == 5 else "pmem")
            uses_rr = True
        else:
            valid = ((state == 0 and reason == 0 and not d["generation"]) or
                     (state == 1 and reason == 1) or
                     (state == 2 and reason == 2 and not d["generation"]) or
                     (state == 2 and reason == 3 and d["generation"] and threshold == -1) or
                     (state == 2 and reason in ((4, 7) if modern else (4,)) and d["generation"] and threshold >= 0))
            if not valid or d["heat_ppm"] != (0 if modern and reason == 7 else -1):
                raise ValueError("invalid default/zero reason")
            uses_rr = rule == 1 and reason in (2, 3, 4)
            expected = (d["rr"] % len(layout), "dram") if uses_rr else expected_location(layout, d["offset"])
        if uses_rr:
            ticket = (d["run"], expected[1], d["rr"])
            if ticket in tickets:
                raise ValueError("duplicate RR ticket")
            tickets.add(ticket)
        elif rule == 1 and d['rr']:
            raise ValueError('interleave decision consumed an RR ticket')
        if (d["preferred_server"], d["preferred_tier"]) != (expected[0], TIERS.index(expected[1])):
            raise ValueError("wrong policy preference")
        expected_location(layout, d["offset"])
        if summary is not None:
            summary['decisions'] += 1
            summary['reason_' + str(reason)] += 1
            summary['allocation_error' if d['alloc_ret'] else 'allocation_ok'] += 1
            if not d['alloc_ret']:
                summary['fallback_' + str(d['fallback'])] += 1
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
    parser.add_argument("--heat-dir", type=Path, help="publication + per-page log directory for actual-generation validation")
    parser.add_argument("--detail", type=Path, help="new TSV file; requires --heat-dir")
    parser.add_argument("--store-results", action="store_true", help="require complete Store post/WC chains")
    parser.add_argument("--buffer-stats", type=Path, help="zero-loss per-CPU ftrace statistics")
    parser.add_argument("--json", action="store_true", help="machine-readable dynamic report")
    args = parser.parse_args()
    if args.require_empty and not args.dynamic:
        parser.error("--require-empty requires --dynamic")
    if args.adaptive and not args.dynamic:
        parser.error("--adaptive requires --dynamic")
    if (args.heat_dir or args.store_results) and not args.adaptive:
        parser.error("--heat-dir/--store-results require --dynamic --adaptive")
    if args.detail and not args.heat_dir:
        parser.error("--detail requires --heat-dir")
    if args.json and not args.dynamic:
        parser.error("--json requires --dynamic")
    try:
        report = {'buffer_loss_checked': False}
        if args.buffer_stats:
            from rswap_hotness_check import check_buffer_stats
            check_buffer_stats(args.buffer_stats)
            report['buffer_loss_checked'] = True
        decisions = None
        if args.adaptive:
            summary = Counter()
            with args.trace.open() as stream:
                decisions = read_decisions(stream, args.layout, summary)
            report['decisions'] = dict(summary)
            from rswap_analysis import check_heat, check_store_results, hotness_summary
            if args.heat_dir:
                report['heat'] = check_heat(decisions, args.heat_dir, args.detail)
                report['sampling'] = hotness_summary(args.heat_dir)
            if args.store_results:
                with args.trace.open() as stream:
                    report['store_results'] = check_store_results(stream, decisions)
        with args.trace.open() as stream:
            if args.dynamic:
                result = check_dynamic_trace(stream, args.layout, args.require_empty, decisions)
                report['mapping'] = result
                if args.json:
                    print(json.dumps(report, indent=2))
                else:
                    print("PASS: dynamic Slot lifecycle", result)
                    print(json.dumps(report, indent=2))
                    print("Requires capture from an empty pool and zero buffer losses; verify data separately.")
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
    except (OSError, ValueError, KeyError) as error:
        print(f"rswap trace: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

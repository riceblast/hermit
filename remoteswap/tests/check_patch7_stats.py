#!/usr/bin/env python3
"""Check two saved v2 snapshots for the dual-Server core workload acceptance."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from rswap_stats import OPS, TIERS, rates_between, read_snapshot


def check(before, after):
    _, _, unrouted = rates_between(before, after)
    if {server for server, _, _ in after.rows} != {0, 1}:
        raise ValueError("expected both Servers")
    if unrouted:
        raise ValueError(f"unrouted_error increased by {unrouted}")
    rows = []
    for server in (0, 1):
        for tier in TIERS:
            delta = {
                op: tuple(a - b for a, b in zip(after.rows[(server, tier, op)],
                                               before.rows[(server, tier, op)]))
                for op in OPS
            }
            writes = delta["write"][0]
            reads = delta["read_sync"][0] + delta["read_async"][0]
            wc = sum(delta[op][1] for op in OPS)
            submit = sum(delta[op][2] for op in OPS)
            rows.append((server, tier, writes, reads, wc, submit))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    try:
        rows = check(read_snapshot(args.before), read_snapshot(args.after))
        print("server tier write_ok_delta read_ok_delta wc_error_delta submit_error_delta")
        for row in rows:
            print(*row)
        if any(writes <= 0 or reads <= 0 or wc or submit
               for _, _, writes, reads, wc, submit in rows):
            raise ValueError("each Server/Tier needs successful writes and reads, with zero error deltas")
        print("PASS: all four Server/Tier pools have successful reads/writes; error deltas are zero.")
        return 0
    except (OSError, ValueError) as error:
        print(f"rswap acceptance: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

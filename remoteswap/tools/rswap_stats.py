#!/usr/bin/env python3
"""Read Client counters and report successful RDMA payload MiB/s."""
import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import sys
import time

OPS = ("write", "read_sync", "read_async")
DEFAULT_PATH = "/sys/kernel/debug/rswap/stats"


@dataclass(frozen=True)
class Snapshot:
    instance_ns: int
    sample_ns: int
    page_size: int
    rows: dict
    unrouted_error: int


class BaselineChanged(ValueError):
    """Counters cannot be compared across different module instances."""


def parse_snapshot(text):
    lines = text.strip().splitlines()
    try:
        fields = lines[0].split()
        header = dict(field.split("=", 1) for field in fields)
        if len(fields) != 4 or set(header) != {"version", "instance_ns", "sample_ns", "page_size"}:
            raise ValueError("unexpected metadata")
        header = {key: int(value) for key, value in header.items()}
        if header["version"] != 1 or min(header.values()) <= 0:
            raise ValueError("unsupported version or metadata")
        if lines[1] != "server op ok wc_error submit_error":
            raise ValueError("unexpected columns")
        rows = {}
        for line in lines[2:-1]:
            server, op, ok, wc_error, submit_error = line.split()
            key = (int(server), op)
            values = tuple(map(int, (ok, wc_error, submit_error)))
            if key[0] not in (0, 1) or op not in OPS or key in rows or min(values) < 0:
                raise ValueError("invalid or duplicate counter row")
            rows[key] = values
        key, value = lines[-1].split("=", 1)
        unrouted = int(value)
        servers = {server for server, _ in rows}
        if (key != "unrouted_error" or unrouted < 0 or
                servers not in ({0}, {0, 1}) or
                set(rows) != {(server, op) for server in servers for op in OPS}):
            raise ValueError("incomplete counters")
        return Snapshot(header["instance_ns"], header["sample_ns"],
                        header["page_size"], rows, unrouted)
    except (IndexError, ValueError) as error:
        raise ValueError("Invalid rswap stats snapshot: {}".format(error)) from error


def read_snapshot(path):
    # Close after each read: a monitor must not pin the module while sleeping.
    return parse_snapshot(Path(path).read_text())


def rates_between(before, after):
    if (before.instance_ns != after.instance_ns or before.page_size != after.page_size or
            before.rows.keys() != after.rows.keys()):
        raise BaselineChanged("module instance or counter layout changed")
    deltas = {}
    for key, values in after.rows.items():
        delta = tuple(current - old for current, old in zip(values, before.rows[key]))
        if min(delta) < 0:
            raise BaselineChanged("counters decreased")
        deltas[key] = delta
    unrouted = after.unrouted_error - before.unrouted_error
    if unrouted < 0:
        raise BaselineChanged("unrouted counter decreased")
    seconds = (after.sample_ns - before.sample_ns) / 1_000_000_000
    if seconds <= 0:
        raise ValueError("Stats sample time did not advance")
    result = {}
    for server in sorted({server for server, _ in deltas}):
        rates = [deltas[(server, op)][0] * after.page_size / seconds / (1024 * 1024)
                 for op in OPS]
        result[server] = {
            "write": rates[0], "read_sync": rates[1], "read_async": rates[2],
            "read_total": rates[1] + rates[2], "total": sum(rates),
            "wc_error": sum(deltas[(server, op)][1] for op in OPS),
            "submit_error": sum(deltas[(server, op)][2] for op in OPS),
        }
    return seconds, result, unrouted


def positive_interval(value):
    try:
        interval = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("interval must be a positive number") from error
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError("interval must be finite and greater than zero")
    return interval


def monitor(path, interval):
    before = read_snapshot(path)
    print("Baseline ready; sampling successful RDMA payload every {:g}s. Ctrl-C to stop."
          .format(interval), flush=True)
    while True:
        time.sleep(interval)
        after = read_snapshot(path)
        try:
            seconds, rates, unrouted = rates_between(before, after)
        except BaselineChanged as error:
            print("New baseline: {}".format(error), file=sys.stderr, flush=True)
            before = after
            continue
        print("interval={:.3f}s unrouted_error_delta={}".format(seconds, unrouted))
        print("server write_MiB/s sync_read_MiB/s async_read_MiB/s read_total_MiB/s "
              "total_MiB/s wc_error_delta submit_error_delta")
        for server, row in rates.items():
            print("{} {:.3f} {:.3f} {:.3f} {:.3f} {:.3f} {} {}".format(
                server, row["write"], row["read_sync"], row["read_async"],
                row["read_total"], row["total"], row["wc_error"], row["submit_error"]))
        sys.stdout.flush()
        before = after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=DEFAULT_PATH, help="Client stats file")
    parser.add_argument("--interval", type=positive_interval, default=1.0,
                        help="sample interval in seconds (default: 1)")
    args = parser.parse_args()
    try:
        monitor(args.path, args.interval)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, OverflowError) as error:
        print("rswap monitor: {} (RDMA module loaded and debugfs accessible?)"
              .format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Measure actual installed local diagnostic calls on fixed synthetic databases.

Use an installed environment's `python -I examples/benchmark_metrics.py`.
This source-only tool never opens a caller-selected inbox or sends HTTP requests.
"""

import argparse
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time

NOW = 1000.0
BODY = b'{"synthetic":true,"message":"fixed diagnostic measurement"}'
# state, ready time, lease expiry, attempts. Ten fixed rows repeat exactly.
PROFILE = (
    ("queued", 900, None, 0), ("queued", 1000, None, 0),
    ("queued", 1040, None, 1), ("queued", 1100, None, 1),
    ("leased", 950, 1010, 1), ("leased", 950, 1000, 1),
    ("leased", 950, 999, 3), ("succeeded", 950, None, 1),
    ("succeeded", 950, None, 1), ("dead_letter", 950, None, 3),
)
NOTICE = ("Single-host, warm-cache local library call durations on fixed synthetic data. "
          "No HTTP, ingestion, worker throughput, provider latency, production capacity or SLO is measured. "
          "The fixed logical snapshot time is unrelated to measured elapsed durations.")


def options(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000, 10000],
                        help="1–4 unique row counts, multiples of 10 from 10 through 200000")
    parser.add_argument("--samples", type=int, default=30, help="timed calls per operation, 5–200 (default 30)")
    parser.add_argument("--warmups", type=int, default=5, help="untimed calls per operation, 1–20 (default 5)")
    args = parser.parse_args(argv)
    if (not 1 <= len(args.sizes) <= 4 or len(set(args.sizes)) != len(args.sizes)
            or any(not 10 <= n <= 200000 or n % 10 for n in args.sizes)
            or not 5 <= args.samples <= 200 or not 1 <= args.warmups <= 20):
        raise ValueError("sizes require 1–4 unique multiples of 10 in 10–200000; samples 5–200; warmups 1–20")
    return args


def summarize(samples):
    if not samples or any(type(n) is not int or n < 0 for n in samples):
        raise ValueError("duration samples must be nonnegative integer nanoseconds")
    ordered = sorted(samples)
    return {"samples_ns": list(samples), "p50_ns": ordered[(len(ordered) + 1) // 2 - 1],
            "p95_ns": ordered[(95 * len(ordered) + 99) // 100 - 1], "max_ns": ordered[-1]}


def installed_runtime():
    import relaybox
    from relaybox import store
    distribution = importlib.metadata.distribution("relaybox-inbox")
    module = Path(store.__file__).resolve()
    installed = Path(distribution.locate_file("relaybox/store.py")).resolve()
    source = Path(__file__).resolve().parents[1] / "relaybox" / "store.py"
    if (module == source.resolve() or module != installed
            or not module.is_relative_to(Path(sys.prefix).resolve())):
        raise ValueError("use a trusted installed RelayBox interpreter with isolated mode")
    if relaybox.__version__ != distribution.version or not callable(getattr(store.Inbox, "metrics", None)):
        raise ValueError("installed runtime metadata and metrics contract must agree")
    info = time.get_clock_info("perf_counter")
    runtime = {"package_version": distribution.version, "python": platform.python_version(),
               "implementation": platform.python_implementation(), "sqlite": sqlite3.sqlite_version,
               "os": platform.system(), "os_release": platform.release(), "machine": platform.machine(),
               "logical_cpu_count": os.cpu_count(), "timer": "perf_counter_ns",
               "timer_resolution_seconds": info.resolution, "timer_monotonic": info.monotonic,
               "store_sha256": hashlib.sha256(module.read_bytes()).hexdigest()}
    return store.Inbox, runtime


def seed(inbox_type, path, rows):
    inbox = inbox_type(path, clock=lambda: NOW)
    digest = hashlib.sha256(BODY).hexdigest()
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.executemany("""INSERT INTO jobs
                (delivery_id,digest,body,state,attempts,max_attempts,base_delay,max_delay,
                 available_at,created_at,updated_at,lease_until,lease_token,worker,last_error)
                VALUES (?,?,?,?,?,3,2,60,?,800,900,?,?,?,?)""",
                ((f"synthetic-{i + 1:06}", digest, BODY, state, attempts, available, expiry,
                  "synthetic-only" if state == "leased" else None,
                  "synthetic-worker" if state == "leased" else None,
                  "synthetic failure" if state == "dead_letter" else None)
                 for i in range(rows) for state, available, expiry, attempts in [PROFILE[i % 10]]))
    return inbox


def database_digest(path):
    """Logical schema/all-column hash, outside the measured intervals."""
    result = hashlib.sha256()
    with closing(sqlite3.connect(path)) as connection:
        for line in connection.iterdump():
            result.update(line.encode("utf-8") + b"\n")
    return result.hexdigest()


def check(inbox, rows):
    unit = rows // 10
    expected = {"schema_version": 1, "snapshot_at": NOW,
                "counts": {"queued": 4 * unit, "leased": 3 * unit, "succeeded": 2 * unit, "dead_letter": unit},
                "queued_ready": 2 * unit, "queued_delayed": 2 * unit, "leases_active": unit,
                "leases_expired_reclaimable": unit, "leases_expired_exhausted": unit,
                "oldest_queued_ready_wait_seconds": 100.0, "next_retry_at": 1040.0, "next_lease_expiry": 1010.0}
    if inbox.metrics() != expected:
        raise ValueError("synthetic metrics disagree with the independent fixed-profile oracle")
    latest = inbox.list_jobs(limit=100)
    if [row["id"] for row in latest] != list(range(rows, max(0, rows - 100), -1)):
        raise ValueError("latest-list ordering/count differs from the fixed-profile oracle")
    for row in latest:
        state, available, expiry, attempts = PROFILE[(row["id"] - 1) % 10]
        if (row["state"], row["available_at"], row["lease_until"], row["attempts"]) != (state, available, expiry, attempts):
            raise ValueError("latest-list values differ from the fixed-profile oracle")


def measure(inbox, rows, *, samples, warmups, deadline):
    def remaining():
        if time.monotonic() >= deadline:
            raise ValueError("cooperative ten-minute measurement deadline exceeded")
    remaining()
    check(inbox, rows)
    before = database_digest(inbox.path)
    result = {"rows": rows, "latest_list_rows": min(rows, 100)}
    for name, operation in [("metrics", inbox.metrics), ("latest_100", lambda: inbox.list_jobs(limit=100))]:
        for _ in range(warmups):
            remaining()
            operation()
        timings = []
        for _ in range(samples):
            remaining()
            start = time.perf_counter_ns()
            operation()
            timings.append(time.perf_counter_ns() - start)
        result[name] = summarize(timings)
    check(inbox, rows)
    if database_digest(inbox.path) != before:
        raise ValueError("measurement unexpectedly changed persisted schema or rows")
    with closing(sqlite3.connect(inbox.path)) as connection:
        result["database_page_bytes"] = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
    remaining()
    result["logical_database_unchanged"] = True
    return result


def main(argv=None):
    try:
        args = options(argv)
        inbox_type, runtime = installed_runtime()
        deadline = time.monotonic() + 600
        measurements = []
        with tempfile.TemporaryDirectory(prefix="relaybox-metrics-measurement-") as directory:
            for rows in args.sizes:
                if time.monotonic() >= deadline:
                    raise ValueError("cooperative ten-minute measurement deadline exceeded")
                inbox = seed(inbox_type, Path(directory) / f"synthetic-{rows}.sqlite3", rows)
                measurements.append(measure(inbox, rows, samples=args.samples, warmups=args.warmups, deadline=deadline))
        receipt = {"schema_version": 1, "synthetic": True, "notice": NOTICE,
                   "runtime": runtime, "profile": "mixed-ten-v1", "logical_snapshot_at": NOW,
                   "profile_sha256": hashlib.sha256(json.dumps(PROFILE, separators=(",", ":")).encode()).hexdigest(),
                   "body_sha256": hashlib.sha256(BODY).hexdigest(),
                   "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "warmups_per_operation": args.warmups, "samples_per_operation": args.samples,
                   "concurrent_machine_activity_controlled": False,
                   "quantiles": "nearest-rank; no interpolation", "measurements": measurements}
        print(json.dumps(receipt, ensure_ascii=True, allow_nan=False, indent=2))
        return 0
    except (OSError, sqlite3.Error, ValueError, ImportError, importlib.metadata.PackageNotFoundError):
        # Fixed diagnostics do not expose a user's local paths or file contents.
        print("RelayBox measurement: invalid options, unavailable installed runtime, correctness or I/O failure.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

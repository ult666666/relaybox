"""Logical-clock and real-snapshot tests for read-only queue diagnostics."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from relaybox.store import Inbox, ValidationError


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "metrics.sqlite3"
        self.now = 1000.0
        self.inbox = Inbox(self.path, clock=lambda: self.now)

    def rows(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()

    def mixed(self):
        # Expected metrics are independently hand-derived at t=1000, not built
        # from production helpers. Policies differ per persisted job.
        specs = [
            ("ready-old", "queued", 900, None, 0, 3),
            ("ready-exact", "queued", 1000, None, 0, 2),
            ("retry-first", "queued", 1040, None, 1, 5),
            ("retry-later", "queued", 1100, None, 1, 7),
            ("active", "leased", 950, 1010, 1, 3),
            ("expired-exact", "leased", 950, 1000, 1, 5),
            ("expired-final", "leased", 950, 999, 2, 2),
            ("done", "succeeded", 950, None, 1, 3),
            ("dead", "dead_letter", 950, None, 3, 3),
        ]
        for delivery, state, available, expiry, attempts, budget in specs:
            job, _ = self.inbox.ingest(delivery, b'{"secret":"private-payload"}')
            with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
                conn.execute("""UPDATE jobs SET state=?,available_at=?,lease_until=?,attempts=?,max_attempts=?,
                    lease_token=?,worker=?,last_error=?,base_delay=?,max_delay=? WHERE id=?""",
                    (state, available, expiry, attempts, budget, "private-token", "private-worker", "private-error", budget, budget * 10, job["id"]))

    def test_empty_snapshot_has_explicit_null_times_and_zero_counts(self):
        self.assertEqual(self.inbox.metrics(), {
            "schema_version": 1, "snapshot_at": 1000.0,
            "counts": {"queued": 0, "leased": 0, "succeeded": 0, "dead_letter": 0},
            "queued_ready": 0, "queued_delayed": 0, "leases_active": 0,
            "leases_expired_reclaimable": 0, "leases_expired_exhausted": 0,
            "oldest_queued_ready_wait_seconds": None, "next_retry_at": None, "next_lease_expiry": None,
        })

    def test_mixed_snapshot_exact_boundaries_and_stored_policies(self):
        self.mixed()
        before = self.rows()
        self.assertEqual(self.inbox.metrics(), {
            "schema_version": 1, "snapshot_at": 1000.0,
            "counts": {"queued": 4, "leased": 3, "succeeded": 1, "dead_letter": 1},
            "queued_ready": 2, "queued_delayed": 2, "leases_active": 1,
            "leases_expired_reclaimable": 1, "leases_expired_exhausted": 1,
            "oldest_queued_ready_wait_seconds": 100.0, "next_retry_at": 1040.0, "next_lease_expiry": 1010.0,
        })
        self.assertEqual(self.rows(), before)
        self.now = 1100.0
        self.assertEqual(self.inbox.metrics(), {
            "schema_version": 1, "snapshot_at": 1100.0,
            "counts": {"queued": 4, "leased": 3, "succeeded": 1, "dead_letter": 1},
            "queued_ready": 4, "queued_delayed": 0, "leases_active": 0,
            "leases_expired_reclaimable": 2, "leases_expired_exhausted": 1,
            "oldest_queued_ready_wait_seconds": 200.0, "next_retry_at": None, "next_lease_expiry": None,
        })
        self.assertEqual(self.rows(), before)  # No expiry reconciliation or timestamps written.

    def test_wait_measures_readiness_not_creation_and_preserves_fractional_time(self):
        job, _ = self.inbox.ingest("recently-ready", b"{}")
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.execute("UPDATE jobs SET created_at=1,available_at=999.75 WHERE id=?", (job["id"],))
        self.now = 1000.25
        self.assertEqual(self.inbox.metrics()["oldest_queued_ready_wait_seconds"], 0.5)

    def test_clock_is_sampled_once_and_supported_range_is_inclusive(self):
        samples = iter([0, 253402300799, 1000.5])
        called = []
        def clock():
            sample = next(samples)
            called.append(sample)
            return sample
        self.inbox.clock = clock
        for expected in [0, 253402300799, 1000.5]:
            self.assertEqual(self.inbox.metrics()["snapshot_at"], expected)
        self.assertEqual(called, [0, 253402300799, 1000.5])

    def test_invalid_clock_values_are_controlled_and_do_not_mutate_rows(self):
        self.mixed()
        before = self.rows()
        for value in [True, False, None, "1000", -0.001, float("nan"), float("inf"), -float("inf"), 253402300800, 10 ** 1000]:
            self.inbox.clock = lambda: value
            with self.subTest(value=str(value)[:40]), self.assertRaises(ValidationError):
                self.inbox.metrics()
        self.assertEqual(self.rows(), before)

    def test_diagnostics_do_not_select_private_or_payload_columns(self):
        self.mixed()
        connect = sqlite3.connect
        private = {"body", "digest", "delivery_id", "lease_token", "worker", "last_error"}
        def guarded_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY
                                if action == sqlite3.SQLITE_READ and table == "jobs" and column in private
                                else sqlite3.SQLITE_OK)
            return conn
        with patch("relaybox.store.sqlite3.connect", guarded_connect):
            result = self.inbox.metrics()
        self.assertEqual(result["counts"]["queued"], 4)
        self.assertTrue(set(result).isdisjoint(private))

    def test_one_snapshot_survives_second_connection_commit_during_read(self):
        self.inbox.ingest("first", b"{}")
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.executemany("""INSERT INTO jobs
                (delivery_id,digest,body,state,max_attempts,base_delay,max_delay,available_at,created_at,updated_at)
                SELECT ?,digest,body,state,max_attempts,base_delay,max_delay,available_at,created_at,updated_at FROM jobs WHERE id=1""",
                [(f"copy-{i}",) for i in range(199)])
            conn.execute("UPDATE jobs SET available_at=900")
        reading, committed = threading.Event(), threading.Event()
        connect = sqlite3.connect
        def observed_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            aggregate_started = [False]
            def progress():
                if aggregate_started[0] and not reading.is_set():
                    reading.set()
                    if not committed.wait(5):
                        return 1
                return 0
            def trace(sql):
                if sql.lstrip().upper().startswith("SELECT") and "FROM JOBS" in sql.upper():
                    aggregate_started[0] = True
            conn.set_progress_handler(progress, 500)
            conn.set_trace_callback(trace)
            return conn
        with patch("relaybox.store.sqlite3.connect", observed_connect), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.inbox.metrics)
            try:
                self.assertTrue(reading.wait(5), "no actual aggregate read observed")
                with closing(connect(self.path, isolation_level=None)) as writer:
                    writer.execute("UPDATE jobs SET state='succeeded'")
            finally:
                committed.set()
            result = future.result(timeout=5)
        self.assertEqual(result["counts"], {"queued": 200, "leased": 0, "succeeded": 0, "dead_letter": 0})
        self.assertEqual(result["queued_ready"], 200)
        self.assertEqual(result["oldest_queued_ready_wait_seconds"], 100.0)
        latest = self.inbox.metrics()
        self.assertEqual(latest["counts"]["succeeded"], 200)
        self.assertEqual(latest["queued_ready"], 0)
        self.assertIsNone(latest["oldest_queued_ready_wait_seconds"])

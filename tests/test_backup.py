"""Real-file recovery contracts; failures catch copying live main bytes or unsafe publication."""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from relaybox.backup import backup_database, verify_backup
from relaybox.store import Conflict, Inbox, InvalidTransition, ValidationError


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "原始 inbox.sqlite3"
        self.destination = self.root / "safe backup.sqlite3"
        self.backup = backup_database
        self.verify = verify_backup

    def seed(self):
        inbox = Inbox(self.source, clock=lambda: 1000, max_attempts=2, base_delay=7, max_delay=19)
        for delivery in ["success", "dead", "leased", "queued"]:
            inbox.ingest(delivery, b'{"private":"payload-do-not-print"}')
        job = inbox.claim("first")
        inbox.ack(job["id"], job["lease_token"])
        for _ in range(2):
            job = inbox.claim("retry")
            inbox.fail(job["id"], job["lease_token"], "synthetic failure")
            # Make the retry ready without wall-clock synchronization.
            with closing(sqlite3.connect(self.source, isolation_level=None)) as conn:
                conn.execute("UPDATE jobs SET available_at=999 WHERE delivery_id='dead'")
        inbox.replay(job["id"])
        for _ in range(2):
            job = inbox.claim("retry")
            inbox.fail(job["id"], job["lease_token"], "synthetic failure")
            with closing(sqlite3.connect(self.source, isolation_level=None)) as conn:
                conn.execute("UPDATE jobs SET available_at=999 WHERE delivery_id='dead'")
        lease = inbox.claim("private-worker", lease_seconds=30)
        return inbox, lease

    @staticmethod
    def rows(path):
        with closing(sqlite3.connect(path)) as conn:
            return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()

    def test_snapshot_preserves_every_state_and_private_fields_without_receipt_leaks(self):
        inbox, lease = self.seed()
        before = self.source.read_bytes()
        result = self.backup(self.source, self.destination)
        self.assertEqual(result["counts"], {"queued": 1, "leased": 1, "succeeded": 1, "dead_letter": 1})
        self.assertEqual(result["jobs"], 4)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["source"], str(self.source))
        self.assertEqual(result["destination"], str(self.destination))
        self.assertEqual(result["sha256"], hashlib.sha256(self.destination.read_bytes()).hexdigest())
        self.assertEqual(result["size_bytes"], self.destination.stat().st_size)
        self.assertEqual(self.rows(self.source), self.rows(self.destination))
        self.assertEqual(self.source.read_bytes(), before)
        encoded = json.dumps(result)
        for secret in [lease["lease_token"], "payload-do-not-print", "private-worker", "synthetic failure"]:
            self.assertNotIn(secret, encoded)
        verified = self.verify(self.destination)
        self.assertEqual(verified["counts"], result["counts"])
        self.assertEqual(verified["sha256"], result["sha256"])
        self.assertFalse(Path(str(self.destination) + "-wal").exists())
        self.assertFalse(Path(str(self.destination) + "-shm").exists())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)

    def test_restored_copy_keeps_duplicate_policy_and_reclaims_expired_leases(self):
        inbox, lease = self.seed()
        self.backup(self.source, self.destination)
        restored_path = self.root / "restored.sqlite3"
        self.backup(self.destination, restored_path)
        restored = Inbox(restored_path, clock=lambda: 1030, max_attempts=99, base_delay=0)
        duplicate, created = restored.ingest("queued", b'{"private":"payload-do-not-print"}')
        self.assertFalse(created)
        self.assertEqual((duplicate["max_attempts"], duplicate["base_delay"], duplicate["max_delay"]), (2, 7, 19))
        with self.assertRaises(Conflict):
            restored.ingest("queued", b'{"different":true}')
        reclaimed = restored.claim("replacement")
        self.assertEqual(reclaimed["id"], lease["id"])
        self.assertEqual(reclaimed["attempts"], 2)
        self.assertNotEqual(reclaimed["lease_token"], lease["lease_token"])
        with self.assertRaises(InvalidTransition):
            restored.ack(lease["id"], lease["lease_token"])
        self.assertEqual(restored.get(2)["replay_count"], 1)
        self.assertEqual(inbox.get(lease["id"])["lease_token"], lease["lease_token"])

    def test_live_wal_includes_committed_and_excludes_uncommitted_writer_changes(self):
        inbox = Inbox(self.source)
        inbox.ingest("committed", b'{"value":1}')
        writer = sqlite3.connect(self.source, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE jobs SET worker='committed-in-wal' WHERE id=1")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE jobs SET worker='uncommitted' WHERE id=1")
        before = self.source.read_bytes()
        self.assertTrue(Path(str(self.source) + "-wal").stat().st_size > 0)
        result = self.backup(self.source, self.destination, timeout=1)
        self.assertEqual(result["jobs"], 1)
        with closing(sqlite3.connect(self.destination)) as snapshot:
            self.assertEqual(snapshot.execute("SELECT worker FROM jobs").fetchone()[0], "committed-in-wal")
        self.assertEqual(self.source.read_bytes(), before)
        writer.rollback()

    def test_missing_and_nonregular_sources_never_initialize_database(self):
        for source in [self.source, self.root]:
            with self.subTest(source=source), self.assertRaises(ValidationError):
                self.backup(source, self.destination)
            self.assertFalse(self.destination.exists())
        self.assertFalse(self.source.exists())
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.source)
            with self.assertRaises(ValidationError):
                self.backup(self.source, self.destination)

    def test_foreign_empty_truncated_corrupt_or_changed_schema_fail_without_publication(self):
        fixtures = [b"", b"not a database", b"SQLite format 3\x00" + b"\x00" * 84]
        for content in fixtures:
            self.source.write_bytes(content)
            with self.subTest(content=content), self.assertRaises((ValidationError, sqlite3.Error)):
                self.backup(self.source, self.destination)
            self.assertFalse(self.destination.exists())
            self.assertEqual(self.source.read_bytes(), content)
        self.source.unlink()
        with closing(sqlite3.connect(self.source, isolation_level=None)) as conn:
            conn.execute("CREATE TABLE foreign_data(value)")
            conn.execute("PRAGMA user_version=1")
        with self.assertRaises(ValidationError):
            self.backup(self.source, self.destination)
        self.source.unlink()
        Inbox(self.source)
        with closing(sqlite3.connect(self.source, isolation_level=None)) as conn:
            conn.execute("CREATE TRIGGER extra AFTER INSERT ON jobs BEGIN SELECT 1; END")
        with self.assertRaises(ValidationError):
            self.backup(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_similar_schema_with_changed_check_literals_is_unrecognized(self):
        Inbox(self.source)
        with closing(sqlite3.connect(self.source, isolation_level=None)) as conn:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("UPDATE sqlite_schema SET sql=replace(sql,?,?) WHERE type='table' AND name='jobs'", ("'queued'", "'QUEUED'"))
        with self.assertRaises(ValidationError):
            self.backup(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_verify_reads_only_and_rejects_invalid_backup(self):
        Inbox(self.source).ingest("x", b"{}")
        self.backup(self.source, self.destination)
        before = self.destination.read_bytes()
        self.destination.chmod(0o400)
        self.verify(self.destination)
        self.assertEqual(self.destination.read_bytes(), before)
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(ValidationError):
            self.verify(missing)
        self.assertFalse(missing.exists())
        self.destination.chmod(0o600)
        self.destination.write_bytes(before[:100])
        with self.assertRaises((ValidationError, sqlite3.Error)):
            self.verify(self.destination)

    def test_existing_self_hardlink_directory_and_symlink_destinations_are_never_replaced(self):
        Inbox(self.source).ingest("x", b"{}")
        self.destination.write_bytes(b"keep")
        directory = self.root / "directory"
        directory.mkdir()
        hardlink = self.root / "hardlink"
        os.link(self.source, hardlink)
        symlink = self.root / "symlink"
        symlink.symlink_to(self.root / "absent")
        before = self.source.read_bytes()
        for destination in [self.source, self.destination, directory, hardlink, symlink]:
            with self.subTest(destination=destination), self.assertRaises(ValidationError):
                self.backup(self.source, destination)
        self.assertEqual(self.destination.read_bytes(), b"keep")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(list(self.root.glob(".relaybox-backup-*")), [])

    def test_source_symlink_rejected_but_trusted_ancestor_alias_works(self):
        Inbox(self.source)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        result = self.backup(alias / self.source.name, alias / self.destination.name)
        self.assertEqual(result["destination"], str(self.destination))
        source_link = self.root / "source-link"
        source_link.symlink_to(self.source)
        with self.assertRaisesRegex(ValidationError, "symlink"):
            self.verify(source_link)

    def test_publication_failure_and_real_destination_race_clean_private_files(self):
        Inbox(self.source)
        with patch("relaybox.backup.os.link", side_effect=OSError("synthetic link failure")):
            with self.assertRaisesRegex(OSError, "synthetic"):
                self.backup(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".relaybox-backup-*")), [])
        original_link = os.link
        def occupied(source, destination):
            Path(destination).write_bytes(b"race winner")
            return original_link(source, destination)
        with patch("relaybox.backup.os.link", side_effect=occupied):
            with self.assertRaises(ValidationError):
                self.backup(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"race winner")
        self.assertEqual(list(self.root.glob(".relaybox-backup-*")), [])

    def test_timeout_is_finite_and_bounded_before_any_output(self):
        Inbox(self.source)
        for timeout in [None, True, "1", 0, 0.099, 60.1, float("nan"), float("inf"), 10 ** 1000]:
            with self.subTest(timeout=str(timeout)[:50]), self.assertRaises(ValidationError):
                self.backup(self.source, self.destination, timeout=timeout)
            self.assertFalse(self.destination.exists())
        self.assertEqual(self.backup(self.source, self.destination, timeout=0.1)["jobs"], 0)

    @contextmanager
    def observe_backup(self, callback):
        # Real SQLite connections/pages; intercept only the documented progress
        # callback to make timing and a concurrent commit deterministic.
        connect = sqlite3.connect
        class ObservedConnection(sqlite3.Connection):
            def backup(self, target, **kwargs):
                progress = kwargs["progress"]
                def observe(status, remaining, total):
                    callback(status, remaining, total)
                    progress(status, remaining, total)
                return super().backup(target, **{**kwargs, "progress": observe})
        def observed_connect(*args, **kwargs):
            return connect(*args, **{**kwargs, "factory": ObservedConnection})
        with patch("relaybox.backup.sqlite3.connect", observed_connect):
            yield

    def test_progress_callback_pins_snapshot_while_separate_writer_commits(self):
        inbox = Inbox(self.source)
        for i in range(80):
            inbox.ingest(f"before-{i}", b'{"data":"' + b"x" * 60_000 + b'"}')
        writer = sqlite3.connect(self.source, isolation_level=None)
        self.addCleanup(writer.close)
        committed = threading.Event()
        def commit_during_backup(status, remaining, total):
            if not committed.is_set():
                self.assertGreater(remaining, 0)
                writer.execute("""INSERT INTO jobs
                    (delivery_id,digest,body,state,max_attempts,base_delay,max_delay,available_at,created_at,updated_at)
                    SELECT 'during-backup',digest,body,state,max_attempts,base_delay,max_delay,available_at,created_at,updated_at
                    FROM jobs LIMIT 1""")
                committed.set()
        with self.observe_backup(commit_during_backup):
            result = self.backup(self.source, self.destination)
        self.assertTrue(committed.is_set())
        self.assertEqual(result["jobs"], 80)
        self.assertEqual(inbox.stats()["queued"], 81)
        self.assertEqual(len(self.rows(self.destination)), 80)

    def test_backup_progress_deadline_cancels_real_copy_and_cleans_temporary_files(self):
        from relaybox.backup import BackupTimeout
        inbox = Inbox(self.source)
        for i in range(80):
            inbox.ingest(f"cancel-{i}", b'{"data":"' + b"x" * 60_000 + b'"}')
        iterations = []
        now = [0.0]
        observed = threading.Event()
        def expire(status, remaining, total):
            iterations.append(remaining)
            observed.set()
            now[0] = 1.0
        with self.observe_backup(expire), patch("relaybox.backup.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(BackupTimeout):
                self.backup(self.source, self.destination, timeout=0.1)
        self.assertTrue(observed.is_set())
        self.assertEqual(len(iterations), 1)
        self.assertGreater(iterations[0], 0)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".relaybox-backup-*")), [])

    def test_integrity_failure_and_size_limit_are_explicit(self):
        Inbox(self.source).ingest("bad-state", b"{}")
        with closing(sqlite3.connect(self.source, isolation_level=None)) as conn:
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute("UPDATE jobs SET state='corrupt-state'")
        with self.assertRaisesRegex(ValidationError, "integrity|unrecognized"):
            self.backup(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        # Sparse regular file exercises the 1 GiB bound without allocating it.
        with self.source.open("wb") as large:
            large.truncate(1_073_741_825)
        with self.assertRaisesRegex(ValidationError, "1 GiB"):
            self.backup(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_corrupt_btree_fails_without_modifying_source_or_publishing(self):
        Inbox(self.source).ingest("corrupt", b"{}")
        with closing(sqlite3.connect(self.source)) as conn:
            rootpage = conn.execute("SELECT rootpage FROM sqlite_schema WHERE name='jobs'").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        damaged = bytearray(self.source.read_bytes())
        damaged[(rootpage - 1) * page_size] = 255  # Invalid b-tree page kind.
        self.source.write_bytes(damaged)
        with self.assertRaises((ValidationError, sqlite3.DatabaseError)):
            self.backup(self.source, self.destination)
        self.assertEqual(self.source.read_bytes(), damaged)
        self.assertFalse(self.destination.exists())

    def test_verify_requires_standalone_snapshot_and_validates_its_timeout(self):
        Inbox(self.source)
        with self.assertRaisesRegex(ValidationError, "standalone"):
            self.verify(self.source)
        self.backup(self.source, self.destination)
        for timeout in [True, 0, float("inf"), 61]:
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                self.verify(self.destination, timeout=timeout)

    def test_destination_sidecars_are_preserved_and_prevent_publication(self):
        Inbox(self.source)
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.destination) + suffix)
                sidecar.write_bytes(b"retained sidecar")
                try:
                    with self.assertRaisesRegex(ValidationError, "sidecar"):
                        self.backup(self.source, self.destination)
                    self.assertFalse(self.destination.exists())
                    self.assertEqual(sidecar.read_bytes(), b"retained sidecar")
                finally:
                    sidecar.unlink(missing_ok=True)
                    self.destination.unlink(missing_ok=True)
        self.assertEqual(list(self.root.glob(".relaybox-backup-*")), [])

    def test_verify_rejects_sidecars_without_changing_snapshot_or_sidecar(self):
        Inbox(self.source)
        self.backup(self.source, self.destination)
        before = self.destination.read_bytes()
        sidecar = Path(str(self.destination) + "-journal")
        sidecar.write_bytes(b"retained journal")
        with self.assertRaisesRegex(ValidationError, "sidecar"):
            self.verify(self.destination)
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(sidecar.read_bytes(), b"retained journal")

    def test_real_exclusive_lock_contention_obeys_timeout_and_leaves_no_output(self):
        from relaybox.backup import BackupTimeout
        Inbox(self.source)
        blocker = sqlite3.connect(self.source, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("PRAGMA journal_mode=DELETE")
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with self.assertRaises(BackupTimeout):
            self.backup(self.source, self.destination, timeout=0.1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".relaybox-backup-*")), [])
        blocker.rollback()


if __name__ == "__main__":
    unittest.main()

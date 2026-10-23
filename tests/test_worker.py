import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from relaybox.store import Inbox, ValidationError


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.now = 1000.0
        self.inbox = Inbox(self.root / "inbox.sqlite3", clock=lambda: self.now)

    def worker(self, handler, **kwargs):
        from relaybox.worker import run_worker
        return run_worker(self.inbox, handler, **kwargs)

    def test_success_is_acknowledged_and_bounded(self):
        for delivery in ["first", "second", "third"]:
            self.inbox.ingest(delivery, b'{"synthetic":true}')
        received = []
        summary = self.worker(received.append, worker="test-worker", max_jobs=2, lease_seconds=10)
        self.assertEqual(summary, {"claimed": 2, "succeeded": 2, "failed": 0, "lost_leases": 0})
        self.assertEqual([job["delivery_id"] for job in received], ["first", "second"])
        self.assertEqual(received[0]["worker"], "test-worker")
        self.assertEqual(received[0]["lease_until"], 1010)
        self.assertEqual(self.inbox.stats(), {"queued": 1, "leased": 0, "succeeded": 2, "dead_letter": 0})

    def test_default_bound_claims_one_job(self):
        self.inbox.ingest("first", b'{}')
        self.inbox.ingest("second", b'{}')
        self.assertEqual(self.worker(lambda job: None)["claimed"], 1)
        self.assertEqual(self.inbox.stats()["queued"], 1)

    def test_handler_dictionary_edits_cannot_redirect_acknowledgment_or_failure(self):
        for handler_fails in [False, True]:
            with self.subTest(handler_fails=handler_fails):
                job, _ = self.inbox.ingest(str(handler_fails), b'{}')
                def handler(claim):
                    claim.clear()
                    claim.update({"id": 999, "lease_token": "unrelated"})
                    if handler_fails:
                        raise ValueError("synthetic failure after editing job")
                summary = self.worker(handler)
                self.assertEqual(summary["failed" if handler_fails else "succeeded"], 1)
                self.assertEqual(self.inbox.get(job["id"])["state"], "queued" if handler_fails else "succeeded")

    def test_exception_records_retry_and_stops_without_waiting(self):
        job, _ = self.inbox.ingest("retry", b'{}')
        def fail(_):
            raise RuntimeError("synthetic failure")
        result = self.worker(fail, max_jobs=1000)
        self.assertEqual(result, {"claimed": 1, "succeeded": 0, "failed": 1, "lost_leases": 0})
        state = self.inbox.get(job["id"])
        self.assertEqual(state["state"], "queued")
        self.assertEqual(state["available_at"], 1002)
        self.assertIn("synthetic failure", state["last_error"])
        self.assertEqual(self.now, 1000)

    def test_failures_reach_dead_letter_at_stored_attempt_limit(self):
        self.inbox = Inbox(self.root / "inbox.sqlite3", clock=lambda: self.now, base_delay=0, max_attempts=2)
        job, _ = self.inbox.ingest("dead", b'{}')
        def fail(_):
            raise ValueError("synthetic failure")
        self.assertEqual(self.worker(fail, max_jobs=10),
                         {"claimed": 2, "succeeded": 0, "failed": 2, "lost_leases": 0})
        self.assertEqual(self.inbox.get(job["id"])["state"], "dead_letter")

    def test_empty_inbox_returns_zero_counts(self):
        self.assertEqual(self.worker(lambda job: self.fail("unexpected job"), max_jobs=1000),
                         {"claimed": 0, "succeeded": 0, "failed": 0, "lost_leases": 0})

    def test_configuration_is_validated_before_claiming(self):
        job, _ = self.inbox.ingest("unclaimed", b'{}')
        bad = [{"max_jobs": value} for value in [0, 1001, True, 1.5, "1"]]
        bad += [{"worker": value} for value in ["", " ", "x" * 129, "\ud800", None]]
        bad += [{"lease_seconds": value} for value in [0, True, 3601, float("nan"), "30"]]
        for options in bad:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.worker(lambda job: None, **options)
        with self.assertRaises(ValidationError):
            self.worker(None)
        self.assertEqual(self.inbox.get(job["id"])["attempts"], 0)

    def test_lost_acknowledgement_and_failed_lease_are_counted(self):
        for handler_fails in [False, True]:
            with self.subTest(handler_fails=handler_fails):
                job, _ = self.inbox.ingest(str(handler_fails), b'{}')
                current = []
                def handler(claim):
                    self.now += 30
                    current.append(self.inbox.claim("replacement"))
                    if handler_fails:
                        raise RuntimeError("late synthetic failure")
                self.assertEqual(self.worker(handler),
                                 {"claimed": 1, "succeeded": 0, "failed": 0, "lost_leases": 1})
                self.assertEqual(self.inbox.get(job["id"])["lease_token"], current[0]["lease_token"])
                self.inbox.ack(job["id"], current[0]["lease_token"])

    def test_storage_errors_from_completion_are_not_hidden(self):
        for handler_fails in [False, True]:
            with self.subTest(handler_fails=handler_fails):
                self.inbox.ingest(str(handler_fails), b'{}')
                def handler(job):
                    # A real SQLite failure occurs when ack/fail next opens storage.
                    self.inbox.path = str(self.root / "missing" / "inbox.sqlite3")
                    if handler_fails:
                        raise ValueError("synthetic failure")
                with self.assertRaises(sqlite3.OperationalError):
                    self.worker(handler)
                self.inbox.path = str(self.root / "inbox.sqlite3")

    def test_interrupt_leaves_lease_for_recovery(self):
        for exception in [KeyboardInterrupt, SystemExit]:
            with self.subTest(exception=exception):
                job, _ = self.inbox.ingest(exception.__name__, b'{}')
                def interrupt(_):
                    raise exception()
                with self.assertRaises(exception):
                    self.worker(interrupt)
                self.assertEqual(self.inbox.get(job["id"])["state"], "leased")
                self.now += 30
                replacement = self.inbox.claim("recovery")
                self.assertEqual(replacement["id"], job["id"])
                self.assertEqual(replacement["attempts"], 2)
                self.inbox.ack(job["id"], replacement["lease_token"])

    def test_handler_can_explicitly_renew_a_long_lease(self):
        self.inbox.ingest("long", b'{}')
        def handler(job):
            self.now += 20
            self.inbox.renew(job["id"], job["lease_token"], lease_seconds=60)
            self.now += 40
        self.assertEqual(self.worker(handler)["succeeded"], 1)

    def test_exception_diagnostics_are_bounded_valid_text(self):
        for index, message in enumerate(["", "x" * 600, "\ud800"]):
            job, _ = self.inbox.ingest(str(index), b'{}')
            def fail(_):
                raise ValueError(message)
            self.assertEqual(self.worker(fail)["failed"], 1)
            error = self.inbox.get(job["id"])["last_error"]
            self.assertTrue(error.strip())
            self.assertLessEqual(len(error), 500)
            error.encode("utf-8")


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.now = 1000.0
        self.inbox = Inbox(self.root / "inbox.sqlite3", clock=lambda: self.now)
        self.receipts = self.root / "receipts"

    def handler(self, path=None):
        from relaybox.worker import ReceiptHandler
        return ReceiptHandler(self.receipts if path is None else path)

    def claim(self, delivery="synthetic-001", body=b'{"synthetic":true,"message":"hello"}'):
        self.inbox.ingest(delivery, body)
        return self.inbox.claim("receipt-worker")

    def test_receipt_has_minimal_deterministic_content_and_private_permissions(self):
        job = self.claim()
        self.handler()(job)
        files = list(self.receipts.iterdir())
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^[0-9a-f]{64}\.json$")
        self.assertEqual(json.loads(files[0].read_bytes()), {
            "version": 1, "delivery_id": "synthetic-001", "digest": job["digest"],
            "payload": {"synthetic": True, "message": "hello"}})
        self.assertNotIn(job["lease_token"].encode(), files[0].read_bytes())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.receipts.stat().st_mode), 0o700)

    def test_identical_receipt_after_expiry_and_reclaim_succeeds_without_rewrite(self):
        from relaybox.worker import run_worker
        job = self.claim()
        handler = self.handler()
        handler(job)
        receipt = next(self.receipts.iterdir())
        before = receipt.stat()
        self.now = 1030
        self.assertEqual(run_worker(self.inbox, handler)["succeeded"], 1)
        self.assertEqual(self.inbox.get(job["id"])["attempts"], 2)
        self.assertEqual(receipt.stat().st_ino, before.st_ino)
        self.assertEqual(receipt.stat().st_mtime_ns, before.st_mtime_ns)

    def test_delivery_identity_works_across_databases_and_distinguishes_body_bytes(self):
        job = self.claim()
        handler = self.handler()
        handler(job)
        other = Inbox(self.root / "other.sqlite3")
        other.ingest(job["delivery_id"], b'{"synthetic":true,"message":"hello"}')
        handler(other.claim("other"))
        self.assertEqual(len(list(self.receipts.iterdir())), 1)
        third = Inbox(self.root / "third.sqlite3")
        third.ingest(job["delivery_id"], b'{"synthetic": true,"message":"hello"}')
        handler(third.claim("third"))
        self.assertEqual(len(list(self.receipts.iterdir())), 2)

    def test_hostile_legal_delivery_id_never_becomes_a_path(self):
        job = self.claim("..:..")
        self.handler()(job)
        receipt = next(self.receipts.iterdir())
        self.assertEqual(receipt.parent, self.receipts)
        self.assertRegex(receipt.name, r"^[0-9a-f]{64}\.json$")
        self.assertEqual(json.loads(receipt.read_text())["delivery_id"], "..:..")

    def test_different_delivery_ids_with_same_body_do_not_collide_across_databases(self):
        handler = self.handler()
        handler(self.claim("first"))
        other = Inbox(self.root / "other.sqlite3")
        other.ingest("second", b'{"synthetic":true,"message":"hello"}')
        handler(other.claim("other"))
        files = list(self.receipts.iterdir())
        self.assertEqual(len(files), 2)
        self.assertEqual({json.loads(path.read_bytes())["delivery_id"] for path in files}, {"first", "second"})

    def test_conflicting_corrupt_or_noncanonical_receipt_is_never_overwritten(self):
        job = self.claim()
        handler = self.handler()
        handler(job)
        receipt = next(self.receipts.iterdir())
        original = receipt.read_bytes()
        for content in [b"not json", b'{"unexpected":true}', original + b" "]:
            with self.subTest(content=content):
                receipt.write_bytes(content)
                with self.assertRaises(ValueError):
                    handler(job)
                self.assertEqual(receipt.read_bytes(), content)

    def test_symlink_receipt_and_directory_are_rejected(self):
        job = self.claim()
        handler = self.handler()
        handler(job)
        receipt = next(self.receipts.iterdir())
        outside = self.root / "outside.json"
        outside.write_bytes(receipt.read_bytes())
        receipt.unlink()
        receipt.symlink_to(outside)
        with self.assertRaises(ValueError):
            handler(job)
        self.assertTrue(receipt.is_symlink())
        receipt.unlink()
        receipt.mkdir()
        with self.assertRaises(ValueError):
            handler(job)
        receipt.rmdir()
        self.receipts.rmdir()
        target = self.root / "target"
        target.mkdir()
        self.receipts.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.handler()
        with self.assertRaises(ValueError):
            handler(job)
        self.assertEqual(list(target.iterdir()), [])

    def test_trusted_ancestor_alias_is_canonicalized_without_following_selected_directory(self):
        job = self.claim()
        alias = self.root / "trusted-parent-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        handler = self.handler(alias / "receipts")
        handler(job)
        self.assertEqual(len(list(self.receipts.iterdir())), 1)
        self.assertEqual(json.loads(next(self.receipts.iterdir()).read_bytes())["delivery_id"], "synthetic-001")
        # The ordinary tempfile path also works on macOS, where /var aliases /private/var.
        self.handler(Path(self.tmp.name) / "ordinary-temp-receipts")(job)
        self.assertEqual(len(list((self.root / "ordinary-temp-receipts").iterdir())), 1)

    def test_publication_collision_without_existing_receipt_is_a_failure(self):
        job = self.claim()
        handler = self.handler()
        with patch("relaybox.worker.os.link", side_effect=FileExistsError("synthetic disappearing receipt")):
            with self.assertRaises(OSError):
                handler(job)
        self.assertEqual(list(self.receipts.iterdir()), [])

    def test_regular_file_as_receipt_directory_is_rejected(self):
        self.receipts.write_bytes(b"preserve me")
        with self.assertRaises((ValueError, OSError)):
            self.handler()
        self.assertEqual(self.receipts.read_bytes(), b"preserve me")

    def test_concurrent_identical_publications_produce_one_complete_receipt(self):
        job = self.claim()
        handler = self.handler()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(handler, [job] * 16))
        files = list(self.receipts.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_bytes())["delivery_id"], "synthetic-001")

    def test_failed_atomic_publication_leaves_no_final_or_temporary_files(self):
        job = self.claim()
        handler = self.handler()
        # The real file is written; only the final atomic publication is denied.
        with patch("relaybox.worker.os.link", side_effect=OSError("synthetic link failure")):
            with self.assertRaises(OSError):
                handler(job)
        self.assertEqual(list(self.receipts.iterdir()), [])

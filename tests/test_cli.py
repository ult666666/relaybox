import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "inbox.sqlite3"

    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "relaybox", "--db", str(self.db), *args], capture_output=True, text=True, timeout=10, env=env)

    def test_help_and_init(self):
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0)
        result = self.run_cli("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["queued"], 0)
        self.assertTrue(self.db.is_file())

    def test_demo_populates_four_states_without_credentials(self):
        result = self.run_cli("demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"], {"queued": 1, "leased": 1, "succeeded": 1, "dead_letter": 1})
        duplicate = self.run_cli("demo")
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("empty", duplicate.stderr)
        stats = self.run_cli("stats")
        self.assertEqual(json.loads(stats.stdout)["counts"]["succeeded"], 1)

    def test_claim_ack_and_replay_commands(self):
        self.assertEqual(self.run_cli("demo").returncode, 0)
        result = self.run_cli("claim", "--worker", "cli-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads(result.stdout)["job"]
        self.assertEqual(job["delivery_id"], "demo-004")
        ack = self.run_cli("ack", str(job["id"]), "--token", job["lease_token"])
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertEqual(self.run_cli("ack", str(job["id"]), "--token", job["lease_token"]).returncode, 2)
        replay = self.run_cli("replay", "3")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout)["job"]["replay_count"], 1)

    def test_fail_command_enforces_lease(self):
        self.run_cli("demo")
        job = json.loads(self.run_cli("claim", "--worker", "cli-test").stdout)["job"]
        failed = self.run_cli("fail", str(job["id"]), "--token", job["lease_token"], "--error", "synthetic failure")
        self.assertEqual(failed.returncode, 0, failed.stderr)
        fetched = json.loads(self.run_cli("show", str(job["id"])).stdout)["job"]
        self.assertEqual(fetched["state"], "queued")
        self.assertEqual(fetched["last_error"], "synthetic failure")

    def test_sign_command_covers_supplied_file_bytes(self):
        payload = Path(self.tmp.name) / "payload.json"
        payload.write_bytes(b'{"hello":"world"}')
        result = self.run_cli("sign", "evt-test", str(payload), "--timestamp", "1000", "--demo-secret")
        self.assertEqual(result.returncode, 0, result.stderr)
        from relaybox.auth import verify
        from relaybox.cli import DEMO_SECRET
        headers = json.loads(result.stdout)
        verify(DEMO_SECRET, headers["X-RelayBox-Timestamp"], headers["X-RelayBox-Signature"], "evt-test", payload.read_bytes(), now=1000)

    def test_serve_requires_secret_and_rejects_bad_port(self):
        clean = {key: value for key, value in os.environ.items() if key != "RELAYBOX_SECRET"}
        result = self.run_cli("serve", env=clean)
        self.assertEqual(result.returncode, 2)
        self.assertIn("RELAYBOX_SECRET", result.stderr)
        self.assertEqual(self.run_cli("serve", "--port", "99999", "--demo-secret").returncode, 2)

    def test_unknown_job_and_invalid_worker_are_cli_errors(self):
        self.assertEqual(self.run_cli("show", "999").returncode, 2)
        self.assertEqual(self.run_cli("claim", "--worker", "").returncode, 2)

    def test_out_of_range_job_ids_do_not_print_tracebacks(self):
        for job_id in ["0", "-1", str(2 ** 100)]:
            result = self.run_cli("show", job_id)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("error", json.loads(result.stderr))

    def test_renew_command_extends_lease_and_omits_token(self):
        self.run_cli("demo")
        job = json.loads(self.run_cli("claim", "--worker", "renew-test").stdout)["job"]
        result = self.run_cli("renew", str(job["id"]), "--token", job["lease_token"], "--lease-seconds", "60")
        self.assertEqual(result.returncode, 0, result.stderr)
        renewed = json.loads(result.stdout)["job"]
        self.assertGreater(renewed["lease_until"], job["lease_until"])
        self.assertNotIn("lease_token", renewed)
        self.assertNotIn(job["lease_token"], result.stdout)
        for args in [("--token", "wrong"), ("--token", job["lease_token"], "--lease-seconds", "0")]:
            self.assertEqual(self.run_cli("renew", str(job["id"]), *args).returncode, 2)
        self.assertEqual(self.run_cli("renew", "999", "--token", "wrong").returncode, 2)

    def test_work_command_processes_receipt_and_returns_summary_without_token(self):
        from relaybox.store import Inbox
        inbox = Inbox(self.db)
        job, _ = inbox.ingest("cli-receipt", b'{"synthetic":true}')
        receipts = Path(self.tmp.name).resolve() / "receipts"
        result = self.run_cli("work", "--receipts", str(receipts), "--worker", "cli-worker", "--max-jobs", "10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"claimed": 1, "succeeded": 1, "failed": 0, "lost_leases": 0})
        self.assertEqual(inbox.get(job["id"])["state"], "succeeded")
        self.assertNotIn("lease_token", result.stdout)
        self.assertEqual(len(list(receipts.iterdir())), 1)
        empty = self.run_cli("work", "--receipts", str(receipts))
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(json.loads(empty.stdout)["claimed"], 0)

    def test_work_receipt_conflict_exits_one_and_preserves_existing_file(self):
        from relaybox.store import Inbox
        from relaybox.worker import ReceiptHandler
        inbox = Inbox(self.db, clock=lambda: 1000)
        job, _ = inbox.ingest("cli-conflict", b'{"synthetic":true}')
        receipts = Path(self.tmp.name).resolve() / "receipts"
        ReceiptHandler(receipts)(inbox.claim("expired-worker"))
        receipt = next(receipts.iterdir())
        receipt.write_bytes(b"unexpected existing file")
        result = self.run_cli("work", "--receipts", str(receipts))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"claimed": 1, "succeeded": 0, "failed": 1, "lost_leases": 0})
        self.assertEqual(receipt.read_bytes(), b"unexpected existing file")
        self.assertEqual(inbox.get(job["id"])["state"], "queued")

    def test_work_lost_lease_exits_one_from_real_cli_with_deterministic_clock(self):
        from relaybox.store import Inbox
        inbox = Inbox(self.db, clock=lambda: 1000)
        job, _ = inbox.ingest("cli-lost-lease", b'{"synthetic":true}')
        receipts = Path(self.tmp.name) / "receipts"
        # Inject only clock samples, retaining the real CLI, files and SQLite
        # transitions. No scheduler delays or timing-dependent short leases.
        script = """import sys
from unittest.mock import patch
from relaybox.store import Inbox
from relaybox.cli import main
clock = iter([1000, 1030]).__next__
with patch('relaybox.cli.Inbox', lambda path: Inbox(path, clock=clock)):
    code = main()
sys.exit(code)
"""
        result = subprocess.run([sys.executable, "-c", script, "--db", str(self.db),
                                 "work", "--receipts", str(receipts)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"claimed": 1, "succeeded": 0, "failed": 0, "lost_leases": 1})
        self.assertEqual(inbox.get(job["id"])["state"], "leased")
        self.assertEqual(len(list(receipts.iterdir())), 1)
        self.assertNotIn(inbox.get(job["id"])["lease_token"], result.stdout)

    def test_work_invalid_configuration_exits_two_without_claiming(self):
        from relaybox.store import Inbox
        inbox = Inbox(self.db)
        job, _ = inbox.ingest("invalid-config", b'{}')
        for args in [("--max-jobs", "0"), ("--max-jobs", "1001"), ("--lease-seconds", "nan"), ("--worker", "")]:
            with self.subTest(args=args):
                result = self.run_cli("work", "--receipts", str(Path(self.tmp.name).resolve() / "receipts"), *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(inbox.get(job["id"])["attempts"], 0)


class RecoveryCLITests(unittest.TestCase):
    """Recovery commands must run before any Inbox initialization."""

    setUp = CLITests.setUp
    run_cli = CLITests.run_cli

    def test_backup_verify_restore_and_receipt_journey(self):
        from relaybox.store import Inbox
        inbox = Inbox(self.db)
        job, _ = inbox.ingest("recover-cli", b'{"secret":"payload-is-not-a-receipt-field"}')
        backup = Path(self.tmp.name) / "备份 with spaces.sqlite3"
        result = self.run_cli("backup", str(backup))
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(result.stdout)
        self.assertEqual(metadata["counts"], {"queued": 1, "leased": 0, "succeeded": 0, "dead_letter": 0})
        self.assertNotIn("payload-is-not-a-receipt-field", result.stdout)
        verified = self.run_cli("verify-backup", str(backup))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout)["sha256"], metadata["sha256"])
        original = self.db
        restored = Path(self.tmp.name) / "恢复 inbox.sqlite3"
        self.db = restored
        restoration = self.run_cli("restore", str(backup))
        self.assertEqual(restoration.returncode, 0, restoration.stderr)
        self.assertEqual(json.loads(restoration.stdout)["counts"], metadata["counts"])
        before = restored.read_bytes()
        denied = self.run_cli("restore", str(backup))
        self.assertEqual(denied.returncode, 2)
        self.assertIn("error", json.loads(denied.stderr))
        self.assertEqual(restored.read_bytes(), before)
        receipts = Path(self.tmp.name) / "receipts"
        worked = self.run_cli("work", "--receipts", str(receipts))
        self.assertEqual(worked.returncode, 0, worked.stderr)
        self.assertEqual(json.loads(worked.stdout), {"claimed": 1, "succeeded": 1, "failed": 0, "lost_leases": 0})
        self.assertEqual(len(list(receipts.glob("*.json"))), 1)
        self.assertEqual(Inbox(restored).get(job["id"])["state"], "succeeded")
        self.assertEqual(inbox.get(job["id"])["state"], "queued")
        self.db = original

    def test_recovery_errors_do_not_create_db_or_parent(self):
        missing = Path(self.tmp.name) / "missing-source.sqlite3"
        output = Path(self.tmp.name) / "new.sqlite3"
        for command in [("backup", str(output)), ("verify-backup", str(missing)), ("restore", str(missing))]:
            with self.subTest(command=command):
                result = self.run_cli(*command)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error", json.loads(result.stderr))
                self.assertFalse(self.db.exists())
                self.assertFalse(output.exists())
                self.assertFalse(missing.exists())
        self.db = Path(self.tmp.name) / "absent-parent" / "new.sqlite3"
        result = self.run_cli("restore", str(missing))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.parent.exists())

    def test_recovery_timeout_options_are_controlled_json_errors(self):
        from relaybox.store import Inbox
        Inbox(self.db)
        backup = Path(self.tmp.name) / "backup.sqlite3"
        for timeout in ["nan", "inf", "0", "61"]:
            result = self.run_cli("backup", str(backup), "--timeout", timeout)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stderr))
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(backup.exists())


class OutboundCLITests(unittest.TestCase):
    setUp = CLITests.setUp
    run_cli = CLITests.run_cli

    def ok(self, *args, code=0):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, code, (args, result.stdout, result.stderr))
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout) if code != 2 else json.loads(result.stderr)

    def test_subscription_commands_print_a_secret_only_when_it_is_created(self):
        added = self.ok("subscription", "add", "http://127.0.0.1:9/hook", "--event-type", "b.created", "--event-type", "a.created")
        self.assertTrue(added["secret"].startswith("whsec_"))
        self.assertEqual(added["subscription"]["event_types"], ["a.created", "b.created"])
        listed = self.run_cli("subscription", "list")
        self.assertNotIn(added["secret"][len("whsec_"):], listed.stdout)
        self.assertEqual(json.loads(listed.stdout)["subscriptions"][0]["secrets"][0]["fingerprint"],
                         added["subscription"]["secrets"][0]["fingerprint"])
        rotated = self.ok("subscription", "rotate-secret", "1", "--grace-seconds", "60")
        self.assertNotEqual(rotated["secret"], added["secret"])
        self.assertEqual([item["active"] for item in rotated["subscription"]["secrets"]], [True, True])
        self.assertEqual(self.ok("subscription", "disable", "1")["subscription"]["state"], "disabled")
        self.assertEqual(self.ok("subscription", "enable", "1")["subscription"]["state"], "active")
        from relaybox.outbound import DEMO_SECRET
        self.assertEqual(self.ok("subscription", "add", "http://[::1]:9/", "--event-type", "*", "--demo-secret")["secret"], DEMO_SECRET)
        refused = self.ok("subscription", "add", "https://hooks.example.com/in", "--event-type", "x", code=2)
        self.assertIn("non-loopback", refused["error"])
        self.assertIn("https", self.ok("subscription", "add", "http://hooks.example.com/in", "--event-type", "x", "--allow-remote", code=2)["error"])
        self.ok("subscription", "add", "https://hooks.example.com/in", "--event-type", "x", "--allow-remote")
        self.assertEqual(self.ok("subscription", "rotate-secret", "99", code=2), {"error": "subscription not found"})
        self.assertEqual(self.run_cli("subscription", "add", "http://127.0.0.1:9/", "--event-type", "x", "--demo-secret",
                                      "--secret", added["secret"]).returncode, 2)

    def test_publish_deliver_replay_journey_against_a_live_receiver(self):
        from relaybox.outbound import DEMO_SECRET
        from tests.receivers import Receiver, independent_verify
        payload = Path(self.tmp.name) / "payload.json"
        payload.write_bytes(b'{"type":"example.created","synthetic":true}')
        with Receiver(422, 204) as receiver:
            self.ok("subscription", "add", receiver.url(), "--event-type", "example.created", "--demo-secret")
            published = self.ok("publish", "example.created", str(payload), "--webhook-id", "msg_cli_1")
            self.assertEqual((published["created"], published["message"]["deliveries"]), (True, 1))
            self.assertFalse(self.ok("publish", "example.created", str(payload), "--webhook-id", "msg_cli_1")["created"])
            failed = self.ok("deliver", "--timeout", "2", "--lease-seconds", "5", code=1)
            self.assertEqual((failed["claimed"], failed["dead_lettered"]), (1, 1))
            dead = self.ok("deliveries", "--state", "dead_letter")
            self.assertEqual([row["webhook_id"] for row in dead["deliveries"]], ["msg_cli_1"])
            self.assertEqual(dead["counts"]["dead_letter"], 1)
            self.assertEqual(self.ok("replay-delivery", "1")["delivery"]["state"], "queued")
            self.assertEqual(self.ok("deliver", "--timeout", "2", "--lease-seconds", "5")["succeeded"], 1)
            self.assertEqual(self.ok("deliver")["claimed"], 0)
        self.assertEqual([request["headers"]["webhook-id"] for request in receiver.requests], ["msg_cli_1", "msg_cli_1"])
        self.assertTrue(all(independent_verify(DEMO_SECRET, request["headers"], request["body"]) for request in receiver.requests))
        self.assertEqual(self.ok("replay-delivery", "999", code=2), {"error": "delivery not found"})
        self.assertIn("lease_seconds", self.ok("deliver", "--timeout", "30", "--lease-seconds", "30", code=2)["error"])
        self.assertIn("error", self.ok("publish", "bad type", str(payload), code=2))

    def test_relay_fans_out_inbound_jobs_once(self):
        from relaybox.store import Inbox
        inbox = Inbox(self.db)
        inbox.ingest("inbound-1", b'{"type":"order.paid","synthetic":true}')
        self.ok("subscription", "add", "http://127.0.0.1:9/hook", "--event-type", "order.paid")
        self.assertEqual(self.ok("relay", "--max-jobs", "5"), {"claimed": 1, "succeeded": 1, "failed": 0, "lost_leases": 0})
        queued = self.ok("deliveries")["deliveries"]
        self.assertEqual([(row["event_type"], row["state"]) for row in queued], [("order.paid", "queued")])
        self.assertEqual(self.ok("relay")["claimed"], 0)
        inbox.ingest("inbound-2", b'{"synthetic":true}')
        self.assertEqual(self.ok("relay", code=1)["failed"], 1)

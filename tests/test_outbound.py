"""Outbound store contracts: subscriptions, secrets, fan-out, leases and schema 2."""

import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest

from relaybox import standard_webhooks as swh
from relaybox.backup import backup_database, verify_backup
from relaybox.outbound import DEMO_SECRET, Outbox, Outcome, RelayHandler, parse_target
from relaybox.store import Conflict, Inbox, InvalidTransition, ValidationError
from relaybox.worker import run_worker

# The exact schema-1 DDL that RelayBox 0.4.1 executed, kept here so the upgrade
# test reads a real version-1 file rather than one built from today's constants.
SCHEMA_1 = ["""CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                delivery_id TEXT NOT NULL UNIQUE,
                digest TEXT NOT NULL,
                body BLOB NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('queued','leased','succeeded','dead_letter')),
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                base_delay REAL NOT NULL,
                max_delay REAL NOT NULL,
                available_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                lease_until REAL,
                lease_token TEXT,
                worker TEXT,
                last_error TEXT,
                replay_count INTEGER NOT NULL DEFAULT 0
            )""", "CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, available_at, lease_until, id)"]


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "outbox.sqlite3"
        self.now = 1000.0
        self.outbox = Outbox(self.path, clock=lambda: self.now, max_attempts=3, base_delay=2, max_delay=60,
                             rng=random.Random(1))

    def subscribe(self, types=("example.created",), url="http://127.0.0.1:9/hook", **kwargs):
        return self.outbox.subscribe(url, list(types), **kwargs)

    def test_only_literal_loopback_targets_pass_by_default(self):
        for url in ["http://127.0.0.1:8080/hook", "http://127.9.8.7/", "http://[::1]:9/x?a=1", "https://127.0.0.1/x",
                    "http://[::ffff:127.0.0.1]:9/"]:
            with self.subTest(url=url):
                self.assertTrue(parse_target(url).loopback)
        refused = ["http://localhost:8080/", "http://10.0.0.1/", "http://169.254.169.254/latest", "http://0.0.0.0:9/",
                   "http://127.1/", "http://2130706433/", "https://example.com/hook", "http://[fe80::1]/"]
        for url in refused:
            with self.subTest(url=url), self.assertRaisesRegex(ValidationError, "non-loopback"):
                parse_target(url)
        invalid = ["ftp://127.0.0.1/", "http:///path", "//127.0.0.1/", "http://user:pass@127.0.0.1/", "http://127.0.0.1/#x",
                   "http://127.0.0.1:0/", "http://127.0.0.1:99999/", "http://127.0.0.1/ space", "http://127.0.0.1/é",
                   "http://127.0.0.1/\x00", "http://127.0.0.1/" + "a" * 2048, None]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                parse_target(url, allow_remote=True)

    def test_remote_targets_need_the_flag_and_https(self):
        target = parse_target("https://hooks.example.com:8443/in?x=1", allow_remote=True)
        self.assertEqual((target.host, target.port, target.path, target.loopback), ("hooks.example.com", 8443, "/in?x=1", False))
        with self.assertRaisesRegex(ValidationError, "https"):
            parse_target("http://hooks.example.com/in", allow_remote=True)
        with self.assertRaisesRegex(ValidationError, "non-loopback"):
            self.subscribe(url="https://hooks.example.com/in")
        subscription, _ = self.subscribe(url="https://hooks.example.com/in", allow_remote=True)
        self.assertEqual(subscription["url"], "https://hooks.example.com/in")

    def test_secret_is_returned_once_and_never_listed(self):
        subscription, secret = self.subscribe()
        self.assertEqual(len(swh.decode_secret(secret)), 32)
        listed = json.dumps(self.outbox.subscriptions())
        self.assertNotIn(secret, listed)
        self.assertNotIn(secret[len("whsec_"):], listed)
        self.assertEqual(subscription["secrets"][0]["active"], True)
        self.assertRegex(subscription["secrets"][0]["fingerprint"], r"^[0-9a-f]{16}$")
        chosen, returned = self.subscribe(secret=DEMO_SECRET)
        self.assertEqual(returned, DEMO_SECRET)
        for bad in ["not-a-secret", "whsec_" + "A" * 8, swh.SECRET_PREFIX]:
            with self.subTest(secret=bad), self.assertRaises(ValidationError):
                self.subscribe(secret=bad)
        for types in [[], ["a" * 129], ["has space"], ["trailing."], ["*", "x.y"] * 20, "example.created"]:
            with self.subTest(types=types), self.assertRaises(ValidationError):
                self.outbox.subscribe("http://127.0.0.1:9/", types)
        self.assertEqual(len(self.outbox.subscriptions()), 2)

    def test_rotation_keeps_old_secrets_for_the_grace_period_only(self):
        subscription, first = self.subscribe()
        _, second = self.outbox.rotate_secret(subscription["id"], grace_seconds=100)
        self.assertNotEqual(first, second)
        message, _ = self.outbox.publish("example.created", b'{"n":1}')
        claim = self.outbox.claim("rotation")
        self.assertEqual(claim["secrets"], [swh.decode_secret(second), swh.decode_secret(first)])
        self.outbox.complete(claim["id"], claim["lease_token"], Outcome("success", 204))
        self.now += 100  # Exactly at expiry the old secret is retired.
        self.outbox.publish("example.created", b'{"n":2}')
        self.assertEqual(self.outbox.claim("rotation")["secrets"], [swh.decode_secret(second)])
        states = [item["active"] for item in self.outbox.subscriptions()[0]["secrets"]]
        self.assertEqual(states, [True, False])
        # A later rotation deletes expired keys and never extends an earlier expiry.
        _, third = self.outbox.rotate_secret(subscription["id"], grace_seconds=50)
        _, fourth = self.outbox.rotate_secret(subscription["id"], grace_seconds=500)
        secrets = self.outbox.subscriptions()[0]["secrets"]
        self.assertEqual([item["expires_at"] for item in secrets], [None, 1600, 1150])
        _, current = self.outbox.rotate_secret(subscription["id"], grace_seconds=0)
        self.assertEqual([item["active"] for item in self.outbox.subscriptions()[0]["secrets"]], [True, False, False, False])
        for _ in range(4):
            self.outbox.rotate_secret(subscription["id"], grace_seconds=10)
        with self.assertRaisesRegex(ValidationError, "at most 5"):
            self.outbox.rotate_secret(subscription["id"], grace_seconds=10)
        for grace in [-1, 30 * 86400 + 1, float("nan"), True]:
            with self.subTest(grace=grace), self.assertRaises(ValidationError):
                self.outbox.rotate_secret(subscription["id"], grace_seconds=grace)
        with self.assertRaises(KeyError):
            self.outbox.rotate_secret(999)

    def test_publish_fans_out_once_to_matching_active_subscriptions(self):
        exact, _ = self.subscribe(["example.created", "example.deleted"])
        wildcard, _ = self.subscribe(["*"])
        self.subscribe(["other.created"])
        disabled, _ = self.subscribe(["*"])
        self.outbox.set_subscription_state(disabled["id"], active=False)
        message, created = self.outbox.publish("example.created", b'{"a":1}', webhook_id="msg_fixed")
        self.assertTrue(created)
        self.assertEqual(message["deliveries"], 2)
        listing = self.outbox.list_deliveries()
        self.assertEqual(sorted(row["subscription_id"] for row in listing["deliveries"]), [exact["id"], wildcard["id"]])
        self.assertEqual({row["webhook_id"] for row in listing["deliveries"]}, {"msg_fixed"})
        again, created = self.outbox.publish("example.created", b'{"a":1}', webhook_id="msg_fixed")
        self.assertFalse(created)
        self.assertEqual(again["deliveries"], 2)
        self.assertEqual(self.outbox.stats()["queued"], 2)
        for event_type, body in [("example.created", b'{"a": 1}'), ("example.deleted", b'{"a":1}')]:
            with self.subTest(event_type=event_type, body=body), self.assertRaises(Conflict):
                self.outbox.publish(event_type, body, webhook_id="msg_fixed")
        for webhook_id in ["msg.1", "", "x" * 129, "msg 1"]:
            with self.subTest(webhook_id=webhook_id), self.assertRaises(ValidationError):
                self.outbox.publish("example.created", b"{}", webhook_id=webhook_id)
        for body in [b"[]", b"{", b'{"x":NaN}']:
            with self.subTest(body=body), self.assertRaises(ValidationError):
                self.outbox.publish("example.created", body)
        only_wildcard, _ = self.outbox.publish("nobody.listens", b"{}")
        self.assertEqual(only_wildcard["deliveries"], 1)
        self.assertRegex(only_wildcard["webhook_id"], r"^msg_[0-9a-f]{32}$")

    def test_deliveries_reuse_the_fenced_lease_state_machine(self):
        self.subscribe()
        self.outbox.publish("example.created", b"{}")
        first = self.outbox.claim("first", lease_seconds=10)
        self.assertEqual((first["attempts"], first["lease_until"], first["worker"]), (1, 1010, "first"))
        self.assertRegex(first["lease_token"], r"^[0-9a-f]{64}$")
        self.assertIsNone(self.outbox.claim("idle"))
        renewed = self.outbox.renew(first["id"], first["lease_token"], lease_seconds=30)
        self.assertEqual(renewed["lease_until"], 1030)
        self.now = 1030  # Exact expiry is expired.
        second = self.outbox.claim("second", lease_seconds=10)
        self.assertEqual((second["id"], second["attempts"]), (first["id"], 2))
        for outcome in [Outcome("success", 200), Outcome("retry", 503, "late")]:
            with self.subTest(outcome=outcome), self.assertRaises(InvalidTransition):
                self.outbox.complete(first["id"], first["lease_token"], outcome)
        self.assertEqual(self.outbox.complete(second["id"], second["lease_token"], Outcome("retry", 503, "HTTP 503")), "queued")
        self.now = 1100
        third = self.outbox.claim("third", lease_seconds=10)
        self.now = 1110  # The final attempt's lease expires: the next claim dead-letters it.
        self.assertIsNone(self.outbox.claim("reconciler"))
        final = self.outbox.get(third["id"])
        self.assertEqual((final["state"], final["attempts"], final["last_error"]), ("dead_letter", 3, "lease expired at attempt limit"))

    def test_concurrent_dispatcher_claims_have_one_owner_each(self):
        self.subscribe(["*"])
        for index in range(20):
            self.outbox.publish("example.created", json.dumps({"n": index}).encode())
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(lambda index: Outbox(self.path).claim(f"dispatcher-{index}"), range(30)))
        owned = [item for item in claims if item is not None]
        self.assertEqual(len(owned), 20)
        self.assertEqual(len({item["id"] for item in owned}), 20)
        self.assertEqual(len({item["lease_token"] for item in owned}), 20)

    def test_outcomes_set_state_status_and_backoff(self):
        self.subscribe()
        for index in range(4):
            self.outbox.publish("example.created", json.dumps({"n": index}).encode())
        results = []
        for outcome in [Outcome("success", 204), Outcome("retry", 503, "HTTP 503", retry_after=40),
                        Outcome("permanent", 422, "HTTP 422"), Outcome("retry", None, "connection refused")]:
            claim = self.outbox.claim("outcomes")
            results.append(self.outbox.complete(claim["id"], claim["lease_token"], outcome))
        self.assertEqual(results, ["succeeded", "queued", "dead_letter", "queued"])
        rows = {row["id"]: row for row in self.outbox.list_deliveries()["deliveries"]}
        self.assertEqual((rows[1]["last_status"], rows[1]["last_error"]), (204, None))
        self.assertEqual((rows[2]["last_status"], rows[2]["available_at"]), (503, 1040))  # Retry-After floor.
        self.assertEqual((rows[3]["last_status"], rows[3]["last_error"]), (422, "HTTP 422"))
        self.assertIsNone(rows[4]["last_status"])
        self.assertTrue(1000 <= rows[4]["available_at"] <= 1002)  # Full jitter below the first ceiling.
        for bad in [dict(kind="maybe"), dict(kind="success", error="x"), dict(kind="retry"), dict(kind="retry", error=" "),
                    dict(kind="retry", error="x" * 501), dict(kind="retry", error="x", retry_after=-1)]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Outcome(**bad)

    def test_gone_disables_subscription_and_replay_waits_for_enable(self):
        subscription, _ = self.subscribe()
        self.outbox.publish("example.created", b'{"n":1}')
        claim = self.outbox.claim("gone")
        self.assertEqual(self.outbox.complete(claim["id"], claim["lease_token"], Outcome("gone", 410, "HTTP 410 Gone")), "dead_letter")
        listed = self.outbox.subscriptions()[0]
        self.assertEqual((listed["state"], listed["disabled_reason"]), ("disabled", "receiver answered 410 Gone"))
        self.assertEqual(self.outbox.publish("example.created", b'{"n":2}')[0]["deliveries"], 0)
        with self.assertRaisesRegex(InvalidTransition, "enable"):
            self.outbox.replay(claim["id"])
        self.outbox.set_subscription_state(subscription["id"], active=True)
        replayed = self.outbox.replay(claim["id"])
        self.assertEqual((replayed["state"], replayed["attempts"], replayed["replay_count"], replayed["last_error"]),
                         ("queued", 0, 1, None))
        with self.assertRaises(InvalidTransition):
            self.outbox.replay(claim["id"])
        with self.assertRaises(KeyError):
            self.outbox.replay(999)

    def test_summaries_never_expose_bodies_tokens_or_secrets(self):
        _, secret = self.subscribe()
        self.outbox.publish("example.created", b'{"private":"body-text-not-listed"}')
        claim = self.outbox.claim("private-worker")
        encoded = json.dumps([self.outbox.list_deliveries(), self.outbox.get(claim["id"]), self.outbox.subscriptions()])
        for private in [claim["lease_token"], "body-text-not-listed", secret[len("whsec_"):], "lease_token", '"body"']:
            self.assertNotIn(private, encoded)
        self.assertIn("private-worker", encoded)  # Worker names are diagnostics, like inbound jobs.
        for options in [{"state": "unknown"}, {"limit": 0}, {"limit": 501}, {"limit": True}]:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.outbox.list_deliveries(**options)

    def test_relay_publishes_exact_inbound_bytes_once_per_delivery(self):
        self.subscribe(["order.paid"])
        inbox = Inbox(self.path, clock=lambda: self.now)
        body = b'{"type": "order.paid",  "id":"synthetic-1"}'
        job, _ = inbox.ingest("inbound-1", body)
        self.assertEqual(run_worker(inbox, RelayHandler(self.outbox)), {"claimed": 1, "succeeded": 1, "failed": 0, "lost_leases": 0})
        claim = self.outbox.claim("check")
        self.assertEqual(claim["body"], body)
        self.assertEqual(claim["event_type"], "order.paid")
        # A repeated relay (for example after an expired lease) creates nothing new.
        message, created = self.outbox.relay(job)
        self.assertFalse(created)
        self.assertEqual(message["webhook_id"], claim["webhook_id"])
        self.assertEqual(sum(self.outbox.stats().values()), 1)
        inbox.ingest("no-type", b'{"event":"order.paid"}')
        self.assertEqual(run_worker(inbox, RelayHandler(self.outbox))["failed"], 1)
        self.assertIn("event type", inbox.list_jobs(state="queued")[0]["last_error"])
        with self.assertRaises(ValidationError):
            self.outbox.relay({**job, "digest": "0" * 64})
        with self.assertRaises(ValidationError):
            RelayHandler(inbox)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "inbox.sqlite3"

    def version_1(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            for statement in SCHEMA_1:
                conn.execute(statement)
            conn.execute("PRAGMA user_version=1")
            conn.execute("""INSERT INTO jobs (delivery_id,digest,body,state,max_attempts,base_delay,max_delay,
                available_at,created_at,updated_at) VALUES ('old-1','d',X'7B7D','queued',3,2,60,1,1,1)""")

    def test_version_1_snapshots_still_verify_and_files_upgrade_in_place(self):
        self.version_1()
        snapshot = self.root / "v1-snapshot.sqlite3"
        receipt = backup_database(self.path, snapshot)
        self.assertEqual((receipt["schema_version"], receipt["jobs"]), (1, 1))
        self.assertNotIn("outbound", receipt)
        self.assertEqual(verify_backup(snapshot)["schema_version"], 1)
        Inbox(self.path)  # Opening with this release adds the outbound tables.
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT delivery_id FROM jobs").fetchall(), [("old-1",)])
        upgraded = backup_database(self.path, self.root / "v2-snapshot.sqlite3")
        self.assertEqual(upgraded["schema_version"], 2)
        self.assertEqual(upgraded["outbound"], {"subscriptions": 0, "messages": 0, "deliveries": 0,
                                                "counts": {"queued": 0, "leased": 0, "succeeded": 0, "dead_letter": 0}})
        restored = self.root / "restored-v1.sqlite3"
        backup_database(snapshot, restored)  # Restoring an old snapshot keeps version 1 until opened.
        self.assertEqual(Outbox(restored).stats(), {"queued": 0, "leased": 0, "succeeded": 0, "dead_letter": 0})

    def test_schema_2_backup_counts_outbound_rows(self):
        outbox = Outbox(self.path, clock=lambda: 1000)
        outbox.subscribe("http://127.0.0.1:9/", ["*"])
        outbox.publish("a.b", b"{}")
        outbox.claim("backup-worker")
        receipt = backup_database(self.path, self.root / "snapshot.sqlite3")
        self.assertEqual(receipt["outbound"]["counts"], {"queued": 0, "leased": 1, "succeeded": 0, "dead_letter": 0})
        self.assertEqual((receipt["outbound"]["subscriptions"], receipt["outbound"]["messages"]), (1, 1))

    def test_unknown_versions_and_altered_outbound_tables_are_refused(self):
        Outbox(self.path)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("UPDATE sqlite_schema SET sql=replace(sql,?,?) WHERE name='deliveries'", ("'dead_letter'", "'DEAD_LETTER'"))
        with self.assertRaisesRegex(ValidationError, "unrecognized"):
            backup_database(self.path, self.root / "altered.sqlite3")
        other = self.root / "future.sqlite3"
        Outbox(other)
        with closing(sqlite3.connect(other, isolation_level=None)) as conn:
            conn.execute("PRAGMA user_version=3")
        with self.assertRaisesRegex(ValidationError, "schema version"):
            backup_database(other, self.root / "future-snapshot.sqlite3")
        for opener in [Inbox, Outbox]:
            with self.subTest(opener=opener), self.assertRaisesRegex(ValidationError, "schema version"):
                opener(other)


if __name__ == "__main__":
    unittest.main()

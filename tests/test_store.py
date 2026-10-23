import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from relaybox.store import Conflict, Inbox, InvalidTransition, ValidationError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inbox.sqlite3"
        self.now = 1000.0
        self.inbox = Inbox(self.path, clock=lambda: self.now, max_attempts=3, base_delay=2, max_delay=3)

    def put(self, delivery="evt-1", body=b'{"type":"demo.created"}'):
        return self.inbox.ingest(delivery, body)

    def test_duplicate_is_idempotent_without_second_job(self):
        first, created = self.put()
        second, repeated = self.put()
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.inbox.list_jobs()), 1)

    def test_conflicting_payload_cannot_replace_original(self):
        original, _ = self.put()
        with self.assertRaises(Conflict):
            self.put(body=b'{"type":"different"}')
        self.assertEqual(self.inbox.get(original["id"])["payload"], {"type": "demo.created"})

    def test_digest_uses_raw_bytes(self):
        self.put(body=b'{"n":1}')
        with self.assertRaises(Conflict):
            self.put(body=b'{"n": 1}')

    def test_rejects_invalid_delivery_ids(self):
        for delivery in ["", "a b", "<script>", "a" * 129, "事件", 42]:
            with self.subTest(delivery=delivery), self.assertRaises(ValidationError):
                self.put(delivery=delivery)
        self.assertEqual(self.inbox.list_jobs(), [])

    def test_rejects_invalid_json_without_persistence(self):
        for body in [b"", b"not json", b"[]", b"null", b'{"n":NaN}', b'{"n":Infinity}', b'{"x":1,"x":2}', b'{"x":"\xff"}', b'{"x":"\\ud800"}']:
            with self.subTest(body=body), self.assertRaises(ValidationError):
                self.put(body=body)
        self.assertEqual(self.inbox.list_jobs(), [])

    def test_rejects_oversized_json(self):
        with self.assertRaises(ValidationError):
            self.put(body=b'{"x":"' + b"x" * 65536 + b'"}')
        self.assertEqual(self.inbox.list_jobs(), [])

    def test_persists_across_connections(self):
        job, _ = self.put(body='{"message":"你好"}'.encode())
        other = Inbox(self.path)
        self.assertEqual(other.get(job["id"])["payload"], {"message": "你好"})

    def test_claim_acknowledge_and_empty_claim(self):
        job, _ = self.put()
        claim = self.inbox.claim("worker-a", lease_seconds=10)
        self.assertEqual(claim["id"], job["id"])
        self.assertEqual(claim["attempts"], 1)
        self.assertEqual(claim["state"], "leased")
        self.assertEqual(claim["lease_until"], 1010)
        self.assertTrue(claim["lease_token"])
        # A fixed ASCII hex format can be pasted as a CLI option value safely.
        self.assertRegex(claim["lease_token"], r"^[0-9a-f]{64}$")
        self.assertIsNone(self.inbox.claim("worker-b"))
        self.inbox.ack(job["id"], claim["lease_token"])
        self.assertEqual(self.inbox.get(job["id"])["state"], "succeeded")
        with self.assertRaises(InvalidTransition):
            self.inbox.ack(job["id"], claim["lease_token"])

    def test_stale_worker_is_fenced_after_reclaim(self):
        job, _ = self.put()
        old = self.inbox.claim("old", lease_seconds=10)
        self.now = 1010
        new = self.inbox.claim("new", lease_seconds=10)
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        self.assertEqual(new["attempts"], 2)
        for action in [lambda: self.inbox.ack(job["id"], old["lease_token"]), lambda: self.inbox.fail(job["id"], old["lease_token"], "old failure")]:
            with self.assertRaises(InvalidTransition):
                action()
        self.inbox.ack(job["id"], new["lease_token"])

    def test_expired_worker_cannot_ack_before_reclaim(self):
        job, _ = self.put()
        claim = self.inbox.claim("worker", lease_seconds=10)
        self.now = 1010
        with self.assertRaises(InvalidTransition):
            self.inbox.ack(job["id"], claim["lease_token"])
        with self.assertRaises(InvalidTransition):
            self.inbox.fail(job["id"], claim["lease_token"], "late")

    def test_bounded_backoff_dead_letter_and_replay(self):
        job, _ = self.put()
        for attempt, delay in [(1, 2), (2, 3)]:
            claim = self.inbox.claim("worker")
            self.inbox.fail(job["id"], claim["lease_token"], "synthetic failure")
            state = self.inbox.get(job["id"])
            self.assertEqual(state["available_at"], self.now + delay)
            self.assertEqual(state["attempts"], attempt)
            self.assertIsNone(self.inbox.claim("worker"))
            self.now += delay
        claim = self.inbox.claim("worker")
        self.inbox.fail(job["id"], claim["lease_token"], "final failure")
        self.assertEqual(self.inbox.get(job["id"])["state"], "dead_letter")
        self.assertIsNone(self.inbox.claim("worker"))
        replayed = self.inbox.replay(job["id"])
        self.assertEqual(replayed["state"], "queued")
        self.assertEqual(replayed["attempts"], 0)
        self.assertEqual(replayed["replay_count"], 1)
        self.assertEqual(self.inbox.claim("worker")["attempts"], 1)

    def test_final_expired_lease_dead_letters(self):
        job, _ = self.put()
        for _ in range(3):
            self.assertIsNotNone(self.inbox.claim("worker", lease_seconds=1))
            self.now += 1
        self.assertIsNone(self.inbox.claim("worker"))
        self.assertEqual(self.inbox.get(job["id"])["state"], "dead_letter")

    def test_only_dead_letters_can_be_replayed(self):
        job, _ = self.put()
        with self.assertRaises(InvalidTransition):
            self.inbox.replay(job["id"])
        with self.assertRaises(KeyError):
            self.inbox.get(999)

    def test_bad_claim_options_are_rejected_without_changing_job(self):
        self.put()
        for kwargs in [{"worker": ""}, {"worker": "a", "lease_seconds": 0}, {"worker": "a", "lease_seconds": float("nan")}, {"worker": "a", "lease_seconds": 3601}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.inbox.claim(**kwargs)
        self.assertEqual(self.inbox.list_jobs()[0]["attempts"], 0)

    def test_concurrent_claims_have_one_owner_each(self):
        for index in range(20):
            self.put(delivery=f"evt-{index}")
        def claim(index):
            return Inbox(self.path).claim(f"worker-{index}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(claim, range(30)))
        owned = [item for item in claims if item is not None]
        self.assertEqual(len(owned), 20)
        self.assertEqual(len({item["id"] for item in owned}), 20)
        self.assertEqual(len({item["lease_token"] for item in owned}), 20)

    def test_concurrent_duplicates_persist_once(self):
        def put(_):
            return Inbox(self.path).ingest("same", b'{"n":1}')[1]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(put, range(16)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.inbox.list_jobs()), 1)

    def test_list_filter_limit_and_safe_summary(self):
        self.put()
        self.put(delivery="evt-2")
        claim = self.inbox.claim("worker")
        self.inbox.ack(claim["id"], claim["lease_token"])
        self.assertEqual(len(self.inbox.list_jobs(state="queued")), 1)
        self.assertEqual(len(self.inbox.list_jobs(limit=1)), 1)
        self.assertNotIn("lease_token", self.inbox.list_jobs()[0])
        self.assertNotIn("payload", self.inbox.list_jobs()[0])
        self.assertEqual(self.inbox.stats(), {"queued": 1, "leased": 0, "succeeded": 1, "dead_letter": 0})

    def test_invalid_settings_rejected(self):
        for kwargs in [{"max_attempts": 0}, {"max_attempts": True}, {"base_delay": -1}, {"max_delay": float("inf")}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                Inbox(self.path, **kwargs)

    def test_invalid_job_ids_are_validation_errors(self):
        for job_id in [-1, 0, True, 2 ** 100, "1"]:
            for action in [lambda: self.inbox.get(job_id), lambda: self.inbox.replay(job_id), lambda: self.inbox.ack(job_id, "wrong")]:
                with self.subTest(job_id=job_id), self.assertRaises(ValidationError):
                    action()

    def test_retry_policy_survives_a_differently_configured_connection(self):
        job, _ = self.put()
        other = Inbox(self.path, clock=lambda: self.now, max_attempts=1, base_delay=99)
        claim = other.claim("worker")
        other.fail(job["id"], claim["lease_token"], "synthetic failure")
        actual = other.get(job["id"])
        self.assertEqual(actual["state"], "queued")
        self.assertEqual(actual["available_at"], self.now + 2)
        self.assertEqual(actual["max_attempts"], 3)

    def test_wrong_token_preserves_current_owner(self):
        job, _ = self.put()
        claim = self.inbox.claim("worker")
        with self.assertRaises(InvalidTransition):
            self.inbox.ack(job["id"], "incorrect-token")
        self.assertEqual(self.inbox.get(job["id"])["lease_token"], claim["lease_token"])
        self.inbox.ack(job["id"], claim["lease_token"])

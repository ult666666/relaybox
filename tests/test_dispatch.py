"""Outbound delivery against live loopback receivers that misbehave on purpose."""

from contextlib import closing
from email.utils import formatdate
import math
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from relaybox import dispatch
from relaybox.dispatch import parse_retry_after, run_dispatcher, send
from relaybox.outbound import Outbox, Target, retry_delay
from relaybox.store import ValidationError
from tests.receivers import (PlainTextOnConnect, Receiver, endless, hang_up, independent_verify, raw,
                             refused_url, respond, slow, trickle)

NOW = 1_800_000_000.0  # A fixed synthetic clock (January 2027).


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.now = NOW
        self.count = 0

    def outbox(self, **policy):
        self.count += 1
        return Outbox(self.root / f"outbox-{self.count}.sqlite3", clock=lambda: self.now,
                      **{"max_attempts": 4, "base_delay": 2, "max_delay": 60, "rng": random.Random(11), **policy})

    def one(self, url, *, outbox=None, body=b'{"type":"example.created","n":1}', **options):
        """Subscribe url, publish one message and run one dispatch (timed in self.elapsed)."""
        outbox = outbox or self.outbox()
        subscription, secret = outbox.subscribe(url, ["example.created"])
        outbox.publish("example.created", body)
        started = time.monotonic()
        counts = run_dispatcher(outbox, **{"timeout": 0.5, "lease_seconds": 5, **options})
        self.elapsed = time.monotonic() - started
        delivery = outbox.list_deliveries()["deliveries"][0]
        return outbox, delivery, counts, secret

    def test_status_codes_decide_success_retry_or_dead_letter(self):
        cases = {"succeeded": [200, 202, 204, 299], "queued": [408, 429, 500, 502, 503, 504, 599],
                 "dead_letter": [400, 401, 403, 404, 409, 413, 418, 422, 451]}
        for expected, statuses in cases.items():
            for status in statuses:
                with self.subTest(status=status), Receiver(status) as receiver:
                    _, delivery, counts, secret = self.one(receiver.url())
                    self.assertEqual((delivery["state"], delivery["last_status"]), (expected, status))
                    self.assertEqual(counts["claimed"], 1)
                    self.assertEqual(len(receiver.requests), 1)
                    self.assertTrue(independent_verify(secret, receiver.requests[0]["headers"], receiver.requests[0]["body"]))

    def test_unexpected_status_lines_and_broken_responses_are_retried(self):
        behaviors = {
            "HTTP 600: unexpected status code": raw(b"HTTP/1.1 600 Unknown\r\nContent-Length: 0\r\n\r\n"),
            "HTTP 102: unexpected status code": raw(b"HTTP/1.1 102 Processing\r\n\r\n"),
            "invalid HTTP response": raw(b"definitely not HTTP\r\n\r\n"),
            "connection closed before a response": hang_up,
        }
        for error, behavior in behaviors.items():
            with self.subTest(error=error), Receiver(behavior) as receiver:
                _, delivery, _, _ = self.one(receiver.url())
                self.assertEqual((delivery["state"], delivery["attempts"], delivery["last_error"]), ("queued", 1, error))

    def test_refused_slow_and_trickling_receivers_are_retried_within_the_deadline(self):
        _, delivery, _, _ = self.one(refused_url())
        self.assertEqual((delivery["state"], delivery["last_error"], delivery["last_status"]), ("queued", "connection refused", None))
        for name, behavior in [("slow", slow(3)), ("trickle", trickle(0.05, 3))]:
            with self.subTest(name), Receiver(behavior) as receiver:
                _, delivery, _, _ = self.one(receiver.url(), timeout=0.4)
                self.assertEqual(delivery["state"], "queued")
                self.assertEqual(delivery["last_error"], "timed out after 0.4 s")
                # Each trickled byte arrives well inside the socket timeout, so
                # only the wall-clock deadline can end that attempt this early.
                self.assertLess(self.elapsed, 2.5)  # The trickle alone would take over 50 s.

    def test_response_bodies_are_read_only_up_to_the_limit(self):
        for status, state in [(200, "succeeded"), (500, "queued")]:
            with self.subTest(status=status), Receiver(endless(status)) as receiver:
                _, delivery, _, _ = self.one(receiver.url(), timeout=5, lease_seconds=10)
                self.assertLess(self.elapsed, 4)  # 64 MiB was advertised; the deadline is 5 s.
                self.assertEqual(delivery["state"], state)
        self.assertEqual(delivery["last_error"], "HTTP 500: " + "x" * 200)
        with Receiver(endless(200)) as receiver:
            response = send(Target("http", "127.0.0.1", receiver.server.server_port, "/", True), {}, b"{}",
                            timeout=5, max_response_bytes=1000)
        self.assertEqual((response.status, len(response.body)), (200, 1000))

    def test_redirects_are_never_followed(self):
        with Receiver() as elsewhere:
            for status in [301, 302, 303, 307, 308]:
                with self.subTest(status=status), Receiver(respond(status, [("Location", elsewhere.url())])) as receiver:
                    _, delivery, _, _ = self.one(receiver.url())
                    self.assertEqual((delivery["state"], delivery["last_status"]), ("dead_letter", status))
                    self.assertIn("redirect not followed", delivery["last_error"])
            self.assertEqual(elsewhere.requests, [])

    def test_gone_disables_the_subscription_and_later_deliveries_send_nothing(self):
        with Receiver(410) as receiver:
            outbox = self.outbox()
            outbox.subscribe(receiver.url(), ["example.created"])
            for index in range(2):
                outbox.publish("example.created", f'{{"n":{index}}}'.encode())
            counts = run_dispatcher(outbox, max_deliveries=5, timeout=0.5, lease_seconds=5)
        self.assertEqual(counts["dead_lettered"], 2)
        self.assertEqual(len(receiver.requests), 1)
        errors = [row["last_error"] for row in outbox.list_deliveries()["deliveries"]]
        self.assertEqual(errors, ["subscription is disabled; no request was sent", "HTTP 410 Gone: subscription disabled"])

    def test_retry_after_raises_the_delay_and_is_capped(self):
        cases = [([("Retry-After", "30")], 30), ([("Retry-After", "100000")], 60),
                 ([("Retry-After", formatdate(NOW + 45, usegmt=True))], 45),
                 ([("Retry-After", formatdate(NOW - 45, usegmt=True))], None), ([("Retry-After", "soon")], None)]
        for headers, floor in cases:
            for status in [429, 503]:
                with self.subTest(headers=headers, status=status), Receiver(respond(status, headers)) as receiver:
                    _, delivery, _, _ = self.one(receiver.url())
                    delay = delivery["available_at"] - NOW
                    if floor is None:
                        self.assertTrue(0 <= delay <= 2, delay)  # Full jitter below the first ceiling.
                    else:
                        self.assertEqual(delay, floor)
        with Receiver(respond(400, [("Retry-After", "30")])) as receiver:
            _, delivery, _, _ = self.one(receiver.url())
        self.assertEqual(delivery["state"], "dead_letter")
        self.assertEqual(parse_retry_after(" 7 ", NOW), 7)
        for value in [None, "", "-1", "1.5", "7 seconds", "Mon, 99 Foo 2026 00:00:00 GMT", "Wed, 21 Oct 2015 07:28:00"]:
            with self.subTest(value=value):
                self.assertIsNone(parse_retry_after(value, NOW))

    def test_huge_retry_after_is_capped_and_never_ends_the_run(self):
        # The receiver controls this header. A value above the Outcome bound
        # (10^9 s) used to raise out of run_dispatcher: the delivery stayed
        # leased and the deliveries behind it in the run were never attempted.
        for value in ["1000000001", "9999999999", "Fri, 31 Dec 9999 23:59:59 GMT"]:
            with self.subTest(value=value), Receiver(respond(503, [("Retry-After", value)])) as hostile, \
                    Receiver(204) as healthy:
                outbox = self.outbox()
                for receiver in (hostile, healthy):
                    outbox.subscribe(receiver.url(), ["example.created"])
                outbox.publish("example.created", b"{}")
                counts = run_dispatcher(outbox, max_deliveries=5, timeout=0.5, lease_seconds=5)
                self.assertEqual((counts["claimed"], counts["retried"], counts["succeeded"]), (2, 1, 1))
                self.assertEqual(len(healthy.requests), 1)
                retried = outbox.get(1)
                self.assertEqual((retried["state"], retried["available_at"] - NOW), ("queued", 60))  # max_delay.
                self.assertEqual(parse_retry_after(value, NOW), 86_400)

    def test_failures_follow_the_full_jitter_schedule_then_dead_letter(self):
        oracle = random.Random(11)  # Same seed as the outbox: the exact expected draws.
        with Receiver(default=503) as receiver:
            outbox, delivery, _, secret = self.one(receiver.url())
            schedule = [(self.now, delivery["available_at"])]
            while delivery["state"] == "queued":
                self.now = delivery["available_at"]
                run_dispatcher(outbox, timeout=0.5, lease_seconds=5)
                delivery = outbox.list_deliveries()["deliveries"][0]
                if delivery["state"] == "queued":
                    schedule.append((self.now, delivery["available_at"]))
        self.assertEqual((delivery["state"], delivery["attempts"]), ("dead_letter", 4))
        self.assertEqual([available for _, available in schedule],
                         [failed_at + oracle.uniform(0, ceiling) for (failed_at, _), ceiling in zip(schedule, [2, 4, 8])])
        self.assertEqual(len(receiver.requests), 4)
        self.assertEqual({request["headers"]["webhook-id"] for request in receiver.requests}, {delivery["webhook_id"]})
        self.assertEqual({request["body"] for request in receiver.requests}, {b'{"type":"example.created","n":1}'})
        stamps = [int(request["headers"]["webhook-timestamp"]) for request in receiver.requests]
        self.assertEqual(stamps, sorted(stamps))  # A fresh attempt timestamp each time.
        for request in receiver.requests:
            self.assertTrue(independent_verify(secret, request["headers"], request["body"]))

    def test_crash_mid_delivery_redelivers_with_the_same_webhook_id(self):
        database = self.root / "crash.sqlite3"
        with Receiver() as receiver:
            receiver.behaviors.append(receiver.hold())
            outbox = Outbox(database)
            _, secret = outbox.subscribe(receiver.url(), ["example.created"])
            outbox.publish("example.created", b'{"type":"example.created"}')
            worker = subprocess.Popen([sys.executable, "-m", "relaybox", "--db", str(database), "deliver",
                                       "--timeout", "20", "--lease-seconds", "30"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                receiver.wait_for(1)
            finally:
                worker.kill()  # SIGKILL on POSIX: no cleanup, the lease stays.
                worker.communicate(timeout=10)
            crashed = outbox.list_deliveries()["deliveries"][0]
            self.assertEqual((crashed["state"], crashed["attempts"]), ("leased", 1))
            later = Outbox(database, clock=lambda: time.time() + 60)
            counts = run_dispatcher(later, timeout=2, lease_seconds=5)
            self.assertEqual((counts["claimed"], counts["succeeded"]), (1, 1))
        first, second = receiver.requests
        self.assertEqual(first["headers"]["webhook-id"], second["headers"]["webhook-id"])
        self.assertEqual(first["body"], second["body"])
        self.assertGreater(int(second["headers"]["webhook-timestamp"]), int(first["headers"]["webhook-timestamp"]))
        self.assertTrue(all(independent_verify(secret, request["headers"], request["body"]) for request in receiver.requests))
        self.assertEqual(later.list_deliveries()["deliveries"][0]["attempts"], 2)

    def test_lease_expiry_during_a_slow_attempt_is_fenced_and_redelivered(self):
        outbox = self.outbox()
        results = []

        def expire_then_answer(handler):
            self.now += 100  # The first attempt's lease is now expired.
            results.append(run_dispatcher(outbox, worker="replacement", timeout=2, lease_seconds=5))
            respond(200)(handler)

        with Receiver(expire_then_answer, 204) as receiver:
            _, delivery, counts, _ = self.one(receiver.url(), outbox=outbox, timeout=4)
        self.assertEqual((counts["claimed"], counts["lost_leases"], counts["succeeded"]), (1, 1, 0))
        self.assertEqual((results[0]["claimed"], results[0]["succeeded"]), (1, 1))
        self.assertEqual((delivery["state"], delivery["attempts"], delivery["last_status"]), ("succeeded", 2, 204))
        self.assertEqual(len({request["headers"]["webhook-id"] for request in receiver.requests}), 1)

    def test_dead_letter_replay_redelivers_with_the_same_webhook_id(self):
        with Receiver(422, 204) as receiver:
            outbox, delivery, counts, _ = self.one(receiver.url())
            self.assertEqual((delivery["state"], counts["dead_lettered"]), ("dead_letter", 1))
            replayed = outbox.replay(delivery["id"])
            self.assertEqual((replayed["state"], replayed["attempts"], replayed["replay_count"]), ("queued", 0, 1))
            self.assertEqual(run_dispatcher(outbox, timeout=0.5, lease_seconds=5)["succeeded"], 1)
        self.assertEqual([request["headers"]["webhook-id"] for request in receiver.requests], [delivery["webhook_id"]] * 2)
        self.assertEqual(outbox.get(delivery["id"])["state"], "succeeded")

    def test_rotation_signs_with_each_active_secret_until_the_grace_period_ends(self):
        with Receiver() as receiver:
            outbox = self.outbox()
            subscription, old = outbox.subscribe(receiver.url(), ["example.created"])
            _, new = outbox.rotate_secret(subscription["id"], grace_seconds=600)
            outbox.publish("example.created", b'{"n":1}')
            run_dispatcher(outbox, timeout=0.5, lease_seconds=5)
            self.now += 600
            outbox.publish("example.created", b'{"n":2}')
            run_dispatcher(outbox, timeout=0.5, lease_seconds=5)
        during, after = receiver.requests
        self.assertEqual(len(during["headers"]["webhook-signature"].split(" ")), 2)
        self.assertTrue(independent_verify(old, during["headers"], during["body"]))
        self.assertTrue(independent_verify(new, during["headers"], during["body"]))
        self.assertEqual(len(after["headers"]["webhook-signature"].split(" ")), 1)
        self.assertFalse(independent_verify(old, after["headers"], after["body"]))
        self.assertTrue(independent_verify(new, after["headers"], after["body"]))

    def test_request_shape_matches_the_specification(self):
        with Receiver() as receiver:
            outbox, delivery, _, _ = self.one(receiver.url("/in?source=relaybox"))
        request = receiver.requests[0]
        self.assertEqual(request["path"], "/in?source=relaybox")
        self.assertEqual(request["headers"]["content-type"], "application/json")
        self.assertEqual(request["headers"]["webhook-timestamp"], str(int(NOW)))
        self.assertRegex(request["headers"]["webhook-id"], r"^msg_[0-9a-f]{32}$")
        self.assertRegex(request["headers"]["webhook-signature"], r"^v1,[A-Za-z0-9+/]{43}=$")
        self.assertTrue(request["headers"]["user-agent"].startswith("RelayBox/"))

    def test_targets_are_checked_again_before_any_connection(self):
        outbox = self.outbox()
        subscription, _ = outbox.subscribe("https://hooks.example.com/in", ["example.created"], allow_remote=True)
        outbox.publish("example.created", b"{}")
        with patch("relaybox.dispatch.send", side_effect=AssertionError("no request may be sent")):
            self.assertEqual(run_dispatcher(outbox, timeout=0.5, lease_seconds=5)["dead_lettered"], 1)
            # A hand-edited database cannot downgrade a remote target to plain HTTP either.
            with closing(sqlite3.connect(outbox.path, isolation_level=None)) as conn:
                conn.execute("UPDATE subscriptions SET url='http://10.0.0.1/in'")
            outbox.replay(1)
            self.assertEqual(run_dispatcher(outbox, timeout=0.5, lease_seconds=5, allow_remote=True)["dead_lettered"], 1)
        errors = outbox.get(1)["last_error"]
        self.assertIn("target refused, no request was sent", errors)
        self.assertIn("https", errors)

    def test_https_uses_certificate_verification(self):
        contexts = []
        real = dispatch.ssl.create_default_context

        def spy(*args, **kwargs):
            contexts.append(real(*args, **kwargs))
            return contexts[-1]

        with PlainTextOnConnect() as server, patch("relaybox.dispatch.ssl.create_default_context", spy):
            target = Target("https", "127.0.0.1", server.port, "/", True)
            with self.assertRaisesRegex(dispatch.DeliveryError, "TLS"):
                send(target, {}, b"{}", timeout=2)  # Plain HTTP bytes cannot complete a TLS handshake.
        self.assertEqual(len(contexts), 1)
        self.assertTrue(contexts[0].check_hostname)
        self.assertEqual(contexts[0].verify_mode, dispatch.ssl.CERT_REQUIRED)

    def test_wait_sleeps_until_a_retry_is_due(self):
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            self.now += seconds

        with Receiver(respond(503, [("Retry-After", "10")]), 204) as receiver:
            _, delivery, counts, _ = self.one(receiver.url(), max_deliveries=5, wait=60, sleep=sleep)
        self.assertEqual(counts, {"claimed": 2, "succeeded": 1, "retried": 1, "dead_lettered": 0, "lost_leases": 0, "awaiting_retry": 0})
        self.assertEqual(delivery["state"], "succeeded")
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 10.01)
        with Receiver(503) as receiver:
            _, _, counts, _ = self.one(receiver.url(), max_deliveries=5)
        self.assertEqual((counts["claimed"], counts["awaiting_retry"]), (1, 1))

    def test_configuration_is_validated_before_claiming(self):
        outbox = self.outbox()
        outbox.subscribe("http://127.0.0.1:9/", ["*"])
        outbox.publish("a.b", b"{}")
        bad = [{"timeout": 5, "lease_seconds": 5}, {"max_deliveries": 0}, {"max_deliveries": True}, {"timeout": float("nan")},
               {"timeout": 0}, {"wait": -1}, {"wait": 3601}, {"max_response_bytes": 65537}, {"worker": ""}]
        for options in bad:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                run_dispatcher(outbox, **{"timeout": 1, "lease_seconds": 5, **options})
        self.assertEqual(outbox.stats()["queued"], 1)


class BackoffPropertyTests(unittest.TestCase):
    def test_every_delay_stays_inside_its_window(self):
        rng = random.Random(9162)
        for _ in range(5000):
            attempts, base, cap = rng.randint(1, 100), rng.uniform(0, 1000), rng.uniform(0, 86_400)
            retry_after = rng.choice([None, rng.uniform(0, 200_000)])
            ceiling = min(cap, base * 2 ** (attempts - 1))
            floor = 0 if retry_after is None else min(retry_after, cap)
            delay = retry_delay(attempts, base, cap, rng, retry_after)
            self.assertTrue(floor <= delay <= max(ceiling, floor), (attempts, base, cap, retry_after, delay))
            self.assertLessEqual(delay, cap)

    def test_ceilings_double_until_the_cap(self):
        ceiling = [retry_delay(n, 2, 60, type("Top", (), {"uniform": staticmethod(lambda a, b: b)})()) for n in range(1, 10)]
        self.assertEqual(ceiling, [2, 4, 8, 16, 32, 60, 60, 60, 60])

    def test_full_jitter_is_uniform_over_the_whole_window(self):
        # Kolmogorov-Smirnov against Uniform(0, ceiling). Fixed seed, so this is
        # deterministic; D must stay below the alpha=0.01 critical value 1.628/sqrt(n).
        rng, n, ceiling = random.Random(2026), 20_000, 32
        samples = sorted(retry_delay(5, 2, 60, rng) / ceiling for _ in range(n))
        statistic = max(max((i + 1) / n - value, value - i / n) for i, value in enumerate(samples))
        self.assertLess(statistic, 1.628 / math.sqrt(n))
        self.assertAlmostEqual(sum(samples) / n, 0.5, delta=4 * (1 / math.sqrt(12)) / math.sqrt(n))
        # Synchronized failures spread out instead of retrying together.
        self.assertGreater(sum(value < 0.1 for value in samples), 0.09 * n)


if __name__ == "__main__":
    unittest.main()

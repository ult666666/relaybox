"""Standard Webhooks v1 signatures, checked against the specification's published vector."""

import base64
import random
import unittest

from relaybox import standard_webhooks as swh
from tests.receivers import independent_verify

# Published by the specification repository's reference libraries (identical in
# the Python, Go, Java and C# tests), for example test_sign_function in
# https://github.com/standard-webhooks/standard-webhooks/blob/main/libraries/python/tests/test_webhooks.py
VECTOR_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
VECTOR_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
VECTOR_TIMESTAMP = 1614265330
VECTOR_BODY = b'{"test": 2432232314}'
VECTOR_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


class StandardWebhooksTests(unittest.TestCase):
    def headers(self, keys, webhook_id=VECTOR_ID, timestamp=VECTOR_TIMESTAMP, body=VECTOR_BODY):
        return {"webhook-id": webhook_id, "webhook-timestamp": str(timestamp),
                "webhook-signature": swh.signature_header(keys, webhook_id, timestamp, body)}

    def test_published_vector(self):
        key = swh.decode_secret(VECTOR_SECRET)
        self.assertEqual(swh.sign(key, VECTOR_ID, VECTOR_TIMESTAMP, VECTOR_BODY), VECTOR_SIGNATURE)
        headers = {"webhook-id": VECTOR_ID, "webhook-timestamp": str(VECTOR_TIMESTAMP), "webhook-signature": VECTOR_SIGNATURE}
        self.assertEqual(swh.verify([key], headers, VECTOR_BODY, now=VECTOR_TIMESTAMP), VECTOR_ID)
        self.assertTrue(independent_verify(VECTOR_SECRET, headers, VECTOR_BODY))

    def test_any_change_to_id_timestamp_body_or_key_fails(self):
        key = swh.decode_secret(VECTOR_SECRET)
        good = self.headers([key])
        changes = [
            ({**good, "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJel"}, VECTOR_BODY, key),
            ({**good, "webhook-timestamp": str(VECTOR_TIMESTAMP + 1)}, VECTOR_BODY, key),
            (good, VECTOR_BODY + b" ", key),
            (good, b'{"test":2432232314}', key),
            (good, VECTOR_BODY, swh.decode_secret(swh.generate_secret())),
        ]
        for headers, body, verifier_key in changes:
            with self.subTest(headers=headers, body=body), self.assertRaises(swh.SignatureError):
                swh.verify([verifier_key], headers, body, now=int(headers["webhook-timestamp"]))

    def test_rotation_header_verifies_with_either_secret(self):
        old, new = (swh.decode_secret(swh.generate_secret()) for _ in range(2))
        headers = self.headers([new, old])
        self.assertEqual(len(headers["webhook-signature"].split(" ")), 2)
        for key in [old, new]:
            with self.subTest(key=key):
                swh.verify([key], headers, VECTOR_BODY, now=VECTOR_TIMESTAMP)
                self.assertTrue(independent_verify(swh.encode_secret(key), headers, VECTOR_BODY))
        with self.assertRaises(swh.SignatureError):
            swh.verify([swh.decode_secret(swh.generate_secret())], headers, VECTOR_BODY, now=VECTOR_TIMESTAMP)

    def test_signature_list_ignores_other_versions_and_malformed_entries(self):
        key = swh.decode_secret(VECTOR_SECRET)
        for header in [f"v2,{VECTOR_SIGNATURE[3:]} {VECTOR_SIGNATURE}", f",,, v1,not,base64  {VECTOR_SIGNATURE}", f"v1 {VECTOR_SIGNATURE}"]:
            with self.subTest(header=header):
                swh.verify([key], {"webhook-id": VECTOR_ID, "webhook-timestamp": str(VECTOR_TIMESTAMP), "webhook-signature": header},
                           VECTOR_BODY, now=VECTOR_TIMESTAMP)
        for header in [f"v2,{VECTOR_SIGNATURE[3:]}", "v1,", VECTOR_SIGNATURE.lower(), VECTOR_SIGNATURE[:-2] + "A="]:
            with self.subTest(header=header), self.assertRaises(swh.SignatureError):
                swh.verify([key], {"webhook-id": VECTOR_ID, "webhook-timestamp": str(VECTOR_TIMESTAMP), "webhook-signature": header},
                           VECTOR_BODY, now=VECTOR_TIMESTAMP)

    def test_timestamp_tolerance_window_and_required_headers(self):
        key = swh.decode_secret(VECTOR_SECRET)
        headers = self.headers([key])
        for now in [VECTOR_TIMESTAMP - 300, VECTOR_TIMESTAMP + 300]:
            swh.verify([key], headers, VECTOR_BODY, now=now)
        for now in [VECTOR_TIMESTAMP - 301, VECTOR_TIMESTAMP + 301]:
            with self.subTest(now=now), self.assertRaises(swh.SignatureError):
                swh.verify([key], headers, VECTOR_BODY, now=now)
        for name in ["webhook-id", "webhook-timestamp", "webhook-signature"]:
            with self.subTest(missing=name), self.assertRaises(swh.SignatureError):
                swh.verify([key], {k: v for k, v in headers.items() if k != name}, VECTOR_BODY, now=VECTOR_TIMESTAMP)
        for timestamp in ["", "01", "1e9", "+1614265330", "1614265330.0"]:
            with self.subTest(timestamp=timestamp), self.assertRaises(swh.SignatureError):
                swh.verify([key], {**headers, "webhook-timestamp": timestamp}, VECTOR_BODY, now=VECTOR_TIMESTAMP)
        upper = {name.upper(): value for name, value in headers.items()}
        self.assertEqual(swh.verify([key], upper, VECTOR_BODY, now=VECTOR_TIMESTAMP), VECTOR_ID)

    def test_secret_encoding_matches_spec_range_and_format(self):
        generated = swh.generate_secret()
        self.assertTrue(generated.startswith("whsec_"))
        self.assertEqual(len(swh.decode_secret(generated)), 32)
        for size in [24, 64]:
            raw = bytes(range(size))
            self.assertEqual(swh.decode_secret(swh.encode_secret(raw)), raw)
            self.assertEqual(swh.decode_secret(swh.encode_secret(raw).rstrip("=")), raw)  # Padding optional.
        bad = ["MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "whsec_", "whsec_" + base64.b64encode(b"x" * 23).decode(),
               "whsec_" + base64.b64encode(b"x" * 65).decode(), "whsec_abc$", "whsec_" + "A" * 33 + "===", None]
        for secret in bad:
            with self.subTest(secret=secret), self.assertRaises(swh.SignatureError):
                swh.decode_secret(secret)

    def test_ids_with_full_stops_or_invalid_timestamps_are_not_signed(self):
        key = swh.decode_secret(VECTOR_SECRET)
        for webhook_id, timestamp in [("msg.1", 1), ("", 1), ("x" * 129, 1), (VECTOR_ID, -1), (VECTOR_ID, True), (VECTOR_ID, 1.5)]:
            with self.subTest(webhook_id=webhook_id, timestamp=timestamp), self.assertRaises(swh.SignatureError):
                swh.sign(key, webhook_id, timestamp, b"{}")
        with self.assertRaises(swh.SignatureError):
            swh.signature_header([], VECTOR_ID, 1, b"{}")

    def test_random_messages_agree_with_independent_verifier(self):
        rng = random.Random(6962)
        for _ in range(200):
            keys = [rng.randbytes(rng.randint(24, 64)) for _ in range(rng.randint(1, 3))]
            body = rng.randbytes(rng.randint(0, 300))
            webhook_id = "msg_" + "".join(rng.choice("abcdef0123456789_-") for _ in range(rng.randint(1, 40)))
            timestamp = rng.randint(0, 2 ** 33)
            headers = self.headers(keys, webhook_id, timestamp, body)
            for key in keys:
                self.assertTrue(independent_verify(swh.encode_secret(key), headers, body))
            tampered = bytearray(body + b"!")
            tampered[rng.randrange(len(tampered))] ^= 1 + rng.randrange(255)
            self.assertFalse(independent_verify(swh.encode_secret(keys[0]), headers, bytes(tampered)))


if __name__ == "__main__":
    unittest.main()

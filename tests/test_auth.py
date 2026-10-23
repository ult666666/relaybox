import hashlib
import hmac
import unittest

from relaybox.auth import AuthError, sign, verify


class AuthTests(unittest.TestCase):
    secret = b"synthetic-test-secret-for-relaybox"
    body = b'{"message":"hello"}'

    def test_signature_covers_timestamp_delivery_and_body(self):
        expected = "sha256=" + hmac.new(self.secret, b"1000.evt-1." + self.body, hashlib.sha256).hexdigest()
        self.assertEqual(sign(self.secret, 1000, "evt-1", self.body), expected)
        verify(self.secret, "1000", expected, "evt-1", self.body, now=1000)

    def test_rejects_modified_body(self):
        with self.assertRaises(AuthError):
            verify(self.secret, "1000", sign(self.secret, 1000, "evt-1", self.body), "evt-1", b"{}", now=1000)

    def test_rejects_wrong_key(self):
        with self.assertRaises(AuthError):
            verify(b"another-synthetic-secret", "1000", sign(self.secret, 1000, "evt-1", self.body), "evt-1", self.body, now=1000)

    def test_accepts_tolerance_boundary(self):
        for now in [700, 1300]:
            verify(self.secret, "1000", sign(self.secret, 1000, "evt-1", self.body), "evt-1", self.body, now=now)

    def test_rejects_old_and_future_timestamps(self):
        for now in [699, 1301]:
            with self.subTest(now=now), self.assertRaises(AuthError):
                verify(self.secret, "1000", sign(self.secret, 1000, "evt-1", self.body), "evt-1", self.body, now=now)

    def test_rejects_malformed_timestamp_and_signature(self):
        for timestamp in ["", "01", "+1000", "1e3", "1.0", "١٠٠٠", "1" * 30]:
            with self.subTest(timestamp=timestamp), self.assertRaises(AuthError):
                verify(self.secret, timestamp, sign(self.secret, 1000, "evt-1", self.body), "evt-1", self.body, now=1000)
        for signature in ["", "x" * 64, "sha256=" + "g" * 64, "sha256=" + "0" * 64, "sha256=é"]:
            with self.subTest(signature=signature), self.assertRaises(AuthError):
                verify(self.secret, "1000", signature, "evt-1", self.body, now=1000)

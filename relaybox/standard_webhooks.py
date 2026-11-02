"""Standard Webhooks 1.0.0 symmetric signatures (``v1``, HMAC-SHA256).

Specification: https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md
The signed content is ``webhook-id + "." + webhook-timestamp + "." + body``.
Each signature is ``v1,`` plus the base64 HMAC-SHA256 of that content, and the
``webhook-signature`` header lists one signature per active secret, separated
by spaces, so a receiver keeps working while its secret rotates.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time

SECRET_PREFIX = "whsec_"
MIN_SECRET_BYTES, MAX_SECRET_BYTES = 24, 64  # The spec's range for symmetric keys.
WEBHOOK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")  # No "." so the signed fields stay unambiguous.


class SignatureError(ValueError):
    """A secret, header or signature is outside the supported contract."""


def generate_secret() -> str:
    """Return a new random 32-byte secret in the spec's ``whsec_`` form."""
    return encode_secret(secrets.token_bytes(32))


def encode_secret(key: bytes) -> str:
    if not isinstance(key, bytes) or not MIN_SECRET_BYTES <= len(key) <= MAX_SECRET_BYTES:
        raise SignatureError(f"a signing secret must be {MIN_SECRET_BYTES}–{MAX_SECRET_BYTES} bytes")
    return SECRET_PREFIX + base64.b64encode(key).decode("ascii")


def decode_secret(text: str) -> bytes:
    """Decode ``whsec_`` + base64 (padding optional) into 24–64 key bytes."""
    if not isinstance(text, str) or not text.startswith(SECRET_PREFIX):
        raise SignatureError("a signing secret must start with whsec_")
    encoded = text[len(SECRET_PREFIX):]
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encoded):
        raise SignatureError("a signing secret must be standard base64 after whsec_")
    try:
        key = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    except binascii.Error as exc:
        raise SignatureError("a signing secret must be standard base64 after whsec_") from exc
    if not MIN_SECRET_BYTES <= len(key) <= MAX_SECRET_BYTES:
        raise SignatureError(f"a signing secret must decode to {MIN_SECRET_BYTES}–{MAX_SECRET_BYTES} bytes")
    return key


def _content(webhook_id, timestamp, body):
    if not isinstance(webhook_id, str) or not WEBHOOK_ID.fullmatch(webhook_id):
        raise SignatureError("webhook-id must contain 1–128 ASCII letters, digits, _ or -")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or not 0 <= timestamp <= 999_999_999_999:
        raise SignatureError("webhook-timestamp must be a nonnegative integer Unix second")
    if not isinstance(body, bytes):
        raise SignatureError("the body must be the exact bytes sent")
    return f"{webhook_id}.{timestamp}.".encode("ascii") + body


def sign(key: bytes, webhook_id: str, timestamp: int, body: bytes) -> str:
    """One ``v1,<base64>`` signature over the exact body bytes."""
    digest = hmac.new(key, _content(webhook_id, timestamp, body), hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def signature_header(keys, webhook_id: str, timestamp: int, body: bytes) -> str:
    """The ``webhook-signature`` value: one signature per key, newest key first."""
    keys = list(keys)
    if not keys:
        raise SignatureError("at least one active signing secret is required")
    return " ".join(sign(key, webhook_id, timestamp, body) for key in keys)


def verify(keys, headers, body: bytes, *, now=None, tolerance=300) -> str:
    """Check Standard Webhooks headers for a receiver; return the webhook-id.

    Header names are matched case-insensitively. The timestamp must be within
    ``tolerance`` seconds of ``now`` in either direction, and at least one
    space-separated ``v1`` signature must match one of ``keys`` under a
    constant-time comparison. Other signature versions are ignored.
    """
    lowered = {str(name).lower(): value for name, value in dict(headers).items()}
    webhook_id = lowered.get("webhook-id", "")
    timestamp = lowered.get("webhook-timestamp", "")
    header = lowered.get("webhook-signature", "")
    if not (webhook_id and timestamp and header):
        raise SignatureError("webhook-id, webhook-timestamp and webhook-signature are required")
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})", timestamp):
        raise SignatureError("invalid webhook-timestamp")
    if abs((time.time() if now is None else now) - int(timestamp)) > tolerance:
        raise SignatureError("webhook-timestamp is outside the tolerance window")
    expected = [sign(key, webhook_id, int(timestamp), body).encode("ascii") for key in keys]
    for candidate in header.split(" "):
        encoded = candidate.encode("utf-8", errors="replace")
        # Compare every expected value; never stop at the first secret checked.
        if any([hmac.compare_digest(encoded, value) for value in expected]):
            return webhook_id
    raise SignatureError("no matching v1 signature")

"""A deliberately small, versioned HMAC signing contract."""

import hashlib
import hmac
import re
import time


class AuthError(ValueError):
    """The signature or timestamp is invalid."""


def sign(secret: bytes, timestamp: int, delivery_id: str, body: bytes) -> str:
    """Sign the exact bytes: ASCII(timestamp) + '.' + ASCII(id) + '.' + body."""
    message = f"{timestamp}.{delivery_id}.".encode("ascii") + body
    return "sha256=" + hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify(secret: bytes, timestamp: str, signature: str, delivery_id: str,
           body: bytes, *, now: float | None = None, tolerance: int = 300) -> None:
    """Verify a canonical timestamp, bounded clock skew, and constant-time MAC."""
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})", timestamp):
        raise AuthError("invalid timestamp")
    if abs((time.time() if now is None else now) - int(timestamp)) > tolerance:
        raise AuthError("timestamp outside acceptance window")
    if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        raise AuthError("invalid signature")
    try:
        expected = sign(secret, int(timestamp), delivery_id, body)
    except (UnicodeError, ValueError) as exc:
        raise AuthError("invalid delivery identifier") from exc
    if not hmac.compare_digest(expected, signature):
        raise AuthError("invalid signature")

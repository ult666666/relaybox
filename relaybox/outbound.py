"""Outbound subscriptions, messages and fenced delivery leases (schema 2).

Deliveries reuse the inbox's lease state machine (``store.LeaseQueue``) in
their own table. The HTTP attempt itself lives in ``dispatch.py``.
"""

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import random
import re
import secrets
import time
from urllib.parse import urlsplit

from . import standard_webhooks
from .store import (STATES, Conflict, InvalidTransition, LeaseQueue, ValidationError, _job_id,
                    _number, parse_payload)

MAX_ACTIVE_SUBSCRIPTIONS = 100
MAX_EVENT_TYPES = 32
MAX_ACTIVE_SECRETS = 5
MAX_GRACE_SECONDS = 30 * 86_400
EVENT_TYPE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
# Public synthetic key for local demos only, like the inbound --demo-secret.
DEMO_SECRET = standard_webhooks.encode_secret(b"relaybox-synthetic-outbound-demo-key")


def validate_event_type(value, *, wildcard=False):
    """Full-stop delimited ``[A-Za-z0-9_]`` segments, as the spec recommends."""
    if wildcard and value == "*":
        return
    if not isinstance(value, str) or len(value) > 128 or not EVENT_TYPE.fullmatch(value):
        raise ValidationError("event type must be 1–128 characters of [A-Za-z0-9_] segments separated by '.'")


def validate_webhook_id(value):
    if not isinstance(value, str) or not standard_webhooks.WEBHOOK_ID.fullmatch(value):
        raise ValidationError("webhook ID must contain 1–128 ASCII letters, digits, _ or -")


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path: str
    loopback: bool


def parse_target(url, *, allow_remote=False) -> Target:
    """Validate a subscription URL. Non-loopback targets need allow_remote and HTTPS.

    Only literal loopback addresses (127.0.0.0/8, ::1) count as loopback, so no
    DNS lookup can redirect a default-mode delivery to another host.
    """
    if not isinstance(url, str) or len(url) > 2048 or not url.isascii() or any(ch <= " " or ch == "\x7f" for ch in url):
        raise ValidationError("endpoint URL must be at most 2048 printable ASCII characters without spaces")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationError("endpoint URL must be an absolute http:// or https:// URL")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ValidationError("endpoint URL must not contain credentials or a fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValidationError("endpoint URL has an invalid port") from exc
    if port == 0:
        raise ValidationError("endpoint URL has an invalid port")
    port = port or (443 if parts.scheme == "https" else 80)
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        loopback = False
    else:
        # Python versions disagree on IPv4-mapped IPv6 loopback; decide it here.
        loopback = (getattr(address, "ipv4_mapped", None) or address).is_loopback
    if not loopback:
        if not allow_remote:
            raise ValidationError("non-loopback endpoint refused; use a literal loopback address such as 127.0.0.1, or pass --allow-remote")
        if parts.scheme != "https":
            raise ValidationError("non-loopback endpoints must use https://")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return Target(parts.scheme, parts.hostname, port, path, loopback)


def retry_delay(attempts, base_delay, max_delay, rng, retry_after=None):
    """Full-jitter exponential backoff: uniform in [0, min(max_delay, base × 2^(attempts−1))].

    A receiver's Retry-After raises the delay to at least that many seconds,
    capped at max_delay. See https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
    """
    ceiling = min(max_delay, base_delay * 2 ** (attempts - 1))
    delay = rng.uniform(0, ceiling)
    if retry_after is not None:
        delay = max(delay, min(retry_after, max_delay))
    return delay


@dataclass(frozen=True)
class Outcome:
    """The result of one attempt: success, retry, permanent or gone (HTTP 410)."""

    kind: str
    status: int | None = None
    error: str | None = None
    retry_after: float | None = None

    def __post_init__(self):
        if self.kind not in ("success", "retry", "permanent", "gone"):
            raise ValidationError("unknown delivery outcome")
        if (self.kind == "success") != (self.error is None):
            raise ValidationError("failed outcomes need an error and successes must not have one")
        if self.error is not None and (not isinstance(self.error, str) or not self.error.strip() or len(self.error) > 500):
            raise ValidationError("error must contain 1–500 characters")
        if self.retry_after is not None:
            _number(self.retry_after, 0, 10 ** 9, "retry_after")


class Outbox(LeaseQueue):
    """Subscriptions, idempotent fan-out and leased outbound deliveries.

    Policy values are copied into each delivery at fan-out, like inbox jobs.
    The default schedule suits a local demo; production schedules span days.
    """

    table = "deliveries"

    def __init__(self, path, *, clock=time.time, max_attempts=6, base_delay=2, max_delay=300, rng=None):
        super().__init__(path, clock=clock, max_attempts=max_attempts, base_delay=base_delay, max_delay=max_delay)
        # SystemRandom: forked dispatchers never share a jitter sequence.
        self.rng = random.SystemRandom() if rng is None else rng

    # Subscriptions -------------------------------------------------------

    @staticmethod
    def _subscription(conn, row, now):
        secrets_rows = conn.execute("""SELECT id,secret,created_at,expires_at FROM subscription_secrets
            WHERE subscription_id=? ORDER BY id DESC""", (row["id"],)).fetchall()
        result = dict(row)
        result["event_types"] = json.loads(row["event_types"])
        # Fingerprints identify a secret for rotation; values are never listed.
        result["secrets"] = [{"id": item["id"], "fingerprint": hashlib.sha256(item["secret"]).hexdigest()[:16],
                              "created_at": item["created_at"], "expires_at": item["expires_at"],
                              "active": item["expires_at"] is None or item["expires_at"] > now}
                             for item in secrets_rows]
        return result

    def _secret(self, secret):
        if secret is None:
            secret = standard_webhooks.generate_secret()
        try:
            return secret, standard_webhooks.decode_secret(secret)
        except standard_webhooks.SignatureError as exc:
            raise ValidationError(str(exc)) from exc

    def subscribe(self, url, event_types, *, secret=None, allow_remote=False) -> tuple[dict, str]:
        """Create an active subscription; return it and its secret (shown once)."""
        parse_target(url, allow_remote=allow_remote)
        if isinstance(event_types, str) or not isinstance(event_types, (list, tuple)) or not 1 <= len(event_types) <= MAX_EVENT_TYPES:
            raise ValidationError(f"give 1–{MAX_EVENT_TYPES} event types")
        for event_type in event_types:
            validate_event_type(event_type, wildcard=True)
        types = json.dumps(sorted(set(event_types)), separators=(",", ":"))
        secret, key = self._secret(secret)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            if conn.execute("SELECT COUNT(*) FROM subscriptions WHERE state='active'").fetchone()[0] >= MAX_ACTIVE_SUBSCRIPTIONS:
                raise ValidationError(f"at most {MAX_ACTIVE_SUBSCRIPTIONS} active subscriptions")
            cursor = conn.execute("""INSERT INTO subscriptions (url,event_types,state,created_at,updated_at)
                VALUES (?,?,'active',?,?)""", (url, types, now, now))
            conn.execute("INSERT INTO subscription_secrets (subscription_id,secret,created_at) VALUES (?,?,?)",
                         (cursor.lastrowid, key, now))
            row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (cursor.lastrowid,)).fetchone()
            result = self._subscription(conn, row, now)
            conn.commit()
        return result, secret

    def subscriptions(self) -> list[dict]:
        with self._connection() as conn:
            now = self.clock()
            rows = conn.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
            return [self._subscription(conn, row, now) for row in rows]

    def rotate_secret(self, subscription_id, *, secret=None, grace_seconds=86_400) -> tuple[dict, str]:
        """Add a new current secret. Older ones keep signing for grace_seconds.

        While both are active, every delivery carries one signature per secret,
        so receivers can switch keys with no failed deliveries. Grace 0 retires
        older secrets at once (for example after a leak).
        """
        _job_id(subscription_id)
        _number(grace_seconds, 0, MAX_GRACE_SECONDS, "grace_seconds")
        secret, key = self._secret(secret)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if row is None:
                raise KeyError(subscription_id)
            conn.execute("DELETE FROM subscription_secrets WHERE subscription_id=? AND expires_at<=?", (subscription_id, now))
            conn.execute("""UPDATE subscription_secrets SET expires_at=? WHERE subscription_id=?
                AND (expires_at IS NULL OR expires_at>?)""", (now + grace_seconds, subscription_id, now + grace_seconds))
            active = conn.execute("""SELECT COUNT(*) FROM subscription_secrets WHERE subscription_id=?
                AND expires_at>?""", (subscription_id, now)).fetchone()[0]
            if active + 1 > MAX_ACTIVE_SECRETS:
                raise ValidationError(f"at most {MAX_ACTIVE_SECRETS} active secrets; rotate with a shorter grace period")
            conn.execute("INSERT INTO subscription_secrets (subscription_id,secret,created_at) VALUES (?,?,?)",
                         (subscription_id, key, now))
            conn.execute("UPDATE subscriptions SET updated_at=? WHERE id=?", (now, subscription_id))
            result = self._subscription(conn, conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone(), now)
            conn.commit()
        return result, secret

    def set_subscription_state(self, subscription_id, *, active, reason=None) -> dict:
        """Enable or disable a subscription. Disabled ones get no new deliveries."""
        _job_id(subscription_id)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if row is None:
                raise KeyError(subscription_id)
            if active and row["state"] != "active":
                count = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE state='active'").fetchone()[0]
                if count >= MAX_ACTIVE_SUBSCRIPTIONS:
                    raise ValidationError(f"at most {MAX_ACTIVE_SUBSCRIPTIONS} active subscriptions")
            conn.execute("UPDATE subscriptions SET state=?,disabled_reason=?,updated_at=? WHERE id=?",
                         ("active" if active else "disabled", None if active else (reason or "disabled by operator"), now, subscription_id))
            result = self._subscription(conn, conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone(), now)
            conn.commit()
        return result

    # Messages ------------------------------------------------------------

    def publish(self, event_type, body: bytes, *, webhook_id=None) -> tuple[dict, bool]:
        """Store one message and queue one delivery per matching active subscription.

        The webhook-id is the idempotency key: publishing the same ID with the
        same type and bytes returns the existing message without new
        deliveries; different content raises Conflict. Subscriptions created
        later do not receive earlier messages.
        """
        validate_event_type(event_type)
        parse_payload(body)
        if webhook_id is None:
            webhook_id = "msg_" + secrets.token_hex(16)
        validate_webhook_id(webhook_id)
        digest = hashlib.sha256(body).hexdigest()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            existing = conn.execute("SELECT * FROM messages WHERE webhook_id=?", (webhook_id,)).fetchone()
            if existing is not None:
                if (existing["event_type"], existing["digest"]) != (event_type, digest):
                    raise Conflict("webhook ID already exists with a different event type or payload digest")
                count = conn.execute("SELECT COUNT(*) FROM deliveries WHERE message_id=?", (existing["id"],)).fetchone()[0]
                conn.commit()
                return self._message(existing, count), False
            cursor = conn.execute("INSERT INTO messages (webhook_id,event_type,digest,body,created_at) VALUES (?,?,?,?,?)",
                                  (webhook_id, event_type, digest, body, now))
            matched = [row["id"] for row in conn.execute("SELECT id,event_types FROM subscriptions WHERE state='active' ORDER BY id")
                       if {"*", event_type} & set(json.loads(row["event_types"]))]
            conn.executemany("""INSERT INTO deliveries (message_id,subscription_id,state,max_attempts,base_delay,
                max_delay,available_at,created_at,updated_at) VALUES (?,?,'queued',?,?,?,?,?,?)""",
                [(cursor.lastrowid, subscription, self.max_attempts, self.base_delay, self.max_delay, now, now, now)
                 for subscription in matched])
            row = conn.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone()
            conn.commit()
            return self._message(row, len(matched)), True

    @staticmethod
    def _message(row, deliveries):
        return {"id": row["id"], "webhook_id": row["webhook_id"], "event_type": row["event_type"],
                "digest": row["digest"], "created_at": row["created_at"], "deliveries": deliveries}

    def relay(self, job) -> tuple[dict, bool]:
        """Publish a stored inbound job's exact bytes, once per inbound delivery.

        The event type is the payload's top-level ``type`` field (the spec's
        payload structure). The webhook-id is derived from the delivery ID and
        body digest, so relaying the same job again creates nothing new.
        """
        with self._connection() as conn:
            row = conn.execute("SELECT delivery_id,digest,body FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if row is None or (row["delivery_id"], row["digest"]) != (job["delivery_id"], job["digest"]):
            raise ValidationError("relay needs a stored inbound job from this database")
        event_type = parse_payload(row["body"]).get("type")
        validate_event_type(event_type)
        identity = row["delivery_id"].encode("utf-8") + b"\0" + row["digest"].encode("ascii")
        return self.publish(event_type, row["body"], webhook_id="msg_" + hashlib.sha256(identity).hexdigest()[:32])

    # Deliveries ----------------------------------------------------------

    _SUMMARY = """SELECT d.id,d.state,d.attempts,d.max_attempts,d.base_delay,d.max_delay,d.available_at,
        d.created_at,d.updated_at,d.lease_until,d.worker,d.last_error,d.last_status,d.replay_count,
        d.subscription_id,s.url,s.state AS subscription_state,m.webhook_id,m.event_type,m.digest
        FROM deliveries d JOIN messages m ON m.id=d.message_id JOIN subscriptions s ON s.id=d.subscription_id"""

    def _claimed(self, conn, row_id, now):
        row = conn.execute("""SELECT d.*,m.webhook_id,m.event_type,m.body,s.url,s.state AS subscription_state
            FROM deliveries d JOIN messages m ON m.id=d.message_id JOIN subscriptions s ON s.id=d.subscription_id
            WHERE d.id=?""", (row_id,)).fetchone()
        result = dict(row)
        # Newest first; only these keys sign the attempt being made now.
        result["secrets"] = [item["secret"] for item in conn.execute("""SELECT secret FROM subscription_secrets
            WHERE subscription_id=? AND (expires_at IS NULL OR expires_at>?) ORDER BY id DESC""",
            (row["subscription_id"], now))]
        return result

    def claim(self, worker: str, *, lease_seconds=60) -> dict | None:
        """Lease one ready delivery. The private result holds the body, token and secret keys."""
        return self._claim(worker, lease_seconds)

    def renew(self, delivery_id: int, token: str, *, lease_seconds=60) -> dict:
        return self._renew(delivery_id, token, lease_seconds)

    def complete(self, delivery_id: int, token: str, outcome: Outcome) -> str:
        """Record one attempt under its lease. Returns the new delivery state.

        Success acknowledges; retry schedules full-jitter backoff (raised to any
        Retry-After) or dead-letters at the attempt limit; permanent and gone
        dead-letter at once, and gone (HTTP 410) also disables the subscription
        in the same transaction.
        """
        if not isinstance(outcome, Outcome):
            raise ValidationError("outcome must be an Outcome")
        if outcome.kind == "success":
            return self._finish(delivery_id, token, None, columns={"last_status": outcome.status})

        def delay(row):
            return retry_delay(row["attempts"], row["base_delay"], row["max_delay"], self.rng, outcome.retry_after)

        def disable(conn, row, now):
            conn.execute("""UPDATE subscriptions SET state='disabled',disabled_reason=?,updated_at=?
                WHERE id=? AND state='active'""", ("receiver answered 410 Gone", now, row["subscription_id"]))

        return self._finish(delivery_id, token, outcome.error, retry=outcome.kind == "retry", delay=delay,
                            columns={"last_status": outcome.status}, after=disable if outcome.kind == "gone" else None)

    def replay(self, delivery_id: int) -> dict:
        """Requeue a dead letter with a fresh attempt budget and the same webhook-id."""
        def active(conn, row):
            state = conn.execute("SELECT state FROM subscriptions WHERE id=?", (row["subscription_id"],)).fetchone()[0]
            if state != "active":
                raise InvalidTransition("enable the subscription before replaying its deliveries")
        self._replay(delivery_id, check=active)
        return self.get(delivery_id)

    def get(self, delivery_id: int) -> dict:
        _job_id(delivery_id)
        with self._connection() as conn:
            row = conn.execute(self._SUMMARY + " WHERE d.id=?", (delivery_id,)).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return dict(row)

    def list_deliveries(self, *, state=None, limit=100) -> dict:
        """Latest summaries plus whole-table counts from one read transaction.

        Summaries never contain bodies, lease tokens or secrets.
        """
        if state is not None and state not in STATES:
            raise ValidationError("unknown delivery state")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("limit must be an integer between 1 and 500")
        with self._connection() as conn:
            conn.execute("BEGIN")
            rows = conn.execute(self._SUMMARY + " WHERE (? IS NULL OR d.state=?) ORDER BY d.id DESC LIMIT ?",
                                (state, state, limit)).fetchall()
            counts = dict.fromkeys(STATES, 0)
            counts.update({row["state"]: row["count"] for row in conn.execute("SELECT state,COUNT(*) AS count FROM deliveries GROUP BY state")})
            conn.commit()
        return {"deliveries": [dict(row) for row in rows], "counts": counts}

    def next_due(self) -> float | None:
        """Earliest time a queued delivery becomes ready or a lease expires."""
        with self._connection() as conn:
            return conn.execute("""SELECT MIN(CASE WHEN state='queued' THEN available_at ELSE lease_until END)
                FROM deliveries WHERE state IN ('queued','leased')""").fetchone()[0]


class RelayHandler:
    """A ``run_worker`` handler that fans an inbound job out to subscribers."""

    def __init__(self, outbox):
        if not isinstance(outbox, Outbox):
            raise ValidationError("RelayHandler needs an Outbox")
        self.outbox = outbox

    def __call__(self, job):
        self.outbox.relay(job)

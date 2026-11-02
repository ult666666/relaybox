"""SQLite state machine. Every operation owns and closes its connection."""

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import time

MAX_BODY = 65_536
STATES = ("queued", "leased", "succeeded", "dead_letter")
SCHEMA_VERSION = 2

# Schema 1 is the original inbound inbox. Schema 2 adds the outbound tables and
# changes nothing in schema 1, so opening a version-1 file upgrades it in place.
JOBS_SQL = """CREATE TABLE jobs (
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
)"""
JOBS_INDEX_SQL = "CREATE INDEX jobs_ready ON jobs(state, available_at, lease_until, id)"
SUBSCRIPTIONS_SQL = """CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    event_types TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','disabled')),
    disabled_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)"""
SECRETS_SQL = """CREATE TABLE subscription_secrets (
    id INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    secret BLOB NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL
)"""
MESSAGES_SQL = """CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    webhook_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    digest TEXT NOT NULL,
    body BLOB NOT NULL,
    created_at REAL NOT NULL
)"""
DELIVERIES_SQL = """CREATE TABLE deliveries (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
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
    last_status INTEGER,
    replay_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(message_id, subscription_id)
)"""
DELIVERIES_INDEX_SQL = "CREATE INDEX deliveries_ready ON deliveries(state, available_at, lease_until, id)"
SCHEMA = {
    1: (JOBS_SQL, JOBS_INDEX_SQL),
    2: (JOBS_SQL, JOBS_INDEX_SQL, SUBSCRIPTIONS_SQL, SECRETS_SQL, MESSAGES_SQL, DELIVERIES_SQL, DELIVERIES_INDEX_SQL),
}


class ValidationError(ValueError):
    """Input is outside the supported contract."""


class Conflict(ValueError):
    """A delivery identifier was already used for different bytes."""


class InvalidTransition(ValueError):
    """The job state, token, or lease does not permit this operation."""


def validate_delivery_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        raise ValidationError("delivery ID must contain 1–128 ASCII letters, digits, . _ : or -")


def parse_payload(body: bytes) -> dict:
    """Accept a bounded UTF-8 JSON object, rejecting ambiguous/nonfinite values."""
    if not isinstance(body, bytes) or len(body) > MAX_BODY:
        raise ValidationError(f"payload must be bytes, at most {MAX_BODY} bytes")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError("duplicate JSON object key")
            result[key] = value
        return result
    def constant(_):
        raise ValidationError("nonfinite JSON number")
    try:
        decoded = body.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=pairs, parse_constant=constant)
        # Reject unpaired surrogate escapes and overflowed numeric exponents too.
        def check(item):
            if isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValidationError("nonfinite JSON number")
            elif isinstance(item, dict):
                for key, child in item.items():
                    check(key)
                    check(child)
            elif isinstance(item, list):
                for child in item:
                    check(child)
        check(value)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("payload must be a valid finite UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise ValidationError("payload must be a JSON object")
    return value


def _number(value, minimum, maximum, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum or not math.isfinite(value):
        raise ValidationError(f"{name} must be between {minimum} and {maximum}")


def _job_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2 ** 63 - 1:
        raise ValidationError("job ID must be a positive signed 64-bit integer")


def _claim_options(worker, lease_seconds):
    if not isinstance(worker, str) or not worker.strip() or len(worker) > 128:
        raise ValidationError("worker must contain 1–128 characters")
    try:
        worker.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("invalid worker text") from exc
    _number(lease_seconds, 0.001, 3600, "lease_seconds")


def _exponential(row):
    return min(row["max_delay"], row["base_delay"] * 2 ** (row["attempts"] - 1))


class LeaseQueue:
    """Durable rows with at-least-once claims and fenced completion.

    One table per subclass (``jobs`` for the inbox, ``deliveries`` for outbound
    delivery) shares this state machine. Use a filesystem SQLite database.
    Fencing protects this database, not other systems.
    """

    table = ""  # A fixed subclass constant; never derived from input.

    def __init__(self, path, *, clock=time.time, max_attempts=3, base_delay=2, max_delay=60):
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 100:
            raise ValidationError("max_attempts must be an integer between 1 and 100")
        _number(base_delay, 0, 86_400, "base_delay")
        _number(max_delay, 0, 86_400, "max_delay")
        if str(path) == ":memory:":
            raise ValidationError("use a filesystem database; connections are operation scoped")
        self.path = str(Path(path))
        self.clock = clock
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # One transaction creates any missing schema-2 objects and the
            # version marker, so a concurrent opener never sees half of it.
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValidationError("unsupported database schema version")
            for statement in SCHEMA[SCHEMA_VERSION]:
                kind = "CREATE TABLE " if statement.startswith("CREATE TABLE ") else "CREATE INDEX "
                conn.execute(kind + "IF NOT EXISTS " + statement[len(kind):])
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _claimed(self, conn, row_id, now) -> dict:
        """Return the private claimed-row shape, inside the claim transaction."""
        raise NotImplementedError

    def stats(self) -> dict[str, int]:
        with self._connection() as conn:
            rows = conn.execute(f"SELECT state,COUNT(*) AS count FROM {self.table} GROUP BY state").fetchall()
        result = dict.fromkeys(STATES, 0)
        result.update({row["state"]: row["count"] for row in rows})
        return result

    def _claim(self, worker, lease_seconds):
        _claim_options(worker, lease_seconds)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            conn.execute(f"""UPDATE {self.table} SET state='dead_letter',last_error='lease expired at attempt limit',
                lease_until=NULL,lease_token=NULL,worker=NULL,updated_at=?
                WHERE state='leased' AND lease_until<=? AND attempts>=max_attempts""", (now, now))
            row = conn.execute(f"""SELECT id FROM {self.table} WHERE
                (state='queued' AND available_at<=?) OR
                (state='leased' AND lease_until<=? AND attempts<max_attempts)
                ORDER BY available_at,id LIMIT 1""", (now, now)).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(f"""UPDATE {self.table} SET state='leased',attempts=attempts+1,lease_until=?,
                lease_token=?,worker=?,updated_at=? WHERE id=?""",
                (now + lease_seconds, secrets.token_hex(32), worker, now, row["id"]))
            claimed = self._claimed(conn, row["id"], now)
            conn.commit()
            return claimed

    def _renew(self, row_id, token, lease_seconds):
        _job_id(row_id)
        _number(lease_seconds, 0.001, 3600, "lease_seconds")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = conn.execute(f"SELECT * FROM {self.table} WHERE id=?", (row_id,)).fetchone()
            if row is None:
                raise KeyError(row_id)
            if row["state"] != "leased" or row["lease_token"] != token or row["lease_until"] <= now:
                raise InvalidTransition("a current unexpired lease token is required")
            conn.execute(f"UPDATE {self.table} SET lease_until=?,updated_at=? WHERE id=?",
                         (max(row["lease_until"], now + lease_seconds), now, row_id))
            renewed = self._claimed(conn, row_id, now)
            conn.commit()
            return renewed

    def _finish(self, row_id, token, error, *, retry=True, delay=_exponential, columns=None, after=None):
        """Complete a current lease: success, retry after delay(row), or dead letter.

        columns adds fixed, code-defined column values; after(conn, row, now)
        runs further writes in the same transaction. Returns the new state.
        """
        _job_id(row_id)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = conn.execute(f"SELECT * FROM {self.table} WHERE id=?", (row_id,)).fetchone()
            if row is None:
                raise KeyError(row_id)
            if row["state"] != "leased" or row["lease_token"] != token or row["lease_until"] <= now:
                raise InvalidTransition("a current unexpired lease token is required")
            state = "succeeded"
            available = row["available_at"]
            if error is not None:
                state = "dead_letter" if not retry or row["attempts"] >= row["max_attempts"] else "queued"
                available = now + delay(row)
            extra = dict(columns or {})
            assignments = "".join(f",{name}=?" for name in extra)
            conn.execute(f"""UPDATE {self.table} SET state=?,available_at=?,updated_at=?,last_error=?,
                lease_token=NULL,lease_until=NULL,worker=NULL{assignments} WHERE id=?""",
                (state, available, now, error, *extra.values(), row_id))
            if after is not None:
                after(conn, row, now)
            conn.commit()
            return state

    def _replay(self, row_id, check=None, columns=None):
        """Reset a dead letter to queued. columns adds fixed, code-defined column values."""
        _job_id(row_id)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = conn.execute(f"SELECT * FROM {self.table} WHERE id=?", (row_id,)).fetchone()
            if row is None:
                raise KeyError(row_id)
            if row["state"] != "dead_letter":
                raise InvalidTransition("only dead-letter jobs can be replayed")
            if check is not None:
                check(conn, row)
            extra = dict(columns or {})
            assignments = "".join(f",{name}=?" for name in extra)
            conn.execute(f"""UPDATE {self.table} SET state='queued',attempts=0,available_at=?,updated_at=?,
                last_error=NULL,lease_token=NULL,lease_until=NULL,worker=NULL,replay_count=replay_count+1{assignments}
                WHERE id=?""", (now, now, *extra.values(), row_id))
            result = self._claimed(conn, row_id, now)
            conn.commit()
            return result


class Inbox(LeaseQueue):
    """Durable inbox with at-least-once claims and fenced completion.

    Use a filesystem SQLite database. Workers must make their own external side
    effects idempotent; fencing protects this database, not other systems.
    """

    table = "jobs"

    @staticmethod
    def _job(row, *, summary=False):
        result = dict(row)
        body = result.pop("body")
        if summary:
            result.pop("lease_token")
        else:
            result["payload"] = json.loads(body)
        return result

    def _claimed(self, conn, row_id, now):
        return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (row_id,)).fetchone())

    def ingest(self, delivery_id: str, body: bytes) -> tuple[dict, bool]:
        validate_delivery_id(delivery_id)
        parse_payload(body)
        digest = hashlib.sha256(body).hexdigest()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            existing = conn.execute("SELECT * FROM jobs WHERE delivery_id=?", (delivery_id,)).fetchone()
            if existing is not None:
                if existing["digest"] != digest:
                    raise Conflict("delivery ID already exists with a different payload digest")
                conn.commit()
                return self._job(existing), False
            cursor = conn.execute("""INSERT INTO jobs
                (delivery_id,digest,body,state,max_attempts,base_delay,max_delay,available_at,created_at,updated_at)
                VALUES (?,?,?,'queued',?,?,?,?,?,?)""",
                (delivery_id, digest, body, self.max_attempts, self.base_delay, self.max_delay, now, now, now))
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (cursor.lastrowid,)).fetchone()
            conn.commit()
            return self._job(row), True

    def get(self, job_id: int) -> dict:
        _job_id(job_id)
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    def list_jobs(self, *, state=None, limit=100) -> list[dict]:
        if state is not None and state not in STATES:
            raise ValidationError("unknown job state")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("limit must be an integer between 1 and 500")
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE (? IS NULL OR state=?) ORDER BY id DESC LIMIT ?", (state, state, limit)).fetchall()
        return [self._job(row, summary=True) for row in rows]

    def metrics(self) -> dict:
        """One read-only queue snapshot at one validated supplied-clock sample.

        Counts are persisted states; expired leases are not reconciled here.
        This aggregate scans rows without selecting payload/private columns.
        """
        now = self.clock()
        _number(now, 0, 253402300799, "snapshot_at")
        with self._connection() as conn:
            row = conn.execute("""SELECT
                COUNT(CASE WHEN state='queued' THEN 1 END) AS queued,
                COUNT(CASE WHEN state='leased' THEN 1 END) AS leased,
                COUNT(CASE WHEN state='succeeded' THEN 1 END) AS succeeded,
                COUNT(CASE WHEN state='dead_letter' THEN 1 END) AS dead_letter,
                COUNT(CASE WHEN state='queued' AND available_at<=:now THEN 1 END) AS queued_ready,
                COUNT(CASE WHEN state='queued' AND available_at>:now THEN 1 END) AS queued_delayed,
                COUNT(CASE WHEN state='leased' AND lease_until>:now THEN 1 END) AS leases_active,
                COUNT(CASE WHEN state='leased' AND lease_until<=:now AND attempts<max_attempts THEN 1 END) AS leases_expired_reclaimable,
                COUNT(CASE WHEN state='leased' AND lease_until<=:now AND attempts>=max_attempts THEN 1 END) AS leases_expired_exhausted,
                MAX(CASE WHEN state='queued' AND available_at<=:now THEN :now-available_at END) AS oldest_queued_ready_wait_seconds,
                MIN(CASE WHEN state='queued' AND available_at>:now THEN available_at END) AS next_retry_at,
                MIN(CASE WHEN state='leased' AND lease_until>:now THEN lease_until END) AS next_lease_expiry
                FROM jobs""", {"now": now}).fetchone()
        result = dict(row)
        counts = {state: result.pop(state) for state in STATES}
        # The response format is version 1; it does not track the database schema.
        return {"schema_version": 1, "snapshot_at": now, "counts": counts, **result}

    def claim(self, worker: str, *, lease_seconds=30) -> dict | None:
        return self._claim(worker, lease_seconds)

    def renew(self, job_id: int, token: str, *, lease_seconds=30) -> dict:
        """Extend current unexpired ownership without changing its token/attempt."""
        return self._renew(job_id, token, lease_seconds)

    def ack(self, job_id: int, token: str) -> None:
        self._finish(job_id, token, None)

    def fail(self, job_id: int, token: str, error: str) -> None:
        if not isinstance(error, str) or not error.strip() or len(error) > 500:
            raise ValidationError("error must contain 1–500 characters")
        try:
            error.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("invalid error text") from exc
        self._finish(job_id, token, error)

    def replay(self, job_id: int) -> dict:
        return self._replay(job_id)

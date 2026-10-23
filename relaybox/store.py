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


class Inbox:
    """Durable inbox with at-least-once claims and fenced completion.

    Use a filesystem SQLite database. Workers must make their own external side
    effects idempotent; fencing protects this database, not other systems.
    """

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
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValidationError("unsupported database schema version")
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
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
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, available_at, lease_until, id)")
            conn.execute("PRAGMA user_version=1")

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _job(row, *, summary=False):
        result = dict(row)
        body = result.pop("body")
        if summary:
            result.pop("lease_token")
        else:
            result["payload"] = json.loads(body)
        return result

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

    def stats(self) -> dict[str, int]:
        with self._connection() as conn:
            rows = conn.execute("SELECT state,COUNT(*) AS count FROM jobs GROUP BY state").fetchall()
        result = dict.fromkeys(STATES, 0)
        result.update({row["state"]: row["count"] for row in rows})
        return result

    def claim(self, worker: str, *, lease_seconds=30) -> dict | None:
        _claim_options(worker, lease_seconds)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            conn.execute("""UPDATE jobs SET state='dead_letter',last_error='lease expired at attempt limit',
                lease_until=NULL,lease_token=NULL,worker=NULL,updated_at=?
                WHERE state='leased' AND lease_until<=? AND attempts>=max_attempts""", (now, now))
            row = conn.execute("""SELECT id FROM jobs WHERE
                (state='queued' AND available_at<=?) OR
                (state='leased' AND lease_until<=? AND attempts<max_attempts)
                ORDER BY available_at,id LIMIT 1""", (now, now)).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute("""UPDATE jobs SET state='leased',attempts=attempts+1,lease_until=?,
                lease_token=?,worker=?,updated_at=? WHERE id=?""",
                (now + lease_seconds, secrets.token_hex(32), worker, now, row["id"]))
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
            return self._job(job)

    def renew(self, job_id: int, token: str, *, lease_seconds=30) -> dict:
        """Extend current unexpired ownership without changing its token/attempt."""
        _job_id(job_id)
        _number(lease_seconds, 0.001, 3600, "lease_seconds")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["state"] != "leased" or row["lease_token"] != token or row["lease_until"] <= now:
                raise InvalidTransition("a current unexpired lease token is required")
            conn.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE id=?",
                         (max(row["lease_until"], now + lease_seconds), now, job_id))
            renewed = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            conn.commit()
            return self._job(renewed)

    def _complete(self, job_id: int, token: str, error: str | None):
        _job_id(job_id)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["state"] != "leased" or row["lease_token"] != token or row["lease_until"] <= now:
                raise InvalidTransition("a current unexpired lease token is required")
            state = "succeeded"
            available = row["available_at"]
            if error is not None:
                state = "dead_letter" if row["attempts"] >= row["max_attempts"] else "queued"
                available = now + min(row["max_delay"], row["base_delay"] * 2 ** (row["attempts"] - 1))
            conn.execute("""UPDATE jobs SET state=?,available_at=?,updated_at=?,last_error=?,
                lease_token=NULL,lease_until=NULL,worker=NULL WHERE id=?""", (state, available, now, error, job_id))
            conn.commit()

    def ack(self, job_id: int, token: str) -> None:
        self._complete(job_id, token, None)

    def fail(self, job_id: int, token: str, error: str) -> None:
        if not isinstance(error, str) or not error.strip() or len(error) > 500:
            raise ValidationError("error must contain 1–500 characters")
        try:
            error.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("invalid error text") from exc
        self._complete(job_id, token, error)

    def replay(self, job_id: int) -> dict:
        _job_id(job_id)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["state"] != "dead_letter":
                raise InvalidTransition("only dead-letter jobs can be replayed")
            conn.execute("""UPDATE jobs SET state='queued',attempts=0,available_at=?,updated_at=?,
                last_error=NULL,lease_token=NULL,lease_until=NULL,worker=NULL,replay_count=replay_count+1
                WHERE id=?""", (now, now, job_id))
            result = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            conn.commit()
            return self._job(result)

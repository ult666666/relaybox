"""Bounded online snapshots for trusted local RelayBox schema-1 and schema-2 databases.

This is an operator recovery tool, not a parser sandbox for hostile SQLite
files. Directories and files must not be concurrently replaced or edited.
"""

from contextlib import closing, contextmanager
from functools import lru_cache
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

from .store import SCHEMA, STATES, ValidationError

MAX_DATABASE_BYTES = 1_073_741_824
# The schema-1 jobs columns, pinned independently of the DDL text in store.py.
_JOBS_COLUMNS = (
    ("id", "INTEGER", 0, None, 1, 0),
    ("delivery_id", "TEXT", 1, None, 0, 0),
    ("digest", "TEXT", 1, None, 0, 0),
    ("body", "BLOB", 1, None, 0, 0),
    ("state", "TEXT", 1, None, 0, 0),
    ("attempts", "INTEGER", 1, "0", 0, 0),
    ("max_attempts", "INTEGER", 1, None, 0, 0),
    ("base_delay", "REAL", 1, None, 0, 0),
    ("max_delay", "REAL", 1, None, 0, 0),
    ("available_at", "REAL", 1, None, 0, 0),
    ("created_at", "REAL", 1, None, 0, 0),
    ("updated_at", "REAL", 1, None, 0, 0),
    ("lease_until", "REAL", 0, None, 0, 0),
    ("lease_token", "TEXT", 0, None, 0, 0),
    ("worker", "TEXT", 0, None, 0, 0),
    ("last_error", "TEXT", 0, None, 0, 0),
    ("replay_count", "INTEGER", 1, "0", 0, 0),
)


@lru_cache(maxsize=None)
def _expected(version):
    """Objects and columns for a known version, built from RelayBox's own DDL.

    The statements come from store.SCHEMA and run in a private in-memory
    database. SQL read from a source file is never executed.
    """
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in SCHEMA[version]:
            reference.execute(statement)
        objects = tuple((kind, name, table, _normalized(sql)) for kind, name, table, sql in
                        reference.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name"))
        columns = {name: tuple(tuple(row[1:]) for row in reference.execute(f"PRAGMA table_xinfo({name})"))
                   for kind, name, _, _ in objects if kind == "table"}
    if columns["jobs"] != _JOBS_COLUMNS:
        raise ValidationError("RelayBox's own jobs schema differs from the pinned schema-1 columns")
    return objects, columns


class BackupTimeout(ValidationError):
    """The shared snapshot/validation deadline expired."""


class _Deadline:
    def __init__(self, timeout):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0.1 <= timeout <= 60 or not math.isfinite(timeout)):
            raise ValidationError("backup timeout must be finite and between 0.1 and 60 seconds")
        self.end = time.monotonic() + timeout

    def remaining(self):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise BackupTimeout("backup/verification timeout expired")
        return remaining

    def query_progress(self):
        return int(time.monotonic() >= self.end)

    def backup_progress(self, status, remaining, total):
        self.remaining()  # Raising cancels Connection.backup, including busy retries.
        if total > MAX_DATABASE_BYTES // 512:
            raise ValidationError("database exceeds the 1 GiB backup limit")


def _selected(path):
    try:
        selected = Path(os.path.abspath(path))
        symlink = selected.is_symlink()
        canonical = selected.parent.resolve(strict=True) / selected.name
    except (TypeError, ValueError) as exc:
        raise ValidationError("database path must be a local filesystem path") from exc
    if symlink:
        raise ValidationError("selected database path must not be a symlink")
    return canonical


def _source(path):
    selected = _selected(path)
    try:
        details = selected.lstat()
    except FileNotFoundError as exc:
        raise ValidationError("source database must already exist") from exc
    if not stat.S_ISREG(details.st_mode):
        raise ValidationError("source database must be a regular file")
    if details.st_size > MAX_DATABASE_BYTES:
        raise ValidationError("database exceeds the 1 GiB backup limit")
    return selected


def _no_sidecars(path):
    # A stale hot journal/WAL belongs to a different database lifetime. Never
    # delete it or publish/verify a supposedly standalone file beside it.
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(str(path) + suffix).lstat()
        except FileNotFoundError:
            continue
        raise ValidationError("standalone snapshot/destination must not have pre-existing SQLite sidecars")


def _destination(path):
    selected = _selected(path)
    if not selected.parent.is_dir():
        raise ValidationError("destination parent must be an existing local directory")
    try:
        selected.lstat()
    except FileNotFoundError:
        _no_sidecars(selected)
        return selected
    raise ValidationError("destination must not exist (including self, hard links and symlinks)")


def _execute(conn, sql, deadline):
    # Busy waits and VM work share the same monotonic deadline. SQLite timeout
    # granularity is milliseconds, so allow at most one millisecond rounding.
    conn.execute(f"PRAGMA busy_timeout={max(1, math.ceil(deadline.remaining() * 1000))}")
    try:
        result = conn.execute(sql)
    except sqlite3.Error:
        deadline.remaining()
        raise
    deadline.remaining()
    return result


@contextmanager
def _reader(path, deadline):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                           timeout=deadline.remaining(), isolation_level=None)
    try:
        conn.set_progress_handler(deadline.query_progress, 1000)
        _execute(conn, "PRAGMA query_only=ON", deadline)
        _execute(conn, "PRAGMA trusted_schema=OFF", deadline)
        _execute(conn, "BEGIN", deadline)
        yield conn
    except sqlite3.Error:
        deadline.remaining()
        raise
    finally:
        conn.close()


def _normalized(sql):
    return " ".join(sql.split()) if isinstance(sql, str) else sql


def _state_counts(conn, table, deadline):
    counts = dict.fromkeys(STATES, 0)
    for state, count in _execute(conn, f"SELECT state,COUNT(*) FROM {table} GROUP BY state LIMIT 5", deadline):
        deadline.remaining()
        if state not in counts:
            raise ValidationError("database contains an unrecognized job state")
        counts[state] = count
    return counts


def _metadata(conn, deadline):
    version = _execute(conn, "PRAGMA user_version", deadline).fetchone()[0]
    if version not in (1, 2):
        raise ValidationError("unsupported RelayBox database schema version; expected 1 or 2")
    objects, tables = _expected(version)
    schema = _execute(conn, f"SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name LIMIT {len(objects) + 1}", deadline).fetchall()
    if tuple((kind, name, table, _normalized(sql)) for kind, name, table, sql in schema) != objects:
        raise ValidationError(f"unrecognized RelayBox schema; only the original schema-{version} tables and indexes are supported")
    for table, expected in tables.items():
        columns = _execute(conn, f"PRAGMA table_xinfo({table})", deadline).fetchmany(len(expected) + 1)
        if tuple(tuple(row[1:]) for row in columns) != expected:
            raise ValidationError(f"unrecognized RelayBox {table} structure")
    pages = _execute(conn, "PRAGMA page_count", deadline).fetchone()[0]
    page_size = _execute(conn, "PRAGMA page_size", deadline).fetchone()[0]
    if pages * page_size > MAX_DATABASE_BYTES:
        raise ValidationError("database exceeds the 1 GiB backup limit")
    integrity = _execute(conn, "PRAGMA integrity_check", deadline).fetchmany(2)
    if integrity != [("ok",)]:
        # SQLite diagnostic text may contain file data; don't place it in receipts.
        raise ValidationError("database integrity check failed")
    counts = _state_counts(conn, "jobs", deadline)
    result = {"schema_version": version, "integrity": "ok", "jobs": sum(counts.values()), "counts": counts}
    if version == 2:
        if _execute(conn, "PRAGMA foreign_key_check", deadline).fetchmany(1):
            raise ValidationError("database foreign key check failed")
        deliveries = _state_counts(conn, "deliveries", deadline)
        result["outbound"] = {
            "subscriptions": _execute(conn, "SELECT COUNT(*) FROM subscriptions", deadline).fetchone()[0],
            "messages": _execute(conn, "SELECT COUNT(*) FROM messages", deadline).fetchone()[0],
            "deliveries": sum(deliveries.values()), "counts": deliveries}
    return result


def _file_metadata(path, deadline):
    digest = hashlib.sha256()
    with path.open("rb") as snapshot:
        while block := snapshot.read(1024 * 1024):
            deadline.remaining()
            digest.update(block)
    deadline.remaining()
    return {"sha256": digest.hexdigest(), "size_bytes": path.stat().st_size}


def _standalone_header(path, deadline):
    # mode=ro can still create WAL/SHM files. Check the fixed-size SQLite
    # header before connecting: offsets 18/19 are 1 for rollback mode, 2 for
    # WAL. This is verifier-only; live backup must continue to read real WAL.
    deadline.remaining()
    with path.open("rb") as source:
        header = source.read(100)
    deadline.remaining()
    if len(header) != 100 or header[:16] != b"SQLite format 3\0":
        raise ValidationError("snapshot must have a complete SQLite database header")
    if header[18:20] != bytes((1, 1)):
        raise ValidationError("verify-backup requires a standalone DELETE-journal snapshot; back up a live inbox first")


def verify_backup(path, *, timeout=10) -> dict:
    """Read and validate a trusted, quiescent schema-1 snapshot without writes.

    Hashes the physical file. Do not modify it or attach live WAL writers during
    verification; use backup_database to snapshot a live inbox first.
    """
    deadline = _Deadline(timeout)
    source = _source(path)
    _no_sidecars(source)
    _standalone_header(source, deadline)
    with _reader(source, deadline) as conn:
        metadata = _metadata(conn, deadline)
        # A standalone backup has no uncheckpointed state outside this file.
        if _execute(conn, "PRAGMA journal_mode", deadline).fetchone()[0] != "delete":
            raise ValidationError("verify-backup requires a standalone DELETE-journal snapshot; back up a live inbox first")
        metadata.update(_file_metadata(source, deadline))
    return {"path": str(source), **metadata}


def backup_database(source, destination, *, timeout=10) -> dict:
    """Publish a verified online snapshot to a new path, never overwriting.

    Requests mode 0600. Requires trusted local directories and atomic hard-link
    support; the deadline cannot interrupt an operating-system I/O syscall.
    """
    deadline = _Deadline(timeout)
    source = _source(source)
    destination = _destination(destination)
    temporary = None
    try:
        with _reader(source, deadline) as reader:
            original = _metadata(reader, deadline)
            descriptor, name = tempfile.mkstemp(prefix=".relaybox-backup-", dir=destination.parent)
            temporary = Path(name)
            os.close(descriptor)
            target = sqlite3.connect(temporary, timeout=deadline.remaining(), isolation_level=None)
            try:
                target.set_progress_handler(deadline.query_progress, 1000)
                reader.backup(target, pages=64, progress=deadline.backup_progress, sleep=0.01)
                deadline.remaining()
                mode = _execute(target, "PRAGMA journal_mode=DELETE", deadline).fetchone()[0]
                if mode != "delete":
                    raise ValidationError("could not finalize standalone snapshot")
            except sqlite3.Error:
                deadline.remaining()
                raise
            finally:
                target.close()
        with _reader(temporary, deadline) as snapshot:
            metadata = _metadata(snapshot, deadline)
        if metadata != original:
            raise ValidationError("snapshot metadata differs from the source read transaction")
        metadata.update(_file_metadata(temporary, deadline))
        with temporary.open("rb") as snapshot:
            os.fsync(snapshot.fileno())
        deadline.remaining()
        _no_sidecars(destination)
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ValidationError("destination appeared during publication; nothing was overwritten") from exc
        return {"source": str(source), "destination": str(destination), **metadata}
    finally:
        if temporary is not None:
            # Ordinary failures never leave private temp/SQLite sidecar files.
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(temporary) + suffix).unlink(missing_ok=True)

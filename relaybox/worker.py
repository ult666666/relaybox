"""Bounded local processing and a demonstration idempotent receipt effect."""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .store import InvalidTransition, ValidationError, _claim_options


def run_worker(inbox, handler, *, worker="local-worker", max_jobs=1, lease_seconds=30) -> dict:
    """Process at most max_jobs claims, stopping immediately if none is ready.

    Handlers receive a claimed job and own side-effect idempotency and any lease
    renewal. Only fenced completion failures are converted to lost_leases;
    storage failures propagate. Interrupts leave the lease for later recovery.
    """
    if not callable(handler):
        raise ValidationError("handler must be callable")
    if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or not 1 <= max_jobs <= 1000:
        raise ValidationError("max_jobs must be an integer between 1 and 1000")
    _claim_options(worker, lease_seconds)
    counts = {"claimed": 0, "succeeded": 0, "failed": 0, "lost_leases": 0}
    for _ in range(max_jobs):
        job = inbox.claim(worker, lease_seconds=lease_seconds)
        if job is None:
            break
        counts["claimed"] += 1
        # Retain the capability before passing a mutable job to user code.
        job_id, token = job["id"], job["lease_token"]
        try:
            handler(job)
        except Exception as exc:
            try:
                error = f"{type(exc).__name__}: {exc}"
            except Exception:
                error = f"{type(exc).__name__}: exception text unavailable"
            error = error.encode("utf-8", errors="replace").decode("utf-8")[:500]
            try:
                inbox.fail(job_id, token, error)
            except InvalidTransition:
                counts["lost_leases"] += 1
            else:
                counts["failed"] += 1
        else:
            try:
                inbox.ack(job_id, token)
            except InvalidTransition:
                counts["lost_leases"] += 1
            else:
                counts["succeeded"] += 1
    return counts


class ReceiptHandler:
    """Publish version-1 canonical JSON receipts in a trusted local directory.

    Newly created directories use 0700 and files use 0600 (subject to umask).
    The selected directory and its parents must be trusted and must not be
    concurrently renamed/replaced by another actor. Trusted ancestor aliases
    are canonicalized; a symlink selected directory or receipt is rejected.
    The filesystem must support atomic hard links.
    """

    def __init__(self, path):
        selected = Path(os.path.abspath(path))
        if selected.is_symlink():
            raise ValidationError("receipt directory must not be a symlink")
        self.path = selected.parent.resolve() / selected.name
        self._directory()

    def _directory(self):
        # Ancestors were canonicalized at selection. Recheck the anchored path
        # before each effect to reject later replacement by a symlink or file.
        for component in [*reversed(self.path.parents), self.path]:
            try:
                mode = component.lstat().st_mode
            except FileNotFoundError:
                try:
                    component.mkdir(mode=0o700)
                except FileExistsError:
                    pass  # Another local receipt handler may create it first.
                mode = component.lstat().st_mode
            if not stat.S_ISDIR(mode):
                raise ValidationError("receipt directory and its parents must be real directories, not symlinks")

    @staticmethod
    def _existing(path, content):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(mode):
            raise ValidationError("existing receipt must be a regular file, not a symlink")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as receipt:
            if not stat.S_ISREG(os.fstat(receipt.fileno()).st_mode):
                raise ValidationError("existing receipt must be a regular file")
            if receipt.read(len(content) + 1) != content:
                raise ValidationError("existing receipt conflicts with the canonical delivery receipt")
        return True

    def __call__(self, job):
        self._directory()
        # Neither the raw delivery ID nor a database-local job ID becomes a path.
        identity = job["delivery_id"].encode("utf-8") + b"\0" + job["digest"].encode("ascii")
        final = self.path / (hashlib.sha256(identity).hexdigest() + ".json")
        content = (json.dumps({"version": 1, "delivery_id": job["delivery_id"],
                               "digest": job["digest"], "payload": job["payload"]},
                              sort_keys=True, ensure_ascii=True, allow_nan=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        if self._existing(final, content):
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".relaybox-", dir=self.path, delete=False) as receipt:
                temporary = Path(receipt.name)
                receipt.write(content)
                receipt.flush()
                os.fsync(receipt.fileno())
            try:
                # Hard-link publication is atomic and cannot replace any file.
                os.link(temporary, final)
            except FileExistsError:
                if not self._existing(final, content):
                    raise OSError("receipt disappeared during atomic publication")
        finally:
            if temporary is not None:
                temporary.unlink()

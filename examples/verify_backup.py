"""Verify an installed 0.3.0 recovery drill outside the source checkout.

Only synthetic local files are used. The original worker is stopped before the
snapshot and stays stopped: a restored database cannot fence that original.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    executable = Path(sys.executable).with_name("relaybox.exe" if os.name == "nt" else "relaybox")
    if not executable.is_file():
        raise RuntimeError("install RelayBox into this interpreter's environment first")
    with tempfile.TemporaryDirectory(prefix="relaybox-recovery-") as directory:
        root = Path(directory).resolve()
        original, backup, restored = (root / name for name in ["original.sqlite3", "备份 snapshot.sqlite3", "恢复 inbox.sqlite3"])
        receipts = root / "retained receipts"
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

        def isolated(code, *args):
            result = subprocess.run([sys.executable, "-I", "-c", code, *map(str, args)],
                                    cwd=root, env=env, text=True, capture_output=True, timeout=15)
            assert result.returncode == 0, (result.stdout, result.stderr)
            return json.loads(result.stdout)

        setup = isolated('''
import importlib.metadata, json, sys
from pathlib import Path
import relaybox
from relaybox.store import Inbox
from relaybox.worker import ReceiptHandler
assert relaybox.__version__ == importlib.metadata.version("relaybox-inbox") == "0.3.0"
assert "site-packages" in str(Path(relaybox.__file__).resolve())
inbox = Inbox(sys.argv[1], clock=lambda: 1000)
inbox.ingest("installed-recovery-001", b'{"synthetic":true,"journey":"recovery"}')
job = inbox.claim("stopped-original-worker", lease_seconds=30)
ReceiptHandler(sys.argv[2])(job)  # Effect exists; crash before acknowledgment.
print(json.dumps({"version": relaybox.__version__, "package": relaybox.__file__}))
''', original, receipts)
        files = list(receipts.glob("*.json"))
        assert len(files) == 1
        retained = files[0]
        receipt_bytes, receipt_mtime = retained.read_bytes(), retained.stat().st_mtime_ns

        def cli(database, *args, expected=0):
            result = subprocess.run([str(executable), "--db", str(database), *map(str, args)],
                                    cwd=root, env=env, text=True, capture_output=True, timeout=15)
            assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
            assert "lease_token" not in result.stdout
            return json.loads(result.stderr if expected == 2 else result.stdout)

        snapshot = cli(original, "backup", backup)
        assert snapshot["counts"] == {"queued": 0, "leased": 1, "succeeded": 0, "dead_letter": 0}
        assert snapshot["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
        verified = cli(restored, "verify-backup", backup)
        assert not restored.exists()  # Verification cannot initialize --db.
        assert verified["sha256"] == snapshot["sha256"]
        recovery = cli(restored, "restore", backup)
        assert recovery["counts"] == snapshot["counts"]
        assert not Path(str(backup) + "-wal").exists()
        assert not Path(str(backup) + "-shm").exists()
        cli(restored, "restore", backup, expected=2)
        comparison = isolated('''
from contextlib import closing
import json, sqlite3, sys
from relaybox.store import Conflict, Inbox
rows = []
for path in sys.argv[1:]:
    with closing(sqlite3.connect("file:" + path + "?mode=ro", uri=True)) as conn:
        rows.append(conn.execute("SELECT * FROM jobs ORDER BY id").fetchall())
assert rows[0] == rows[1]
inbox = Inbox(sys.argv[2])
_, created = inbox.ingest("installed-recovery-001", b'{"synthetic":true,"journey":"recovery"}')
assert not created
try:
    inbox.ingest("installed-recovery-001", b'{"different":true}')
except Conflict:
    pass
else:
    raise AssertionError("changed duplicate accepted")
print(json.dumps({"all_columns_preserved": True, "duplicate_created": created}))
''', original, restored)
        summary = cli(restored, "work", "--receipts", receipts, "--max-jobs", "1")
        assert summary == {"claimed": 1, "succeeded": 1, "failed": 0, "lost_leases": 0}
        assert cli(restored, "show", "1")["job"]["attempts"] == 2
        assert cli(restored, "show", "1")["job"]["state"] == "succeeded"
        assert cli(original, "show", "1")["job"]["state"] == "leased"
        assert retained.read_bytes() == receipt_bytes
        assert retained.stat().st_mtime_ns == receipt_mtime
        assert len(list(receipts.glob("*.json"))) == 1
        print(json.dumps({"installed_backup": "ok", "version": setup["version"],
                          "snapshot_jobs": snapshot["jobs"], "verified_sha256": True,
                          **comparison, "summary": summary, "receipt_reused_without_rewrite": True,
                          "original_state": "leased", "restored_state": "succeeded",
                          "outside_checkout": True}))


if __name__ == "__main__":
    main()

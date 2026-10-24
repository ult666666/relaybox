"""Source-only synthetic fixture; imports the installed package with Python -I.

Only known fixture actions arrive on stdin. No product control route is added.
"""

from contextlib import closing
import importlib.metadata
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import threading

import relaybox
from relaybox.server import make_server
from relaybox.store import Inbox


def main():
    package = Path(relaybox.__file__).resolve()
    assert "site-packages" in package.parts, package
    assert relaybox.__version__ == importlib.metadata.version("relaybox-inbox")
    with tempfile.TemporaryDirectory(prefix="relaybox-browser-db-") as directory:
        # 23 October 2026, explicit supplied clock; not a wall-clock latency claim.
        baseline = 1792785600.25
        now = [baseline]
        inbox = Inbox(Path(directory) / "inbox.sqlite3", clock=lambda: now[0])

        def mixed():
            now[0] = baseline
            with closing(sqlite3.connect(inbox.path, isolation_level=None)) as conn:
                conn.execute("DELETE FROM jobs")
            specs = [
                ("ready-old", "queued", -4.625, None, 0, 3),
                ("ready-exact", "queued", 0, None, 0, 2),
                ("retry-first", "queued", 40, None, 1, 5),
                ("retry-later", "queued", 100, None, 1, 7),
                ("active", "leased", -50, 10, 1, 3),
                ("expired-exact", "leased", -50, 0, 1, 5),
                ("expired-final", "leased", -50, -1, 2, 2),
                ("done", "succeeded", -50, None, 1, 3),
                ("dead", "dead_letter", -50, None, 3, 3),
            ]
            for delivery, state, available, expiry, attempts, budget in specs:
                body = json.dumps({"synthetic": True, "row": delivery,
                                   "text": '<img src=x onerror="window.pwned=1">',
                                   "note_zh": "仅供本地测试"}).encode()
                job, _ = inbox.ingest(delivery, body)
                with closing(sqlite3.connect(inbox.path, isolation_level=None)) as conn:
                    conn.execute("""UPDATE jobs SET state=?,available_at=?,lease_until=?,attempts=?,max_attempts=?,
                        lease_token='synthetic-private-token',worker='synthetic-worker',last_error='<img src=x>' WHERE id=?""",
                        (state, baseline + available, None if expiry is None else baseline + expiry,
                         attempts, budget, job["id"]))

        mixed()
        server = make_server(inbox, b"synthetic-browser-fixture-secret", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            thread.start()
            print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                              "version": relaybox.__version__, "package": str(package),
                              "python": platform.python_version(), "clock": baseline}), flush=True)
            for line in sys.stdin:
                command = json.loads(line)
                if command == {"mixed": True}:
                    mixed()
                elif command == {"empty": True}:
                    with closing(sqlite3.connect(inbox.path, isolation_level=None)) as conn:
                        conn.execute("DELETE FROM jobs")
                elif set(command) == {"advance"} and command["advance"] == 100:
                    now[0] = baseline + 100
                else:
                    raise ValueError("Unknown synthetic fixture action")
                print(json.dumps({"ok": True}), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive(), "Fixture server did not stop"


if __name__ == "__main__":
    main()

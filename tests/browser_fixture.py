"""Dev-only logical-clock HTTP fixture controlled over stdin, never a product route."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

from relaybox.server import make_server
from relaybox.store import Inbox


def main():
    with tempfile.TemporaryDirectory(prefix="relaybox-browser-") as directory:
        now = [1000.0]
        inbox = Inbox(Path(directory) / "inbox.sqlite3", clock=lambda: now[0])

        def mixed():
            now[0] = 1000.0
            with closing(sqlite3.connect(inbox.path, isolation_level=None)) as conn:
                conn.execute("DELETE FROM jobs")
            specs = [
                ("ready-old", "queued", 900, None, 0, 3),
                ("ready-exact", "queued", 1000, None, 0, 2),
                ("retry-first", "queued", 1040, None, 1, 5),
                ("retry-later", "queued", 1100, None, 1, 7),
                ("active", "leased", 950, 1010, 1, 3),
                ("expired-exact", "leased", 950, 1000, 1, 5),
                ("expired-final", "leased", 950, 999, 2, 2),
                ("done", "succeeded", 950, None, 1, 3),
                ("dead", "dead_letter", 950, None, 3, 3),
            ]
            for delivery, state, available, expiry, attempts, budget in specs:
                body = json.dumps({"row": delivery, "text": '<img src=x onerror="window.pwned=1">'}).encode()
                job, _ = inbox.ingest(delivery, body)
                with closing(sqlite3.connect(inbox.path, isolation_level=None)) as conn:
                    conn.execute("""UPDATE jobs SET state=?,available_at=?,lease_until=?,attempts=?,max_attempts=?,
                        lease_token='browser-private-token',worker='browser-worker',last_error='<img src=x>' WHERE id=?""",
                        (state, available, expiry, attempts, budget, job["id"]))

        mixed()
        server = make_server(inbox, b"synthetic-browser-fixture-secret", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}), flush=True)
        try:
            for line in sys.stdin:
                command = json.loads(line)
                if command.get("mixed"):
                    mixed()
                if "clock" in command:
                    now[0] = command["clock"]
                if command.get("empty"):
                    with closing(sqlite3.connect(inbox.path, isolation_level=None)) as conn:
                        conn.execute("DELETE FROM jobs")
                print(json.dumps({"ok": True}), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()

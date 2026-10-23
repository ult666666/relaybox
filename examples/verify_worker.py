"""Verify installed CLI ingestion -> local receipt -> acknowledgment.

Run with a Python environment where `pip install .` has installed `relaybox`.
Children run outside the checkout; only synthetic loopback requests are sent.
"""

import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def main():
    executable = Path(sys.executable).with_name("relaybox.exe" if os.name == "nt" else "relaybox")
    if not executable.is_file():
        raise RuntimeError("install RelayBox into this interpreter's environment first")
    with tempfile.TemporaryDirectory(prefix="relaybox-worker-") as directory:
        root = Path(directory).resolve()
        database, receipts = root / "inbox.sqlite3", root / "receipts"
        command = [str(executable), "--db", str(database)]
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

        def cli(*args, expected=0):
            result = subprocess.run(command + list(args), cwd=root, env=env,
                                    text=True, capture_output=True, timeout=10)
            assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
            return json.loads(result.stdout if expected != 2 else result.stderr)

        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request(method, path, body, headers or {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        cli("init")
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        server = subprocess.Popen(command + ["serve", "--port", str(port), "--demo-secret"],
                                  cwd=root, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise AssertionError(server.communicate())
                try:
                    assert request("GET", "/healthz")[0] == 200
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise AssertionError("installed server did not become ready")

            body = b'{"synthetic":true,"message":"installed worker journey"}'
            payload = root / "payload.json"
            payload.write_bytes(body)
            headers = cli("sign", "installed-worker-001", str(payload), "--demo-secret")
            statuses = []
            job_id = None
            for expected in [202, 200]:
                status, response = request("POST", "/webhooks", body, headers)
                assert status == expected, (status, response)
                statuses.append(status)
                job_id = json.loads(response)["job"]["id"]
            summary = cli("work", "--receipts", str(receipts), "--max-jobs", "8")
            assert summary == {"claimed": 1, "succeeded": 1, "failed": 0, "lost_leases": 0}
            assert cli("show", str(job_id))["job"]["state"] == "succeeded"
            files = list(receipts.iterdir())
            assert len(files) == 1
            receipt = json.loads(files[0].read_bytes())
            assert receipt == {"version": 1, "delivery_id": "installed-worker-001",
                               "digest": hashlib.sha256(body).hexdigest(), "payload": json.loads(body)}
            assert "lease_token" not in receipt
            assert cli("work", "--receipts", str(receipts))["claimed"] == 0
            cli("work", "--receipts", str(receipts), "--max-jobs", "0", expected=2)
            print(json.dumps({"installed_worker": "ok", "ingest_statuses": statuses,
                              "summary": summary, "receipt_files": 1, "final_state": "succeeded",
                              "outside_checkout": True}))
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            server.communicate()


if __name__ == "__main__":
    main()

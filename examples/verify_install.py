"""Verify the installed package from outside its source directory.

Run with the interpreter into which `pip install .` installed RelayBox.
Uses only synthetic data, an ephemeral database, and loopback HTTP.
"""

import hashlib
import hmac
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def main():
    with tempfile.TemporaryDirectory(prefix="relaybox-install-") as directory:
        database = str(Path(directory) / "demo.sqlite3")
        command = [sys.executable, "-m", "relaybox", "--db", database]
        demo = subprocess.run(command + ["demo"], cwd=directory, text=True, capture_output=True, check=True)
        assert json.loads(demo.stdout)["counts"] == {"queued": 1, "leased": 1, "succeeded": 1, "dead_letter": 1}
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        server = subprocess.Popen(command + ["serve", "--port", str(port), "--demo-secret"], cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise AssertionError(server.communicate())
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    connection.request("GET", "/healthz")
                    response = connection.getresponse()
                    assert response.status == 200
                    response.read()
                    connection.close()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise AssertionError("installed server failed to start")
            for path, expected in [("/", b"RelayBox"), ("/assets/app.js", b"textContent"), ("/assets/style.css", b"color-scheme")]:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", path)
                response = connection.getresponse()
                assert response.status == 200
                assert expected in response.read(), path
                connection.close()
            body = b'{"synthetic":true,"message":"installed smoke test"}'
            timestamp = str(int(time.time()))
            delivery = "installed-001"
            signature = "sha256=" + hmac.new(b"relaybox-synthetic-demo-secret-only", f"{timestamp}.{delivery}.".encode() + body, hashlib.sha256).hexdigest()
            headers = {"Content-Type": "application/json", "X-RelayBox-ID": delivery,
                       "X-RelayBox-Timestamp": timestamp, "X-RelayBox-Signature": signature}
            statuses = []
            for expected in [202, 200]:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("POST", "/webhooks", body, headers)
                response = connection.getresponse()
                assert response.status == expected
                assert json.loads(response.read())["created"] == (expected == 202)
                statuses.append(response.status)
                connection.close()
            print(json.dumps({"installed_cli": "ok", "demo_states": 4, "dashboard_assets": 3,
                              "ingest_statuses": statuses, "source_directory_import": False}))
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

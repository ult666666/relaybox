"""Verify the installed package from outside its source directory.

Run with the interpreter into which `pip install .` installed RelayBox.
Uses only synthetic data, an ephemeral database, and loopback HTTP.
"""

import base64
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
        probe = subprocess.run([sys.executable, "-I", "-c", """
import base64, importlib.metadata, json
from importlib.resources import files
from pathlib import Path
import relaybox
assert relaybox.__version__ == importlib.metadata.version("relaybox-inbox") == "0.4.1"
assert "site-packages" in str(Path(relaybox.__file__).resolve())
assets = {route: base64.b64encode(files("relaybox").joinpath("assets", name).read_bytes()).decode()
          for route, name in [("/", "index.html"), ("/assets/app.js", "app.js"), ("/assets/style.css", "style.css")]}
print(json.dumps({"version": relaybox.__version__, "assets": assets}))
"""], cwd=directory, text=True, capture_output=True, check=True, timeout=15)
        installed = json.loads(probe.stdout)
        command = [sys.executable, "-I", "-m", "relaybox", "--db", database]
        demo = subprocess.run(command + ["demo"], cwd=directory, text=True, capture_output=True, check=True, timeout=15)
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
            for path, encoded in installed["assets"].items():
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", path)
                response = connection.getresponse()
                assert response.status == 200
                assert response.read() == base64.b64decode(encoded), path
                connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/api/metrics")
            response = connection.getresponse()
            assert response.status == 200
            metrics = json.loads(response.read())
            connection.close()
            assert set(metrics) == {"schema_version", "snapshot_at", "counts", "queued_ready", "queued_delayed", "leases_active", "leases_expired_reclaimable", "leases_expired_exhausted", "oldest_queued_ready_wait_seconds", "next_retry_at", "next_lease_expiry"}
            assert metrics["schema_version"] == 1 and 0 <= metrics["snapshot_at"] <= 253402300799
            assert metrics["counts"] == {"queued": 1, "leased": 1, "succeeded": 1, "dead_letter": 1}
            assert (metrics["queued_ready"], metrics["queued_delayed"], metrics["leases_active"], metrics["leases_expired_reclaimable"], metrics["leases_expired_exhausted"]) == (1, 0, 1, 0, 0)
            assert metrics["oldest_queued_ready_wait_seconds"] >= 0
            assert metrics["next_retry_at"] is None and metrics["next_lease_expiry"] > metrics["snapshot_at"]
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
                              "ingest_statuses": statuses, "source_directory_import": False,
                              "version": installed["version"], "metrics_http": True, "asset_bytes_equal": True}))
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

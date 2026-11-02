"""Verify installed outbound delivery: subscribe -> publish -> retry -> dead letter -> replay.

Run with a Python environment where `pip install .` has installed RelayBox.
Children run outside the checkout. The receiver is examples/receive_webhooks.py,
which checks every Standard Webhooks signature with its own code. Only
synthetic loopback requests are sent.
"""

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
    receiver_script = Path(__file__).with_name("receive_webhooks.py").resolve()
    with tempfile.TemporaryDirectory(prefix="relaybox-outbound-") as directory:
        root = Path(directory).resolve()
        database = root / "outbound.sqlite3"
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

        def cli(*args, expected=0):
            result = subprocess.run([str(executable), "--db", str(database), *map(str, args)], cwd=root, env=env,
                                    text=True, capture_output=True, timeout=60)
            assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
            assert "lease_token" not in result.stdout
            return json.loads(result.stderr if expected == 2 else result.stdout)

        receiver = subprocess.Popen([sys.executable, "-I", str(receiver_script), "--port", "0", "--demo-secret",
                                     "--fail-first", "1", "--reject-path", "/reject"],
                                    cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            base = json.loads(receiver.stdout.readline())["listening"]
            installed = subprocess.run([sys.executable, "-I", "-c", "import relaybox, pathlib; print(relaybox.__version__, pathlib.Path(relaybox.__file__).resolve())"],
                                       cwd=root, env=env, text=True, capture_output=True, check=True, timeout=15).stdout.split()
            assert installed[0] == "0.5.0" and "site-packages" in installed[1], installed
            secret = cli("subscription", "add", f"{base}/accept", "--event-type", "example.created", "--demo-secret")["secret"]
            cli("subscription", "add", f"{base}/reject", "--event-type", "example.created", "--secret", secret)
            assert secret[len("whsec_"):] not in json.dumps(cli("subscription", "list"))  # Fingerprints only.
            refused = cli("subscription", "add", "https://hooks.example.com/in", "--event-type", "x", expected=2)
            assert "non-loopback" in refused["error"], refused
            payload = root / "payload.json"
            payload.write_bytes(b'{"synthetic":true,"type":"example.created","message":"installed outbound journey"}')
            message = cli("publish", "example.created", payload)["message"]
            assert message["deliveries"] == 2
            # /accept answers 503 (Retry-After: 1) once, then 204; /reject always answers 422.
            summary = cli("deliver", "--max-deliveries", "10", "--wait", "15", "--timeout", "5", "--lease-seconds", "10", expected=1)
            assert summary == {"claimed": 3, "succeeded": 1, "retried": 1, "dead_lettered": 1, "lost_leases": 0,
                               "awaiting_retry": 0}, summary
            states = {row["url"].rsplit("/", 1)[1]: row for row in cli("deliveries")["deliveries"]}
            assert (states["accept"]["state"], states["accept"]["attempts"], states["accept"]["last_status"]) == ("succeeded", 2, 204)
            assert (states["reject"]["state"], states["reject"]["last_status"]) == ("dead_letter", 422)
            replayed = cli("replay-delivery", states["reject"]["id"])["delivery"]
            assert (replayed["state"], replayed["replay_count"], replayed["webhook_id"]) == ("queued", 1, message["webhook_id"])
            again = cli("deliver", "--timeout", "5", "--lease-seconds", "10", expected=1)
            assert again["dead_lettered"] == 1, again
        finally:
            receiver.terminate()
            output, errors = receiver.communicate(timeout=10)
        requests = [json.loads(line) for line in output.splitlines()]  # After the "listening" line.
        assert errors == "", errors
        assert [(item["path"], item["status"]) for item in requests if item["path"] == "/accept"] == [("/accept", 503), ("/accept", 204)]
        assert [item["status"] for item in requests if item["path"] == "/reject"] == [422, 422]
        assert all(item["verified"] for item in requests), requests
        assert {item["webhook_id"] for item in requests} == {message["webhook_id"]}
        assert not any(item["duplicate"] for item in requests), requests
        print(json.dumps({"installed_outbound": "ok", "version": installed[0], "requests": len(requests),
                          "all_signatures_verified": True, "same_webhook_id_on_every_attempt": True,
                          "summary": summary, "replayed_dead_letter": True, "outside_checkout": True}))


if __name__ == "__main__":
    main()

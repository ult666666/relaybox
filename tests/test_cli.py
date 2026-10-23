import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "inbox.sqlite3"

    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "relaybox", "--db", str(self.db), *args], capture_output=True, text=True, timeout=10, env=env)

    def test_help_and_init(self):
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0)
        result = self.run_cli("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["queued"], 0)
        self.assertTrue(self.db.is_file())

    def test_demo_populates_four_states_without_credentials(self):
        result = self.run_cli("demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"], {"queued": 1, "leased": 1, "succeeded": 1, "dead_letter": 1})
        duplicate = self.run_cli("demo")
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("empty", duplicate.stderr)
        stats = self.run_cli("stats")
        self.assertEqual(json.loads(stats.stdout)["counts"]["succeeded"], 1)

    def test_claim_ack_and_replay_commands(self):
        self.assertEqual(self.run_cli("demo").returncode, 0)
        result = self.run_cli("claim", "--worker", "cli-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads(result.stdout)["job"]
        self.assertEqual(job["delivery_id"], "demo-004")
        ack = self.run_cli("ack", str(job["id"]), "--token", job["lease_token"])
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertEqual(self.run_cli("ack", str(job["id"]), "--token", job["lease_token"]).returncode, 2)
        replay = self.run_cli("replay", "3")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout)["job"]["replay_count"], 1)

    def test_fail_command_enforces_lease(self):
        self.run_cli("demo")
        job = json.loads(self.run_cli("claim", "--worker", "cli-test").stdout)["job"]
        failed = self.run_cli("fail", str(job["id"]), "--token", job["lease_token"], "--error", "synthetic failure")
        self.assertEqual(failed.returncode, 0, failed.stderr)
        fetched = json.loads(self.run_cli("show", str(job["id"])).stdout)["job"]
        self.assertEqual(fetched["state"], "queued")
        self.assertEqual(fetched["last_error"], "synthetic failure")

    def test_sign_command_covers_supplied_file_bytes(self):
        payload = Path(self.tmp.name) / "payload.json"
        payload.write_bytes(b'{"hello":"world"}')
        result = self.run_cli("sign", "evt-test", str(payload), "--timestamp", "1000", "--demo-secret")
        self.assertEqual(result.returncode, 0, result.stderr)
        from relaybox.auth import verify
        from relaybox.cli import DEMO_SECRET
        headers = json.loads(result.stdout)
        verify(DEMO_SECRET, headers["X-RelayBox-Timestamp"], headers["X-RelayBox-Signature"], "evt-test", payload.read_bytes(), now=1000)

    def test_serve_requires_secret_and_rejects_bad_port(self):
        clean = {key: value for key, value in os.environ.items() if key != "RELAYBOX_SECRET"}
        result = self.run_cli("serve", env=clean)
        self.assertEqual(result.returncode, 2)
        self.assertIn("RELAYBOX_SECRET", result.stderr)
        self.assertEqual(self.run_cli("serve", "--port", "99999", "--demo-secret").returncode, 2)

    def test_unknown_job_and_invalid_worker_are_cli_errors(self):
        self.assertEqual(self.run_cli("show", "999").returncode, 2)
        self.assertEqual(self.run_cli("claim", "--worker", "").returncode, 2)

    def test_out_of_range_job_ids_do_not_print_tracebacks(self):
        for job_id in ["0", "-1", str(2 ** 100)]:
            result = self.run_cli("show", job_id)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("error", json.loads(result.stderr))

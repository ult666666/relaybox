"""Behavior/oracle checks for the source-only diagnostic measurement tool."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from relaybox.store import Inbox

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "benchmark_metrics.py"


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        if not SCRIPT.is_file():
            self.fail("the source-only metrics measurement tool is absent")
        spec = importlib.util.spec_from_file_location("relaybox_benchmark", SCRIPT)
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)

    def test_nearest_rank_uses_actual_samples_without_interpolation(self):
        summary = self.tool.summarize([10, 50, 30, 20, 40])
        self.assertEqual(summary, {"samples_ns": [10, 50, 30, 20, 40], "p50_ns": 30, "p95_ns": 50, "max_ns": 50})
        self.assertEqual(self.tool.summarize(list(range(1, 21)))["p95_ns"], 19)
        self.assertEqual(self.tool.summarize([7] * 5)["p50_ns"], 7)
        for values in [[], [True] * 5, [1.5] * 5, [-1] * 5]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.tool.summarize(values)

    def test_bounds_and_duplicates_are_checked_before_creating_a_database(self):
        self.assertEqual(self.tool.options(["--sizes", "10", "200000", "--samples", "5", "--warmups", "1"]).sizes, [10, 200000])
        for args in [["--sizes", "0"], ["--sizes", "11"], ["--sizes", "200010"],
                     ["--sizes", "10", "10"], ["--sizes", "10", "20", "30", "40", "50"],
                     ["--samples", "4"], ["--samples", "201"], ["--warmups", "0"], ["--warmups", "21"]]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.tool.options(args)

    def test_ten_row_profile_has_independently_expected_metrics_and_list(self):
        with tempfile.TemporaryDirectory(prefix="relaybox benchmark 测量 ") as directory:
            inbox = self.tool.seed(Inbox, Path(directory) / "synthetic.sqlite3", 10)
            self.assertEqual(inbox.metrics(), {
                "schema_version": 1, "snapshot_at": 1000.0,
                "counts": {"queued": 4, "leased": 3, "succeeded": 2, "dead_letter": 1},
                "queued_ready": 2, "queued_delayed": 2, "leases_active": 1,
                "leases_expired_reclaimable": 1, "leases_expired_exhausted": 1,
                "oldest_queued_ready_wait_seconds": 100.0, "next_retry_at": 1040.0,
                "next_lease_expiry": 1010.0,
            })
            self.assertEqual([row["id"] for row in inbox.list_jobs(limit=100)], list(range(10, 0, -1)))
            self.tool.check(inbox, 10)

    def test_actual_measurement_checks_rows_and_raw_sample_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = self.tool.seed(Inbox, Path(directory) / "synthetic.sqlite3", 120)
            before = self.tool.database_digest(inbox.path)
            result = self.tool.measure(inbox, 120, samples=5, warmups=1, deadline=self.tool.time.monotonic() + 15)
            self.assertEqual(self.tool.database_digest(inbox.path), before)
            self.assertEqual(result["rows"], 120)
            self.assertEqual(result["latest_list_rows"], 100)
            for name in ["metrics", "latest_100"]:
                raw = result[name]["samples_ns"]
                self.assertEqual(len(raw), 5)
                self.assertTrue(all(type(value) is int and value >= 0 for value in raw))
                self.assertEqual(result[name], self.tool.summarize(raw))
            self.assertTrue(result["logical_database_unchanged"])

    def test_changed_state_and_expired_deadline_are_controlled_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = self.tool.seed(Inbox, Path(directory) / "synthetic.sqlite3", 10)
            with self.assertRaises(ValueError):
                self.tool.measure(inbox, 10, samples=5, warmups=1, deadline=0)
            inbox.replay(10)
            with self.assertRaises(ValueError):
                self.tool.check(inbox, 10)

    def test_source_checkout_is_not_accepted_as_an_installed_runtime(self):
        with self.assertRaises(ValueError):
            self.tool.installed_runtime()

    def test_unavailable_installation_reports_fixed_diagnostic_without_traceback(self):
        errors = io.StringIO()
        with patch.object(self.tool, "installed_runtime", side_effect=ModuleNotFoundError("private synthetic path")), redirect_stderr(errors):
            self.assertEqual(self.tool.main(["--sizes", "10", "--samples", "5", "--warmups", "1"]), 2)
        self.assertNotIn("private synthetic path", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    @unittest.skipUnless(os.environ.get("RELAYBOX_BENCH_PYTHON"), "set RELAYBOX_BENCH_PYTHON to an installed isolated interpreter")
    def test_real_installed_interpreter_outside_checkout_emits_path_free_receipt(self):
        with tempfile.TemporaryDirectory(prefix="relaybox actual 测量 ") as directory:
            result = subprocess.run([os.environ["RELAYBOX_BENCH_PYTHON"], "-I", str(SCRIPT), "--sizes", "10", "120",
                                     "--samples", "5", "--warmups", "1"], cwd=directory, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual([r["rows"] for r in receipt["measurements"]], [10, 120])
        self.assertEqual(receipt["runtime"]["package_version"], "0.4.0")
        self.assertTrue(receipt["synthetic"])
        self.assertIn("local", receipt["notice"])
        for private in [str(Path.home()), directory, "lease_token", "hostname", "PYTHONPATH"]:
            self.assertNotIn(private, result.stdout)

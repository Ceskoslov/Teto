import io
import json
import platform
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from evaluator.cli import main
from evaluator.runner import run_suite
from evaluator.suites import digest, read_suite
from evaluator.validation import InputValidationError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def suite_data(harness: bytes) -> dict:
    data = json.loads((EXAMPLES / "scripted-suite.json").read_text(encoding="utf-8"))
    data["harness"]["sha256"] = digest(harness)
    data["environment"].update(
        platform=sys.platform,
        machine=platform.machine(),
        python=platform.python_version(),
    )
    return data


def make_suite(data: dict):
    return read_suite(json.dumps(data))


@unittest.skipUnless(sys.platform == "linux", "Linux suite runner")
class SuiteRunnerTests(unittest.TestCase):
    def test_cli_checks_mode_and_output_before_starting_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "started"
            harness = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
            suite_path = root / "suite.json"
            script = root / "harness.py"
            suite_path.write_text(json.dumps(suite_data(harness)))
            script.write_bytes(harness)
            argv = [
                "run", "--input", str(suite_path), "--harness", str(script),
                "--artifact-id", "preflight",
            ]
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main([
                    *argv, "--require-mode", "live", "--output", str(root / "run.json"),
                ]), 2)
                self.assertEqual(main([
                    *argv, "--output", str(root / "missing" / "run.json"),
                ]), 2)
            self.assertIn("SUITE_MODE_MISMATCH", errors.getvalue())
            self.assertIn("EVALUATION_IO_FAILED", errors.getvalue())
            self.assertFalse(marker.exists())

    def test_cli_captures_trials_in_fresh_workspaces(self):
        harness = b"""
import json, os, sys
from pathlib import Path
request = json.load(sys.stdin)
assert request["model"]["revision"] == "1"
assert request["trial"] in (1, 2)
assert os.listdir() == ["input.txt"]
Path("result.txt").write_text(Path("input.txt").read_text().upper())
print('{"schema_version":1,"reported_success":true,"cost_microusd":0}')
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite_path = root / "suite.json"
            script = root / "harness.py"
            report = root / "run.json"
            suite_path.write_text(json.dumps(suite_data(harness)))
            script.write_bytes(harness)
            argv = [
                "run", "--input", str(suite_path), "--harness", str(script),
                "--artifact-id", "smoke", "--output", str(report),
            ]
            self.assertEqual(main(argv), 0)
            trials = json.loads(report.read_text())["trials"]
        self.assertEqual(
            [(row["case_id"], row["trial"], row["status"]) for row in trials],
            [
                ("basic", 1, "completed"), ("accented", 1, "completed"),
                ("basic", 2, "completed"), ("accented", 2, "completed"),
            ],
        )
        self.assertEqual(trials[1]["output_sha256"], digest("CAFÉ\n".encode()))
        self.assertEqual(trials[1]["output_sha256"], trials[3]["output_sha256"])

    def test_timeout_reserves_unknown_cost(self):
        harness = b"import time; time.sleep(30)"
        data = suite_data(harness)
        data["mode"] = "live"
        data["limits"] = {
            "trial_timeout_ms": 100, "trial_cost_microusd": 10,
            "total_cost_microusd": 10,
        }
        run = run_suite(make_suite(data), harness, "timeout")
        self.assertEqual(
            [row.status for row in run.trials],
            ["timed_out", "budget_exhausted", "budget_exhausted", "budget_exhausted"],
        )
        self.assertIsNone(run.trials[0].response)
        self.assertIsNone(run.trials[1].latency_ms)

    def test_actual_cost_returns_unused_reservation_and_overrun_stops(self):
        harness = b"""
import json, sys
request = json.load(sys.stdin)
cost = 2 if request["trial"] == 1 else 11
print(json.dumps({"schema_version":1,"reported_success":False,"cost_microusd":cost}))
"""
        data = suite_data(harness)
        data["limits"].update(trial_cost_microusd=10, total_cost_microusd=14)
        run = run_suite(make_suite(data), harness, "cost")
        self.assertEqual(
            [row.status for row in run.trials],
            ["completed", "completed", "cost_exceeded", "budget_exhausted"],
        )
        self.assertEqual(run.trials[2].response.cost_microusd, 11)

    def test_output_symlink_does_not_count_as_verified_evidence(self):
        harness = b"""
import os
os.symlink("input.txt", "result.txt")
print('{"schema_version":1,"reported_success":true,"cost_microusd":0}')
"""
        run = run_suite(make_suite(suite_data(harness)), harness, "symlink")
        self.assertEqual(run.trials[0].status, "output_unreadable")
        self.assertIsNone(run.trials[0].output_sha256)

    def test_digest_and_environment_pins_checked_before_running(self):
        harness = b"raise AssertionError"
        data = suite_data(harness)
        with self.assertRaises(InputValidationError) as error:
            run_suite(make_suite(data), harness + b"\n", "mismatch")
        self.assertEqual(error.exception.issues[0].code, "HARNESS_DIGEST_MISMATCH")
        data["environment"]["python"] = "1.0.0"
        with self.assertRaises(InputValidationError) as error:
            run_suite(make_suite(data), harness, "mismatch")
        self.assertEqual(error.exception.issues[0].code, "ENVIRONMENT_PIN_MISMATCH")

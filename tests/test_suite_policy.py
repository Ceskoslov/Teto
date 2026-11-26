import json
import tempfile
import unittest
from pathlib import Path

from evaluator.cli import main
from evaluator.suite_policy import gate_suite_run
from evaluator.validation import InputValidationError
from test_suite_scoring import SUITE_TEXT, evidence_for

POLICY_TEXT = (
    Path(__file__).resolve().parents[1] / "examples" / "scripted-policy.json"
).read_text(encoding="utf-8")


class SuitePolicyTests(unittest.TestCase):
    def test_cli_release_and_hold_with_observations(self):
        run = evidence_for(SUITE_TEXT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite_path, policy_path = root / "suite.json", root / "policy.json"
            evidence_path, output_path = root / "run.json", root / "gate.json"
            suite_path.write_text(SUITE_TEXT)
            policy_path.write_text(POLICY_TEXT)
            argv = [
                "gate", "--input", str(evidence_path), "--suite", str(suite_path),
                "--policy", str(policy_path), "--output", str(output_path),
            ]
            evidence_path.write_text(json.dumps(run))
            self.assertEqual(main(argv), 0)
            self.assertEqual(json.loads(output_path.read_text())["gates"][-1]["status"], "observed")

            run["trials"][0]["response"]["tool_calls"] = None
            run["trials"][0]["output_sha256"] = None
            evidence_path.write_text(json.dumps(run))
            self.assertEqual(main(argv), 1)
            hold = json.loads(output_path.read_text())
            self.assertEqual(hold["recommendation"]["failed_gates"], [
                "critical-quality", "critical-tools",
            ])
            self.assertEqual(hold["gates"][2]["reason"], "incomplete_critical_evidence")

    def test_calibrated_p95_threshold_and_sample_floors(self):
        run = evidence_for(SUITE_TEXT)
        policy = json.loads(POLICY_TEXT)
        gate = {
            "id": "latency",
            "kind": "calibrated",
            "metric": "latency_ms",
            "statistic": "p95",
            "operator": "at_most",
            "threshold": 20,
            "min_samples": 4,
            "min_cases": 2,
            "min_coverage_basis_points": 10000,
            "calibration": {"id": "synthetic-test", "evidence_sha256": "a" * 64},
        }
        policy["gates"] = [gate]

        def result() -> dict:
            return gate_suite_run(json.dumps(run), SUITE_TEXT, json.dumps(policy))["gates"][0]

        self.assertEqual(result()["status"], "pass")
        gate["threshold"] = 19
        self.assertEqual(result()["reason"], "threshold_missed")
        gate["min_samples"] = 5
        self.assertEqual(result()["reason"], "insufficient_calibrated_evidence")
        gate.update(slice="ascii", min_samples=2)
        self.assertEqual(result()["reason"], "insufficient_calibrated_evidence")

    def test_policy_rejects_wrong_suite_and_unapproved_metrics(self):
        run = json.dumps(evidence_for(SUITE_TEXT))
        policy = json.loads(POLICY_TEXT)
        policy["suite_sha256"] = "a" * 64
        with self.assertRaises(InputValidationError) as error:
            gate_suite_run(run, SUITE_TEXT, json.dumps(policy))
        self.assertEqual(error.exception.issues[0].code, "POLICY_SUITE_DIGEST_MISMATCH")

        policy["gates"][0]["metric"] = "model_grader"
        with self.assertRaises(InputValidationError) as error:
            gate_suite_run(run, SUITE_TEXT, json.dumps(policy))
        self.assertEqual(error.exception.issues[0].code, "INVALID_SUITE_POLICY")

import json
import unittest
from pathlib import Path

from evaluator.suite_scoring import score_suite_run
from evaluator.suites import digest, read_suite, suite_digest
from evaluator.validation import InputValidationError

SUITE_TEXT = (
    Path(__file__).resolve().parents[1] / "examples" / "scripted-suite.json"
).read_text(encoding="utf-8")


def evidence_for(suite_text: str) -> dict:
    suite = read_suite(suite_text)
    return {
        "schema_version": 1,
        "suite_id": suite.id,
        "suite_sha256": suite_digest(suite),
        "artifact_id": "candidate",
        "trials": [
            {
                "case_id": case.id,
                "trial": number,
                "status": "completed",
                "latency_ms": 10 * number,
                "response": {
                    "schema_version": 1,
                    "reported_success": True,
                    "cost_microusd": 0,
                    "tool_calls": case.acceptance.tool_calls,
                },
                "output_sha256": digest(case.acceptance.exact_text.encode("utf-8")),
            }
            for number in range(1, suite.trials + 1)
            for case in suite.cases
        ],
    }


class SuiteScoringTests(unittest.TestCase):
    def test_slices_keep_failures_errors_and_missing_evidence_distinct(self):
        suite = json.loads(SUITE_TEXT)
        suite["limits"].update(trial_cost_microusd=10, total_cost_microusd=10)
        suite_text = json.dumps(suite)
        run = evidence_for(suite_text)
        run["trials"][0]["output_sha256"] = None
        run["trials"][1]["response"]["tool_calls"] = None
        run["trials"][2].update(
            status="timed_out", response=None, output_sha256=None, latency_ms=3000
        )
        run["trials"][3].update(
            status="budget_exhausted", response=None, output_sha256=None, latency_ms=None
        )
        text = json.dumps(run)
        report = score_suite_run(text, suite_text)
        self.assertEqual(report, score_suite_run(text, suite_text))
        self.assertEqual(report["trial_count"], 4)
        self.assertEqual(report["summary"]["verified_success"]["counts"], {
            "pass": 1, "fail": 1, "error": 1, "unscored": 1,
        })
        self.assertEqual(report["summary"]["trial_completed"]["mean"], 0.666667)
        self.assertEqual(report["summary"]["latency_ms"]["p95"], 3000)
        self.assertEqual(report["summary"]["cost_microusd"]["scored_count"], 2)
        self.assertEqual(report["slices"]["unicode"]["trial_count"], 2)
        self.assertEqual(report["slices"]["unicode"]["case_count"], 1)
        self.assertEqual(
            report["slices"]["unicode"]["summary"]["tool_expectations"]["counts"]["unscored"],
            2,
        )
        self.assertEqual(report["trials"][0]["evidence_ref"], "/trials/0")

    def test_suite_pin_trial_plan_and_cost_are_checked(self):
        run = evidence_for(SUITE_TEXT)
        changed = json.loads(SUITE_TEXT)
        changed["model"]["revision"] = "different"
        cases = [
            (run, json.dumps(changed), "SUITE_DIGEST_MISMATCH"),
            ({**run, "trials": run["trials"][:-1]}, SUITE_TEXT, "TRIAL_PLAN_MISMATCH"),
        ]
        malformed = evidence_for(SUITE_TEXT)
        malformed["trials"][0]["response"]["cost_microusd"] = 10
        cases.append((malformed, SUITE_TEXT, "TRIAL_BUDGET_MISMATCH"))
        for evidence, suite_text, issue in cases:
            with self.subTest(issue=issue), self.assertRaises(InputValidationError) as error:
                score_suite_run(json.dumps(evidence), suite_text)
            self.assertEqual(error.exception.issues[0].code, issue)

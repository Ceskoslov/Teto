import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def agent_case(case_id, verified, reported=False):
    return {
        "id": case_id,
        "fixture_sha256": "a" * 64,
        "verified_success": verified,
        "reported_success": reported,
    }


def agent_run(artifact_id, *cases):
    return {
        "schema_version": 1,
        "suite_id": "smoke-v1",
        "artifact_id": artifact_id,
        "cases": list(cases),
    }


def nausicaa_summary():
    fixture = {
        "version": 1,
        "id": "repair",
        "split": "development",
        "objective": "PRIVATE_INSTRUCTION",
        "initial": "initial source",
        "expected": "fixed source",
        "proposals": ["wrong source", "fixed source"],
        "max_attempts": 2,
        "deny_write": False,
        "interrupt_execution": False,
    }
    return {
        "schema_version": 1,
        "fixture_suite": "task-baseline-v1",
        "live_config": None,
        "provider_settings": None,
        "skipped_fault_injection_fixtures": [],
        "runs": [
            {
                "fixture": fixture,
                "mode": mode,
                "reported_success": True,
                "verified_success": mode == "task",
                "live_model": False,
                "turn_errors": [],
                "artifact_directory": "/private/path",
            }
            for mode in ("core_only", "task")
        ],
    }


class AgentCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.baseline = directory / "baseline.json"
        self.candidate = directory / "candidate.json"
        self.report = directory / "report.json"
        self.summary = directory / "summary.json"

    def run_cli(self, *args):
        # Use the installed command in CI and support direct source-tree tests.
        teto = Path(sys.executable).with_name("teto")
        command = [str(teto)] if teto.is_file() else [sys.executable, "-m", "evaluator"]
        return subprocess.run(
            command + [str(arg) for arg in args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def compare(self, baseline=None, candidate=None, output=None):
        return self.run_cli(
            "compare",
            "--baseline",
            baseline or self.baseline,
            "--candidate",
            candidate or self.candidate,
            "--output",
            output or self.report,
        )

    def import_summary(self, mode, output):
        return self.run_cli(
            "import-nausicaa",
            "--input",
            self.summary,
            "--mode",
            mode,
            "--artifact-id",
            f"nausicaa-{mode}",
            "--output",
            output,
        )

    def score(self, source, evidence, *options):
        return self.run_cli(
            "score", source, "--input", evidence, "--output", self.report, *options
        )

    def assert_issue(self, process, code):
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual(json.loads(process.stderr)["error"]["issues"][0]["code"], code)
        self.assertNotIn("PRIVATE_INSTRUCTION", process.stderr)

    def test_import_and_compare_in_both_directions(self):
        self.summary.write_text(json.dumps(nausicaa_summary()))
        for mode, output in (("core_only", self.baseline), ("task", self.candidate)):
            with self.subTest(mode=mode):
                imported = self.import_summary(mode, output)
                self.assertEqual(imported.returncode, 0, imported.stderr)
                self.assertNotIn("PRIVATE_INSTRUCTION", output.read_text())
                self.assertNotIn("/private/path", output.read_text())

        comparison = self.compare()
        self.assertEqual(comparison.returncode, 0, comparison.stderr)
        self.assertEqual(
            json.loads(self.report.read_text())["recommendation"]["decision"], "release"
        )

        comparison = self.compare(baseline=self.candidate, candidate=self.baseline)
        self.assertEqual(comparison.returncode, 1, comparison.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["rule_version"], "agent-regression-v1")
        self.assertEqual(report["recommendation"]["decision"], "hold")
        self.assertEqual(
            report["cases"][0]["failed_gates"],
            ["verified_success_regressed", "candidate_false_completion"],
        )

    def test_generic_producer_and_per_case_regression(self):
        self.baseline.write_text(
            json.dumps(
                agent_run(
                    "before",
                    agent_case("improved", False),
                    agent_case("regressed", True),
                )
            )
        )
        self.candidate.write_text(
            json.dumps(
                agent_run(
                    "after",
                    agent_case("regressed", False),
                    agent_case("improved", True),
                )
            )
        )
        self.assertEqual(self.compare().returncode, 1)
        first = self.report.read_bytes()
        report = json.loads(first)
        self.assertEqual(
            [case["id"] for case in report["cases"]], ["improved", "regressed"]
        )
        self.assertEqual(
            report["cases"][1]["failed_gates"], ["verified_success_regressed"]
        )
        self.assertEqual(len(report["evidence"]["baseline"]["agent_run_sha256"]), 64)
        self.assertEqual(self.compare().returncode, 1)
        self.assertEqual(self.report.read_bytes(), first)

    def test_false_completion_holds_even_when_present_in_both_runs(self):
        case = agent_case("case", False, reported=True)
        self.baseline.write_text(json.dumps(agent_run("before", case)))
        self.candidate.write_text(json.dumps(agent_run("after", case)))
        self.assertEqual(self.compare().returncode, 1)
        report = json.loads(self.report.read_text())
        self.assertEqual(
            report["cases"][0]["failed_gates"], ["candidate_false_completion"]
        )

    def test_invalid_pairs_preserve_report_and_sources(self):
        case = agent_case("case", True)
        baseline = agent_run("before", case)
        self.baseline.write_text(json.dumps(baseline))
        self.report.write_text("previous report")
        candidate = agent_run("after", case)
        for data, code in (
            (agent_run("after", case, case), "DUPLICATE_CASE_ID"),
            (agent_run("after", {**case, "id": "other"}), "CASE_SETS_MUST_MATCH"),
            (
                agent_run("after", {**case, "fixture_sha256": "b" * 64}),
                "FIXTURE_DIGESTS_MUST_MATCH",
            ),
            (baseline, "ARTIFACT_IDS_MUST_DIFFER"),
            ({**candidate, "suite_id": "other"}, "SUITE_IDS_MUST_MATCH"),
            ({**candidate, "schema_version": True}, "INVALID_AGENT_RUN"),
        ):
            with self.subTest(code=code):
                self.candidate.write_text(json.dumps(data))
                self.assert_issue(self.compare(), code)
                self.assertEqual(self.report.read_text(), "previous report")

        self.candidate.write_text('{"schema_version":1,"schema_version":2}')
        self.assert_issue(self.compare(), "DUPLICATE_JSON_KEY")
        self.assertEqual(self.report.read_text(), "previous report")

        original = self.candidate.read_bytes()
        self.assert_issue(
            self.compare(output=self.candidate), "INPUT_OUTPUT_MUST_DIFFER"
        )
        self.assertEqual(self.candidate.read_bytes(), original)

    def test_import_rejects_unexpected_errors_and_live_runs(self):
        summary = nausicaa_summary()
        summary["runs"][1]["turn_errors"] = ["PRIVATE_INSTRUCTION"]
        self.summary.write_text(json.dumps(summary))
        self.assert_issue(
            self.import_summary("task", self.candidate), "NAUSICAA_TURN_FAILED"
        )
        self.assertFalse(self.candidate.exists())

        summary["live_config"] = "live-config.json"
        self.summary.write_text(json.dumps(summary))
        self.assert_issue(
            self.import_summary("task", self.candidate), "UNSUPPORTED_NAUSICAA_SUMMARY"
        )

    def test_score_agent_run_without_baseline_and_select_metrics(self):
        self.candidate.write_text(
            json.dumps(
                agent_run("candidate", agent_case("missed", False, reported=True))
            )
        )
        result = self.score("agent-run", self.candidate)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["score_report_schema_version"], 1)
        self.assertEqual(report["evidence"]["artifact_id"], "candidate")
        self.assertEqual(report["summary"]["verified_success"]["counts"]["fail"], 1)
        self.assertEqual(
            report["cases"][0]["scores"]["no_false_completion"]["reason"],
            "false_completion",
        )

        self.assertEqual(self.score("agent-run", self.candidate).returncode, 0)
        first = self.report.read_bytes()
        self.assertEqual(self.score("agent-run", self.candidate).returncode, 0)
        self.assertEqual(self.report.read_bytes(), first)

        selected = self.score(
            "agent-run", self.candidate, "--metric", "verified_success"
        )
        self.assertEqual(selected.returncode, 0, selected.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(
            report["scorers"], {"verified_success": "agent-run/verified_success@1"}
        )
        self.assertEqual(set(report["cases"][0]["scores"]), {"verified_success"})

    def test_score_nausicaa_receipts_and_unexpected_errors(self):
        summary = nausicaa_summary()
        task = summary["runs"][1]
        task.update(denied_receipts=0, unknown_receipts=1)
        options = ("--mode", "task", "--artifact-id", "fixture")

        for change, status in (
            ({}, "fail"),
            ({"unknown_receipts": 0}, "pass"),
            ({"unknown_receipts": None}, "unscored"),
            ({"unknown_receipts": True}, "error"),
            ({"turn_errors": ["PRIVATE_INSTRUCTION"]}, "error"),
        ):
            with self.subTest(change=change):
                evidence = {**summary, "runs": [{**task, **change}]}
                self.summary.write_text(json.dumps(evidence))
                result = self.score("nausicaa", self.summary, *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(self.report.read_text())
                self.assertEqual(
                    report["summary"]["receipt_expectations"]["counts"][status], 1
                )
                self.assertNotIn("PRIVATE_INSTRUCTION", self.report.read_text())
                if "turn_errors" in change:
                    self.assertEqual(
                        report["summary"]["verified_success"]["counts"]["error"], 1
                    )

    def test_score_ohmu_recomputes_recall_and_separates_rejected_answers(self):
        annotations = ROOT / "examples/ohmu-cases.json"
        source = json.loads((ROOT / "examples/ohmu-evaluation.json").read_text())
        source["results"][0]["expected_source_recall"] = 1.0
        self.summary.write_text(json.dumps(source))
        result = self.score(
            "ohmu", self.summary, "--cases", annotations, "--artifact-id", "fixture"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(
            report["evidence"]["input_sha256"],
            hashlib.sha256(self.summary.read_bytes()).hexdigest(),
        )
        self.assertEqual(report["summary"]["expected_source_recall"]["mean"], 0.5)
        self.assertEqual(report["summary"]["host_acceptance"]["counts"]["fail"], 1)
        cases = {case["id"]: case["scores"] for case in report["cases"]}
        self.assertEqual(cases["expected-abstention"]["abstention"]["status"], "pass")
        self.assertEqual(cases["rejected-answer"]["abstention"]["status"], "unscored")
        self.assertNotIn("returns policy", self.report.read_text())

    def test_score_ohmu_validates_raw_annotation_bytes_and_preserves_report(self):
        self.report.write_text("previous report")
        source = json.loads((ROOT / "examples/ohmu-evaluation.json").read_text())
        annotations = (
            (ROOT / "examples/ohmu-cases.json").read_bytes().replace(b"\n", b"\r\n")
        )
        self.candidate.write_bytes(annotations)
        self.summary.write_text(json.dumps(source))
        options = ("--cases", self.candidate, "--artifact-id", "fixture")
        self.assert_issue(
            self.score("ohmu", self.summary, *options),
            "OHMU_ANNOTATIONS_DIGEST_MISMATCH",
        )
        self.assertEqual(self.report.read_text(), "previous report")

        source["cases_sha256"] = hashlib.sha256(annotations).hexdigest()
        self.summary.write_text(json.dumps(source))
        self.assertEqual(self.score("ohmu", self.summary, *options).returncode, 0)

        source["results"].pop()
        self.summary.write_text(json.dumps(source))
        self.assert_issue(
            self.score("ohmu", self.summary, *options), "CASE_SETS_MUST_MATCH"
        )

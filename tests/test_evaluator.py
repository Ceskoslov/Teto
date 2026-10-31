import json
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from evaluator import evaluate
from evaluator.policy import DEFAULT_POLICY
from evaluator.validation import validate_request

ROOT = Path(__file__).resolve().parents[1]


def make_case(case_id="case-1", *, baseline_output="answer", candidate_output="answer"):
    return {
        "id": case_id,
        "prompt": "A private question",
        "expected_terms": ["answer"],
        "forbidden_terms": ["unsafe phrase"],
        "baseline": {"output": baseline_output, "latency_ms": 100, "cost_usd": 0.1},
        "candidate": {"output": candidate_output, "latency_ms": 100, "cost_usd": 0.1},
    }


def make_input(cases=None):
    return {
        "product": "test-product",
        "dataset_version": "test-v1",
        "baseline_artifact_id": "baseline-v1",
        "candidate_artifact_id": "candidate-v2",
        "cases": cases if cases is not None else [make_case()],
    }


def run_evaluation(data, policy=DEFAULT_POLICY):
    return evaluate(validate_request(json.dumps(data)), policy)


def by_id(report, gate_id):
    return next(gate for gate in report["gates"] if gate["id"] == gate_id)


class EvaluatorTests(unittest.TestCase):
    def test_challenge_fixture_matches_independent_oracle(self):
        request = validate_request((ROOT / "examples/input.json").read_text())
        expected = json.loads((ROOT / "examples/input.expected.json").read_text())
        report = evaluate(request)
        self.assertEqual(report["policy"]["id"], expected["policy_id"])
        self.assertEqual(report["gates"], expected["gates"])
        self.assertEqual(report["recommendation"], expected["recommendation"])
        summary = report["summary"]
        self.assertEqual(
            summary["quality"]["mean_score"],
            {"baseline": 0.75, "candidate": 1, "delta": 0.25},
        )
        self.assertEqual(summary["safety_annotated_case_count"], 3)
        self.assertEqual(
            summary["latency"]["mean_ms"],
            {"baseline": 950, "candidate": 1100, "delta": 150},
        )
        self.assertEqual(
            summary["cost"]["total_usd"],
            {"baseline": 0.008, "candidate": 0.009, "delta": 0.001},
        )

    def test_all_gates_pass(self):
        report = run_evaluation(make_input())
        self.assertEqual(
            report["recommendation"], {"decision": "release", "failed_gate_ids": []}
        )
        self.assertTrue(all(gate["passed"] for gate in report["gates"]))

    def test_matching_normalizes_unicode_case_and_whitespace(self):
        case = make_case(candidate_output="An ＡＮＳＷＥＲ\n\tfor you")
        case["expected_terms"] = ["answer for", "absent"]
        finding = run_evaluation(make_input([case]))["cases"][0]["candidate"]
        self.assertEqual(finding["quality_score"], 0.5)
        self.assertEqual(finding["expected_matched_indices"], [0])
        self.assertEqual(finding["expected_missing_indices"], [1])

    def test_substring_matching_preserves_punctuation(self):
        case = make_case(candidate_output="reset-link answers")
        case["expected_terms"] = ["reset link", "answer"]
        finding = run_evaluation(make_input([case]))["cases"][0]["candidate"]
        self.assertEqual(finding["expected_matched_indices"], [1])

    def test_unannotated_case_is_excluded_only_from_quality(self):
        unannotated = make_case("unannotated", candidate_output="")
        unannotated["expected_terms"] = []
        unannotated["forbidden_terms"] = []
        report = run_evaluation(make_input([make_case(), unannotated]))
        summary = report["summary"]
        self.assertEqual(summary["case_count"], 2)
        self.assertEqual(summary["quality_annotated_case_count"], 1)
        self.assertEqual(summary["safety_annotated_case_count"], 1)
        self.assertEqual(summary["quality"]["mean_score"]["candidate"], 1)
        self.assertEqual(summary["quality"]["complete_match_rate"]["candidate"], 1)
        self.assertEqual(summary["quality"]["unchanged_case_count"], 1)
        self.assertIsNone(report["cases"][1]["candidate"]["quality_score"])
        self.assertIsNone(report["cases"][1]["comparison"]["quality_regressed"])

    def test_quality_is_macro_averaged(self):
        first = make_case("first", baseline_output="alpha", candidate_output="alpha")
        first["expected_terms"] = ["alpha", "beta", "gamma"]
        report = run_evaluation(make_input([first, make_case("second")]))
        self.assertEqual(
            report["summary"]["quality"]["mean_score"]["candidate"], 0.666667
        )

    def test_case_regression_cannot_hide_behind_aggregate_improvement(self):
        cases = [
            make_case("loss", candidate_output="missing"),
            make_case("gain-1", baseline_output="missing"),
            make_case("gain-2", baseline_output="missing"),
            make_case("same-1"),
            make_case("same-2"),
        ]
        report = run_evaluation(make_input(cases))
        self.assertEqual(report["summary"]["quality"]["mean_score"]["delta"], 0.2)
        self.assertEqual(
            report["recommendation"]["failed_gate_ids"], ["case_quality_non_regression"]
        )
        self.assertEqual(
            by_id(report, "case_quality_non_regression")["affected_case_ids"], ["loss"]
        )

    def test_quality_floor_and_mean_regression_fail(self):
        report = run_evaluation(make_input([make_case(candidate_output="missing")]))
        self.assertEqual(
            report["recommendation"]["failed_gate_ids"],
            [
                "quality_floor",
                "quality_non_regression",
                "case_quality_non_regression",
            ],
        )

    def test_baseline_safety_does_not_excuse_candidate_violation(self):
        case = make_case(
            baseline_output="answer unsafe phrase",
            candidate_output="answer unsafe phrase",
        )
        case["forbidden_terms"].append("unsafe")
        report = run_evaluation(make_input([case]))
        self.assertFalse(by_id(report, "candidate_safety")["passed"])
        self.assertEqual(report["summary"]["safety"]["new_violation_case_ids"], [])
        self.assertEqual(
            report["summary"]["safety"]["violation_case_count"]["candidate"], 1
        )
        self.assertEqual(
            report["summary"]["safety"]["forbidden_match_count"]["candidate"], 2
        )

    def test_resolving_baseline_safety_allows_release(self):
        case = make_case(baseline_output="answer unsafe phrase")
        report = run_evaluation(make_input([case]))
        self.assertEqual(report["recommendation"]["decision"], "release")
        self.assertEqual(
            report["summary"]["safety"]["resolved_violation_case_ids"], ["case-1"]
        )

    def test_exact_threshold_boundaries_pass(self):
        case = make_case(
            baseline_output="one two three four", candidate_output="one two three four"
        )
        case["expected_terms"] = ["one", "two", "three", "four", "five"]
        case["candidate"].update(latency_ms=120, cost_usd=0.11)
        report = run_evaluation(make_input([case]))
        self.assertEqual(report["recommendation"]["decision"], "release")
        self.assertEqual(by_id(report, "quality_floor")["observed"], 0.8)

    def test_gate_comparisons_happen_before_rounding(self):
        case = make_case()
        case["candidate"].update(latency_ms=120.0000001, cost_usd=0.11000001)
        report = run_evaluation(make_input([case]))
        for gate_id in ("latency_budget", "cost_budget"):
            gate = by_id(report, gate_id)
            self.assertEqual(gate["observed"], gate["limit"])
            self.assertFalse(gate["passed"])

    def test_nearest_rank_p95_is_not_always_maximum(self):
        cases = [make_case(f"case-{index}") for index in range(1, 21)]
        for index, case in enumerate(cases, start=1):
            case["baseline"]["latency_ms"] = index
            case["candidate"]["latency_ms"] = index
        report = run_evaluation(make_input(cases))
        self.assertEqual(report["summary"]["latency"]["p95_ms"]["candidate"], 19)
        self.assertNotIn("P95_EQUALS_SAMPLE_MAX", report["summary"]["limitations"])

    def test_zero_baselines(self):
        for candidate_value in (0, 1):
            with self.subTest(candidate_value=candidate_value):
                case = make_case()
                case["baseline"].update(latency_ms=0, cost_usd=0)
                case["candidate"].update(
                    latency_ms=candidate_value, cost_usd=candidate_value
                )
                report = run_evaluation(make_input([case]))
                for gate_id in ("latency_budget", "cost_budget"):
                    self.assertEqual(
                        by_id(report, gate_id)["passed"], candidate_value == 0
                    )
                self.assertIsNone(
                    report["summary"]["latency"]["p95_ms"]["relative_change"]
                )
                self.assertIsNone(
                    report["summary"]["cost"]["mean_usd"]["relative_change"]
                )

    def test_rounding_uses_decimal_ties_to_even(self):
        case = make_case()
        case["candidate"].update(latency_ms=1.2345665, cost_usd=1.2345675)
        finding = run_evaluation(make_input([case]))["cases"][0]["candidate"]
        self.assertEqual(finding["latency_ms"], 1.234566)
        self.assertEqual(finding["cost_usd"], 1.234568)

    def test_determinism_input_immutability_and_json_serialization(self):
        request = validate_request(json.dumps(make_input()))
        before = deepcopy(request.model_dump())
        first = evaluate(request)
        second = evaluate(request)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, allow_nan=False), json.dumps(second, allow_nan=False)
        )
        self.assertEqual(request.model_dump(), before)
        first["policy"]["gates"][0]["fixed_limit"] = "99"
        self.assertEqual(evaluate(request), second)

    def test_hash_tracks_evidence_but_not_object_key_order(self):
        data = make_input()
        original = run_evaluation(data)
        reordered = evaluate(
            validate_request(json.dumps(data, sort_keys=True, indent=4))
        )
        self.assertEqual(original, reordered)
        data["cases"][0]["prompt"] += " changed"
        changed = run_evaluation(data)
        self.assertNotEqual(
            original["provenance"]["input_sha256"],
            changed["provenance"]["input_sha256"],
        )
        self.assertEqual(original["gates"], changed["gates"])

    def test_hash_preserves_int_float_distinction(self):
        data = make_input()
        before = run_evaluation(data)
        data["cases"][0]["candidate"]["latency_ms"] = 100.0
        after = run_evaluation(data)
        self.assertNotEqual(
            before["provenance"]["input_sha256"], after["provenance"]["input_sha256"]
        )

    def test_policy_thresholds_and_hash_come_from_snapshot(self):
        case = make_case()
        case["candidate"]["latency_ms"] = 130
        before = run_evaluation(make_input([case]))
        gates = tuple(
            replace(gate, multiplier="1.30") if gate.id == "latency_budget" else gate
            for gate in DEFAULT_POLICY.gates
        )
        policy = replace(DEFAULT_POLICY, id="offline-terms-v2-test", gates=gates)
        after = run_evaluation(make_input([case]), policy)
        self.assertEqual(before["recommendation"]["decision"], "hold")
        self.assertEqual(after["recommendation"]["decision"], "release")
        self.assertNotEqual(
            before["provenance"]["policy_sha256"], after["provenance"]["policy_sha256"]
        )

    def test_gate_requires_a_limit_source(self):
        gates = tuple(
            replace(gate, fixed_limit=None, baseline_metric=None)
            if gate.id == "quality_floor"
            else gate
            for gate in DEFAULT_POLICY.gates
        )
        policy = replace(DEFAULT_POLICY, gates=gates)
        with self.assertRaisesRegex(ValueError, "GATE_LIMIT_REQUIRED"):
            run_evaluation(make_input(), policy)

    def test_report_omits_raw_text(self):
        data = make_input()
        case = data["cases"][0]
        case["prompt"] = "PRIVATE_PROMPT_7e83"
        case["expected_terms"] = ["PRIVATE_TERM_8a4d"]
        case["forbidden_terms"] = ["PRIVATE_FORBIDDEN_a4d1"]
        case["baseline"]["output"] = "PRIVATE_BASELINE_1c2b"
        case["candidate"]["output"] = "PRIVATE_CANDIDATE_9d3f"
        serialized = json.dumps(run_evaluation(data))
        for value in (
            case["prompt"],
            *case["expected_terms"],
            *case["forbidden_terms"],
            case["baseline"]["output"],
            case["candidate"]["output"],
        ):
            self.assertNotIn(value, serialized)


if __name__ == "__main__":
    unittest.main()

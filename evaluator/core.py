"""Pure, deterministic evaluation of previously generated responses."""

import hashlib
import json
import operator
from fractions import Fraction
from typing import Any

from .models import Case, EvaluationRequest, ResponseObservation
from .policy import DEFAULT_POLICY, ReleasePolicy
from .text import normalize_text

EVALUATOR_VERSION = "0.1.0"
REPORT_SCHEMA_VERSION = "1.0"


def _number(value: float) -> Fraction:
    # Use the validated decimal representation, not float's binary expansion.
    return Fraction(str(value))


def _digest(value: Any) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _render(value: Any) -> Any:
    """Round only at the report boundary, after every decision is made."""
    if isinstance(value, Fraction):
        rounded = round(value, 6)
        return rounded.numerator if rounded.denominator == 1 else float(rounded)
    if isinstance(value, dict):
        return {key: _render(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item) for item in value]
    return value


def _score_observation(case: Case, observation: ResponseObservation) -> dict:
    output = normalize_text(observation.output)
    expected_matches = [normalize_text(term) in output for term in case.expected_terms]
    forbidden_indices = [
        index
        for index, term in enumerate(case.forbidden_terms)
        if normalize_text(term) in output
    ]
    return {
        "quality_score": (
            Fraction(sum(expected_matches), len(expected_matches))
            if expected_matches
            else None
        ),
        "expected_matched_indices": [
            index for index, matched in enumerate(expected_matches) if matched
        ],
        "expected_missing_indices": [
            index for index, matched in enumerate(expected_matches) if not matched
        ],
        "forbidden_matched_indices": forbidden_indices,
        "safety_violation": bool(forbidden_indices),
        "latency_ms": _number(observation.latency_ms),
        "cost_usd": _number(observation.cost_usd),
    }


def _evaluate_case(case: Case) -> dict:
    baseline = _score_observation(case, case.baseline)
    candidate = _score_observation(case, case.candidate)
    quality_delta = (
        candidate["quality_score"] - baseline["quality_score"]
        if baseline["quality_score"] is not None
        else None
    )
    latency_delta = candidate["latency_ms"] - baseline["latency_ms"]
    cost_delta = candidate["cost_usd"] - baseline["cost_usd"]
    return {
        "id": case.id,
        "baseline": baseline,
        "candidate": candidate,
        "comparison": {
            "quality_delta": quality_delta,
            "quality_regressed": quality_delta < 0
            if quality_delta is not None
            else None,
            "new_safety_violation": (
                candidate["safety_violation"] and not baseline["safety_violation"]
            ),
            "resolved_safety_violation": (
                baseline["safety_violation"] and not candidate["safety_violation"]
            ),
            "latency_delta_ms": latency_delta,
            "latency_regressed": latency_delta > 0,
            "cost_delta_usd": cost_delta,
            "cost_regressed": cost_delta > 0,
        },
    }


def _aggregate(cases: list[dict], side: str) -> dict:
    observations = [case[side] for case in cases]
    scores = [
        observation["quality_score"]
        for observation in observations
        if observation["quality_score"] is not None
    ]
    latencies = sorted(observation["latency_ms"] for observation in observations)
    count = len(cases)
    violation_count = sum(
        observation["safety_violation"] for observation in observations
    )
    complete_count = sum(score == 1 for score in scores)
    total_cost = sum(observation["cost_usd"] for observation in observations)
    return {
        "mean_quality": sum(scores) / len(scores),
        "complete_match_case_count": complete_count,
        "complete_match_rate": Fraction(complete_count, len(scores)),
        "safety_violation_case_count": violation_count,
        "safety_violation_rate": Fraction(violation_count, count),
        "forbidden_match_count": sum(
            len(observation["forbidden_matched_indices"])
            for observation in observations
        ),
        "mean_latency_ms": sum(latencies) / count,
        "p95_latency_ms": latencies[(95 * count + 99) // 100 - 1],
        "total_cost_usd": total_cost,
        "mean_cost_usd": total_cost / count,
    }


def _pair(baseline: dict, candidate: dict, metric: str, *, relative=False) -> dict:
    before, after = baseline[metric], candidate[metric]
    pair = {"baseline": before, "candidate": after, "delta": after - before}
    if relative:
        pair["relative_change"] = (after - before) / before if before else None
    return pair


def _summary(
    request: EvaluationRequest, cases: list[dict], baseline: dict, candidate: dict
) -> dict:
    deltas = [
        case["comparison"]["quality_delta"]
        for case in cases
        if case["comparison"]["quality_delta"] is not None
    ]
    return {
        "case_count": len(cases),
        "quality_annotated_case_count": len(deltas),
        "safety_annotated_case_count": sum(
            bool(case.forbidden_terms) for case in request.cases
        ),
        "limitations": [
            "ANNOTATION_MATCHING_PROXY",
            "SINGLE_OBSERVATION_PER_ARTIFACT_CASE",
            "NO_STATISTICAL_CONFIDENCE_ESTIMATE",
        ]
        + (["P95_EQUALS_SAMPLE_MAX"] if len(cases) < 20 else []),
        "quality": {
            "mean_score": _pair(baseline, candidate, "mean_quality"),
            "complete_match_case_count": _pair(
                baseline, candidate, "complete_match_case_count"
            ),
            "complete_match_rate": _pair(baseline, candidate, "complete_match_rate"),
            "improved_case_count": sum(delta > 0 for delta in deltas),
            "unchanged_case_count": sum(delta == 0 for delta in deltas),
            "regressed_case_count": sum(delta < 0 for delta in deltas),
        },
        "safety": {
            "violation_case_count": _pair(
                baseline, candidate, "safety_violation_case_count"
            ),
            "violation_rate": _pair(baseline, candidate, "safety_violation_rate"),
            "forbidden_match_count": _pair(
                baseline, candidate, "forbidden_match_count"
            ),
            "new_violation_case_ids": [
                case["id"]
                for case in cases
                if case["comparison"]["new_safety_violation"]
            ],
            "resolved_violation_case_ids": [
                case["id"]
                for case in cases
                if case["comparison"]["resolved_safety_violation"]
            ],
        },
        "latency": {
            "mean_ms": _pair(baseline, candidate, "mean_latency_ms"),
            "p95_ms": _pair(baseline, candidate, "p95_latency_ms", relative=True),
        },
        "cost": {
            "total_usd": _pair(baseline, candidate, "total_cost_usd"),
            "mean_usd": _pair(baseline, candidate, "mean_cost_usd", relative=True),
        },
    }


def _evaluate_gates(
    cases: list[dict], baseline: dict, candidate: dict, policy: ReleasePolicy
) -> list[dict]:
    regressed = [
        case["id"] for case in cases if case["comparison"]["quality_regressed"]
    ]
    metrics = {
        **{f"baseline.{key}": value for key, value in baseline.items()},
        **{f"candidate.{key}": value for key, value in candidate.items()},
        "quality_regressed_case_count": len(regressed),
    }
    affected = {
        "candidate_safety": [
            case["id"] for case in cases if case["candidate"]["safety_violation"]
        ],
        "case_quality_non_regression": regressed,
    }
    comparisons = {"==": operator.eq, ">=": operator.ge, "<=": operator.le}
    gates = []
    for definition in policy.gates:
        observed = metrics[definition.metric]
        if definition.fixed_limit is not None:
            limit = Fraction(definition.fixed_limit)
        elif definition.baseline_metric is not None:
            limit = metrics[definition.baseline_metric] * Fraction(
                definition.multiplier
            )
        else:
            raise ValueError("GATE_LIMIT_REQUIRED")
        passed = comparisons[definition.operator](observed, limit)
        gates.append(
            {
                "id": definition.id,
                "passed": passed,
                "observed": observed,
                "operator": definition.operator,
                "limit": limit,
                "affected_case_ids": affected.get(definition.id, []),
                "reason_code": f"{definition.id}_{'passed' if passed else 'failed'}".upper(),
            }
        )
    return gates


def evaluate(
    request: EvaluationRequest, policy: ReleasePolicy = DEFAULT_POLICY
) -> dict:
    """Evaluate a validated request without I/O, logging or input mutation."""
    cases = [_evaluate_case(case) for case in request.cases]
    baseline, candidate = (
        _aggregate(cases, side) for side in ("baseline", "candidate")
    )
    gates = _evaluate_gates(cases, baseline, candidate, policy)
    failed = [gate["id"] for gate in gates if not gate["passed"]]
    snapshot = policy.snapshot()
    return _render(
        {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "provenance": {
                "product": request.product,
                "dataset_version": request.dataset_version,
                "baseline_artifact_id": request.baseline_artifact_id,
                "candidate_artifact_id": request.candidate_artifact_id,
                "input_sha256": _digest(request.model_dump(mode="json")),
                "policy_sha256": _digest(snapshot),
                "evaluator_version": EVALUATOR_VERSION,
            },
            "policy": snapshot,
            "summary": _summary(request, cases, baseline, candidate),
            "cases": cases,
            "gates": gates,
            "recommendation": {
                "decision": "hold" if failed else "release",
                "failed_gate_ids": failed,
            },
        }
    )

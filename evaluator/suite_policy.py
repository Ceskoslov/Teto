"""Apply a versioned standalone policy to validated suite evidence."""

from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field

from .agent_runs import FixtureDigest, VersionOne
from .models import Identifier, InputModel, NonNegativeNumber
from .suite_scoring import score_suite_run, statistic
from .suites import digest, invalid, read_contract

Metric = Literal[
    "verified_success", "no_false_completion", "tool_expectations",
    "trial_completed", "latency_ms", "cost_microusd",
]
_BINARY = {"verified_success", "no_false_completion", "tool_expectations", "trial_completed"}


class _Gate(InputModel):
    id: Identifier
    metric: Metric
    slice: Identifier | None = None


class _Critical(_Gate):
    kind: Literal["critical"]
    metric: Literal[
        "verified_success", "no_false_completion", "tool_expectations", "trial_completed"
    ]


class _Calibration(InputModel):
    id: Identifier
    evidence_sha256: FixtureDigest


class _Calibrated(_Gate):
    kind: Literal["calibrated"]
    statistic: Literal["mean", "p95"]
    operator: Literal["at_least", "at_most"]
    threshold: NonNegativeNumber
    min_samples: int = Field(ge=2)
    min_cases: int = Field(ge=2)
    min_coverage_basis_points: int = Field(gt=0, le=10000)
    calibration: _Calibration

    def model_post_init(self, context: object) -> None:
        if self.statistic == "p95" and self.metric != "latency_ms":
            raise ValueError("p95 is defined for latency")
        if self.metric in _BINARY and self.threshold > 1:
            raise ValueError("binary metric threshold must be a rate")


class _Observe(_Gate):
    kind: Literal["observe"]


Gate = Annotated[_Critical | _Calibrated | _Observe, Field(discriminator="kind")]


class Policy(InputModel):
    schema_version: VersionOne
    id: Identifier
    suite_sha256: FixtureDigest
    gates: list[Gate] = Field(min_length=1, max_length=100)

    def model_post_init(self, context: object) -> None:
        if len({gate.id for gate in self.gates}) != len(self.gates):
            raise ValueError("duplicate gate ID")
        if all(gate.kind == "observe" for gate in self.gates):
            raise ValueError("at least one enforcing gate is required")


def _gate_result(gate: Gate, summary: dict, rows: list[dict]) -> dict:
    samples = [
        (row["case_id"], score["value"]) for row in rows
        if (score := row["scores"][gate.metric])["value"] is not None
    ]
    sample_count = len(samples)
    case_count = len({case_id for case_id, _ in samples})
    result = {
        "id": gate.id,
        "kind": gate.kind,
        "metric": gate.metric,
        "slice": gate.slice,
        "trial_count": len(rows),
        "scored_count": sample_count,
        "scored_case_count": case_count,
        "summary": summary,
    }
    if gate.kind == "observe":
        return {**result, "status": "observed"}

    if gate.kind == "critical":
        if summary["counts"]["fail"]:
            reason = "critical_check_failed"
        elif sample_count != len(rows):
            reason = "incomplete_critical_evidence"
        else:
            reason = None
    else:
        observed = statistic([value for _, value in samples], gate.statistic)
        result["observed"] = None if observed is None else float(round(observed, 6))
        if (
            observed is None
            or sample_count < gate.min_samples
            or case_count < gate.min_cases
            or sample_count * 10000 < len(rows) * gate.min_coverage_basis_points
        ):
            reason = "insufficient_calibrated_evidence"
        else:
            threshold = Fraction(str(gate.threshold))
            meets_threshold = (
                observed >= threshold if gate.operator == "at_least" else observed <= threshold
            )
            reason = None if meets_threshold else "threshold_missed"

    return {**result, "status": "pass" if reason is None else "hold", "reason": reason}


def get_policy(policy_text: str) -> Policy:
    return read_contract(policy_text, Policy, "INVALID_SUITE_POLICY")


def check_policy_suite(policy: Policy, sha256: str, slices: set[str]) -> None:
    if sha256 != policy.suite_sha256:
        raise invalid("POLICY_SUITE_DIGEST_MISMATCH")
    if any(gate.slice is not None and gate.slice not in slices for gate in policy.gates):
        raise invalid("UNKNOWN_POLICY_SLICE")


def gate_suite_run(text: str, suite_text: str, policy_text: str) -> dict:
    policy = get_policy(policy_text)
    report = score_suite_run(text, suite_text)
    return apply_suite_policy(report, policy, digest(policy_text.encode("utf-8")))


def apply_suite_policy(report: dict, policy: Policy, policy_sha256: str) -> dict:
    evidence = report["evidence"]
    check_policy_suite(policy, evidence["suite_sha256"], set(report["slices"]))

    results = []
    for gate in policy.gates:
        if gate.slice is None:
            summary = report["summary"]
            rows = report["trials"]
        else:
            summary = report["slices"][gate.slice]["summary"]
            rows = [row for row in report["trials"] if gate.slice in row["slices"]]
        results.append(_gate_result(gate, summary[gate.metric], rows))
    failed = [gate["id"] for gate in results if gate["status"] == "hold"]
    return {
        "suite_policy_report_schema_version": 1,
        "evidence": evidence,
        "scorers_sha256": report["scorers_sha256"],
        "policy_sha256": policy_sha256,
        "policy": policy.model_dump(mode="json"),
        "gates": results,
        "recommendation": {
            "decision": "hold" if failed else "release",
            "failed_gates": failed,
        },
    }

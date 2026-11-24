"""Deterministic scoring of one saved evaluation artifact."""

import hashlib
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Literal

from pydantic import Field, TypeAdapter, ValidationError

from .agent_runs import (
    FixtureDigest,
    NausicaaMode,
    NausicaaRun,
    VersionOne,
    parse_agent_run,
    read_nausicaa_runs,
)
from .models import Identifier, InputModel
from .validation import InputValidationError, ValidationIssue, json_sha256, parse_json

_TASK_METRICS = ("verified_success", "no_false_completion")
METRICS = {
    "agent-run": _TASK_METRICS,
    "nausicaa": (*_TASK_METRICS, "receipt_expectations"),
    "ohmu": (
        "host_acceptance",
        "quote_verification",
        "expected_source_recall",
        "abstention",
    ),
}
_STATUSES = ("pass", "fail", "unscored", "error")


@dataclass(frozen=True)
class Score:
    status: Literal["pass", "fail", "unscored", "error"]
    value: float | int | None
    reason: str


@dataclass(frozen=True)
class CaseScores:
    id: str
    evidence_ref: str
    scores: dict[str, Score]


class _OhmuResult(InputModel, extra="ignore"):
    id: Identifier
    accepted: bool
    quote_checks_passed: bool
    cited_sources: list[Identifier]
    abstained: bool


class _OhmuEvaluation(InputModel, extra="ignore"):
    schema_version: VersionOne
    cases_sha256: FixtureDigest
    results: list[_OhmuResult] = Field(min_length=1)


class _OhmuAnnotation(InputModel, extra="ignore"):
    id: Identifier
    question: str
    expected_sources: list[Identifier] = Field(default_factory=list)
    expected_abstain: bool | None = None


_OHMU_ANNOTATIONS = TypeAdapter(list[_OhmuAnnotation])


def _invalid(code: str, path: str = "$") -> InputValidationError:
    return InputValidationError("INVALID_INPUT", [ValidationIssue(path, code)])


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binary(passed: bool, success: str, failure: str) -> Score:
    if passed:
        return Score("pass", 1, success)
    return Score("fail", 0, failure)


def _task_scores(reported: bool, verified: bool) -> dict[str, Score]:
    return {
        "verified_success": _binary(verified, "task_verified", "task_not_verified"),
        "no_false_completion": _binary(
            not reported or verified, "no_false_completion", "false_completion"
        ),
    }


def _receipt_score(run: NausicaaRun) -> Score:
    extra = run.model_extra or {}
    denied = extra.get("denied_receipts")
    unknown = extra.get("unknown_receipts")
    if any(
        type(value) is not int or value < 0
        for value in (denied, unknown)
        if value is not None
    ):
        return Score("error", None, "invalid_receipt_counts")
    if denied is None or unknown is None:
        return Score("unscored", None, "missing_receipt_counts")
    denied_matches = (denied > 0) == run.fixture.deny_write
    unknown_matches = unknown == int(run.fixture.interrupt_execution)
    return _binary(
        denied_matches and unknown_matches,
        "receipt_expectations_met",
        "receipt_expectations_missed",
    )


def _nausicaa_scores(text: str, mode: NausicaaMode) -> list[CaseScores]:
    cases = []
    for index, run in read_nausicaa_runs(text, mode):
        if run.has_unexpected_turn_errors():
            scores = {
                metric: Score("error", None, "unexpected_turn_error")
                for metric in METRICS["nausicaa"]
            }
        else:
            scores = _task_scores(run.reported_success, run.verified_success)
            scores["receipt_expectations"] = _receipt_score(run)
        cases.append(CaseScores(run.fixture.id, f"/runs/{index}", scores))
    return cases


def _ohmu_case(result: _OhmuResult, annotation: _OhmuAnnotation) -> dict[str, Score]:
    acceptance = _binary(
        result.accepted, "host_accepted_answer", "host_rejected_answer"
    )
    if not result.accepted:
        unavailable = Score("unscored", None, "no_accepted_answer")
        return {
            "host_acceptance": acceptance,
            "quote_verification": unavailable,
            "expected_source_recall": unavailable,
            "abstention": unavailable,
        }

    if not result.quote_checks_passed:
        quote = Score("fail", 0, "host_quote_verification_failed")
    elif not result.cited_sources:
        quote = Score("unscored", None, "no_quotes")
    else:
        quote = Score("pass", 1, "host_verified_quotes")

    expected = set(annotation.expected_sources)
    if expected:
        matched = len(expected.intersection(result.cited_sources))
        if matched == len(expected):
            recall = Score("pass", 1, "all_expected_sources_cited")
        else:
            recall = Score("fail", matched / len(expected), "expected_sources_missing")
    else:
        recall = Score("unscored", None, "no_expected_sources")

    abstention = Score("unscored", None, "no_abstention_annotation")
    if annotation.expected_abstain is not None:
        abstention = _binary(
            result.abstained == annotation.expected_abstain,
            "abstention_matches_annotation",
            "abstention_mismatch",
        )
    return {
        "host_acceptance": acceptance,
        "quote_verification": quote,
        "expected_source_recall": recall,
        "abstention": abstention,
    }


def _ohmu_scores(text: str, annotations_text: str) -> tuple[str, list[CaseScores]]:
    data = parse_json(text)
    try:
        evaluation = _OhmuEvaluation.model_validate(data)
    except ValidationError:
        raise _invalid("INVALID_OHMU_EVALUATION") from None
    if _sha256(annotations_text) != evaluation.cases_sha256:
        raise _invalid("OHMU_ANNOTATIONS_DIGEST_MISMATCH", "$.cases")
    annotations_data = parse_json(annotations_text)
    try:
        annotations = _OHMU_ANNOTATIONS.validate_python(annotations_data, strict=True)
    except ValidationError:
        raise _invalid("INVALID_OHMU_ANNOTATIONS", "$.cases") from None

    by_id = {annotation.id: annotation for annotation in annotations}
    if len(by_id) != len(annotations):
        raise _invalid("DUPLICATE_CASE_ID", "$.cases")
    if by_id.keys() != {result.id for result in evaluation.results}:
        raise _invalid("CASE_SETS_MUST_MATCH", "$.cases")
    cases = [
        CaseScores(result.id, f"/results/{index}", _ohmu_case(result, by_id[result.id]))
        for index, result in enumerate(evaluation.results)
    ]
    return evaluation.cases_sha256, cases


def _summarize(cases: list[CaseScores], metric: str) -> dict:
    counts = dict.fromkeys(_STATUSES, 0)
    values = []
    for case in cases:
        score = case.scores[metric]
        counts[score.status] += 1
        if score.value is not None:
            values.append(score.value)
    mean = (
        sum(Fraction(str(value)) for value in values) / len(values) if values else None
    )
    return {
        "counts": counts,
        "scored_count": len(values),
        "mean": None if mean is None else float(round(mean, 6)),
    }


def score_artifact(
    text: str,
    *,
    source: Literal["agent-run", "nausicaa", "ohmu"],
    metrics: list[str] | None = None,
    artifact_id: str | None = None,
    mode: NausicaaMode | None = None,
    annotations_text: str | None = None,
) -> dict:
    available = METRICS[source]
    selected = tuple(metrics) if metrics is not None else available
    if (
        not selected
        or len(set(selected)) != len(selected)
        or set(selected) - set(available)
    ):
        raise _invalid("INVALID_METRIC_SELECTION")
    selected = tuple(metric for metric in available if metric in selected)

    evidence = {"format": source, "input_sha256": _sha256(text)}
    if source == "agent-run":
        run = parse_agent_run(text, "$")
        artifact_id = run.artifact_id
        evidence["suite_id"] = run.suite_id
        cases = [
            CaseScores(
                case.id,
                f"/cases/{index}",
                _task_scores(case.reported_success, case.verified_success),
            )
            for index, case in enumerate(run.cases)
        ]
    elif source == "nausicaa":
        if mode is None:
            raise _invalid("NAUSICAA_MODE_MISSING")
        evidence["mode"] = mode
        cases = _nausicaa_scores(text, mode)
    else:
        if annotations_text is None:
            raise _invalid("OHMU_ANNOTATIONS_REQUIRED")
        evidence["annotations_sha256"], cases = _ohmu_scores(text, annotations_text)

    if (
        artifact_id is None
        or not artifact_id.strip()
        or artifact_id != artifact_id.strip()
    ):
        raise _invalid("INVALID_ARTIFACT_ID")
    if len({case.id for case in cases}) != len(cases):
        raise _invalid("DUPLICATE_CASE_ID")
    cases.sort(key=lambda case: case.id)

    scorers = {metric: f"{source}/{metric}@1" for metric in selected}
    return {
        "score_report_schema_version": 1,
        "evidence": {"artifact_id": artifact_id, **evidence},
        "scorers": scorers,
        "scorers_sha256": json_sha256(scorers),
        "case_count": len(cases),
        "summary": {metric: _summarize(cases, metric) for metric in selected},
        "cases": [
            {
                "id": case.id,
                "evidence_ref": case.evidence_ref,
                "scores": {metric: asdict(case.scores[metric]) for metric in selected},
            }
            for case in cases
        ],
    }

"""Rescore a captured suite run and aggregate trial metrics by slice."""

from dataclasses import asdict
from fractions import Fraction

from .scoring import CaseScores, Score, _binary, _summarize, _task_scores
from .suites import (
    RunBudget, Suite, SuiteCase, SuiteRun, Trial, digest, invalid,
    read_contract, read_suite, suite_digest,
)
from .validation import json_sha256

SUITE_METRICS = (
    "verified_success",
    "no_false_completion",
    "tool_expectations",
    "trial_completed",
    "latency_ms",
    "cost_microusd",
)


def _read_trials(text: str, suite: Suite) -> SuiteRun:
    run = read_contract(text, SuiteRun, "INVALID_SUITE_RUN")
    if run.suite_id != suite.id or run.suite_sha256 != suite_digest(suite):
        raise invalid("SUITE_DIGEST_MISMATCH")
    expected = [
        (case.id, trial)
        for trial in range(1, suite.trials + 1)
        for case in suite.cases
    ]
    if [(trial.case_id, trial.trial) for trial in run.trials] != expected:
        raise invalid("TRIAL_PLAN_MISMATCH")
    budget = RunBudget.from_limits(suite.limits)
    for trial in run.trials:
        if budget.can_start() == (trial.status == "budget_exhausted"):
            raise invalid("TRIAL_BUDGET_MISMATCH")
        if trial.status != "budget_exhausted":
            budget.charge(trial.response)
            if budget.exceeded != (trial.status == "cost_exceeded"):
                raise invalid("TRIAL_BUDGET_MISMATCH")
    return run


def _trial_scores(
    trial: Trial, case: SuiteCase, expected_output: str
) -> dict[str, Score]:
    if trial.status == "budget_exhausted":
        return {metric: Score("unscored", None, trial.status) for metric in SUITE_METRICS}

    response = trial.response
    scores = {
        "trial_completed": _binary(
            trial.status == "completed", "trial_completed", trial.status
        ),
        "latency_ms": Score("pass", trial.latency_ms, "measured"),
        "cost_microusd": (
            Score("pass", response.cost_microusd, "measured")
            if response is not None
            else Score("unscored", None, "missing_cost")
        ),
    }
    if trial.status == "completed":
        assert response is not None
        scores.update(
            _task_scores(response.reported_success, trial.output_sha256 == expected_output)
        )
    else:
        scores.update(
            {metric: Score("error", None, trial.status) for metric in
             ("verified_success", "no_false_completion")}
        )

    expected_tools = case.acceptance.tool_calls
    if expected_tools is None:
        tools = Score("unscored", None, "no_tool_expectations")
    elif response is None:
        tools = Score("error", None, trial.status)
    elif response.tool_calls is None:
        tools = Score("unscored", None, "missing_tool_counts")
    else:
        tools = _binary(
            response.tool_calls == expected_tools,
            "tool_expectations_met",
            "tool_expectations_missed",
        )
    scores["tool_expectations"] = tools
    return scores


def statistic(values: list[int], name: str) -> Fraction | None:
    if not values:
        return None
    if name == "p95":
        return Fraction(sorted(values)[(95 * len(values) + 99) // 100 - 1])
    return Fraction(sum(values), len(values))


def _summary(rows: list[CaseScores], metrics: tuple[str, ...]) -> dict:
    result = {metric: _summarize(rows, metric) for metric in metrics}
    if "latency_ms" in result:
        values = [
            score.value for row in rows
            if (score := row.scores["latency_ms"]).value is not None
        ]
        p95 = statistic(values, "p95")
        result["latency_ms"]["p95"] = None if p95 is None else int(p95)
    if "cost_microusd" in result:
        result["cost_microusd"]["observed_total"] = sum(
            row.scores["cost_microusd"].value or 0 for row in rows
        )
    return result


def score_suite_run(
    text: str, suite_text: str, metrics: list[str] | None = None
) -> dict:
    suite = read_suite(suite_text)
    run = _read_trials(text, suite)
    selected = tuple(metrics) if metrics is not None else SUITE_METRICS
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(SUITE_METRICS):
        raise invalid("INVALID_METRIC_SELECTION")
    selected = tuple(metric for metric in SUITE_METRICS if metric in selected)

    cases = {case.id: case for case in suite.cases}
    expected_outputs = {
        case.id: digest(case.acceptance.exact_text.encode("utf-8")) for case in suite.cases
    }
    groups: dict[str, list[CaseScores]] = {}
    rows = []
    for index, trial in enumerate(run.trials):
        case = cases[trial.case_id]
        scores = _trial_scores(trial, case, expected_outputs[case.id])
        row = CaseScores(trial.case_id, f"/trials/{index}", scores)
        rows.append(row)
        for name in case.slices:
            groups.setdefault(name, []).append(row)

    scorers = {metric: f"suite-run/{metric}@1" for metric in selected}
    return {
        "suite_score_report_schema_version": 1,
        "evidence": {
            "format": "suite-run",
            "input_sha256": digest(text.encode("utf-8")),
            "suite_sha256": run.suite_sha256,
            "artifact_id": run.artifact_id,
            "suite_id": suite.id,
            "mode": suite.mode,
            "model": suite.model.model_dump(mode="json"),
            "harness": suite.harness.model_dump(mode="json"),
            "environment": suite.environment.model_dump(mode="json"),
        },
        "scorers": scorers,
        "scorers_sha256": json_sha256(scorers),
        "trial_count": len(rows),
        "case_count": len(cases),
        "summary": _summary(rows, selected),
        "slices": {
            name: {
                "trial_count": len(group),
                "case_count": len({row.id for row in group}),
                "summary": _summary(group, selected),
            }
            for name, group in sorted(groups.items())
        },
        "trials": [
            {
                "case_id": trial.case_id,
                "trial": trial.trial,
                "status": trial.status,
                "slices": cases[trial.case_id].slices,
                "evidence_ref": row.evidence_ref,
                "scores": {metric: asdict(row.scores[metric]) for metric in selected},
            }
            for trial, row in zip(run.trials, rows, strict=True)
        ],
    }

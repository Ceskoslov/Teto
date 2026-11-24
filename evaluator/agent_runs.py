"""Versioned agent evidence, the Nausicaa importer, and the regression policy."""

from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from .models import Identifier, InputModel
from .validation import InputValidationError, ValidationIssue, json_sha256, parse_json

RULE_VERSION = "agent-regression-v1"
SUITE = "task-baseline-v1"
_INJECTED_INTERRUPTION = (
    "injected interruption at execution boundary; recovered without replay"
)

VersionOne = Annotated[int, Field(ge=1, le=1)]
FixtureDigest = Annotated[
    str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
]
NausicaaMode = Literal["core_only", "task"]


class AgentCase(InputModel):
    id: Identifier
    fixture_sha256: FixtureDigest
    reported_success: bool
    verified_success: bool


class AgentRun(InputModel):
    schema_version: VersionOne
    suite_id: Identifier
    artifact_id: Identifier
    cases: list[AgentCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_ids(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise PydanticCustomError("DUPLICATE_CASE_ID", "DUPLICATE_CASE_ID")
        return self


class _NausicaaFixture(InputModel):
    version: VersionOne
    id: Identifier
    split: Identifier
    objective: str
    initial: str
    expected: str
    proposals: list[str] = Field(min_length=1)
    max_attempts: int = Field(gt=0)
    deny_write: bool
    interrupt_execution: bool


# The upstream report also contains local paths, timings, and other diagnostics.
class NausicaaRun(InputModel, extra="allow"):
    fixture: _NausicaaFixture
    mode: NausicaaMode
    reported_success: bool
    verified_success: bool
    live_model: bool
    turn_errors: list[str]

    def has_unexpected_turn_errors(self) -> bool:
        return bool(self.turn_errors) and (
            not self.fixture.interrupt_execution
            or self.turn_errors != [_INJECTED_INTERRUPTION]
        )


class _NausicaaSummary(InputModel, extra="ignore"):
    schema_version: VersionOne
    fixture_suite: Literal["task-baseline-v1"]
    live_config: None
    provider_settings: None
    skipped_fault_injection_fixtures: list[str] = Field(max_length=0)
    runs: list[NausicaaRun] = Field(min_length=1)


def _invalid(issue: str, path: str = "$") -> InputValidationError:
    return InputValidationError("INVALID_INPUT", [ValidationIssue(path, issue)])


def _run_issues(exception: ValidationError, path: str) -> InputValidationError:
    for error in exception.errors(include_input=False, include_context=False):
        if error["type"] == "DUPLICATE_CASE_ID":
            return _invalid("DUPLICATE_CASE_ID", f"{path}.cases")
    return _invalid("INVALID_AGENT_RUN", path)


def parse_agent_run(text: str, side: str) -> AgentRun:
    data = parse_json(text)
    try:
        return AgentRun.model_validate(data)
    except ValidationError as exception:
        raise _run_issues(exception, side) from None


def read_nausicaa_runs(text: str, mode: NausicaaMode) -> list[tuple[int, NausicaaRun]]:
    data = parse_json(text)
    try:
        summary = _NausicaaSummary.model_validate(data)
    except ValidationError:
        raise _invalid("UNSUPPORTED_NAUSICAA_SUMMARY") from None
    if any(run.live_model for run in summary.runs):
        raise _invalid("UNSUPPORTED_NAUSICAA_SUMMARY")
    selected = [
        (index, run) for index, run in enumerate(summary.runs) if run.mode == mode
    ]
    if not selected:
        raise _invalid("NAUSICAA_MODE_MISSING", "$.runs")
    return selected


def import_nausicaa(text: str, mode: NausicaaMode, artifact_id: str) -> AgentRun:
    cases = []
    for _, run in read_nausicaa_runs(text, mode):
        if run.has_unexpected_turn_errors():
            raise _invalid("NAUSICAA_TURN_FAILED", "$.runs")
        cases.append(
            AgentCase(
                id=run.fixture.id,
                fixture_sha256=json_sha256(run.fixture.model_dump(mode="json")),
                reported_success=run.reported_success,
                verified_success=run.verified_success,
            )
        )
    try:
        return AgentRun(
            schema_version=1,
            suite_id=SUITE,
            artifact_id=artifact_id,
            cases=sorted(cases, key=lambda case: case.id),
        )
    except ValidationError as exception:
        raise _run_issues(exception, "$") from None


def compare_runs(baseline: AgentRun, candidate: AgentRun) -> dict:
    if baseline.artifact_id == candidate.artifact_id:
        raise _invalid("ARTIFACT_IDS_MUST_DIFFER")
    if baseline.suite_id != candidate.suite_id:
        raise _invalid("SUITE_IDS_MUST_MATCH")

    baseline_cases = {case.id: case for case in baseline.cases}
    candidate_cases = {case.id: case for case in candidate.cases}
    if baseline_cases.keys() != candidate_cases.keys():
        raise _invalid("CASE_SETS_MUST_MATCH")

    results = []
    for case_id in sorted(baseline_cases):
        before = baseline_cases[case_id]
        after = candidate_cases[case_id]
        if before.fixture_sha256 != after.fixture_sha256:
            raise _invalid("FIXTURE_DIGESTS_MUST_MATCH")

        failed_gates = []
        if before.verified_success and not after.verified_success:
            failed_gates.append("verified_success_regressed")
        if after.reported_success and not after.verified_success:
            failed_gates.append("candidate_false_completion")

        results.append(
            {
                "id": case_id,
                "baseline": {
                    "reported_success": before.reported_success,
                    "verified_success": before.verified_success,
                },
                "candidate": {
                    "reported_success": after.reported_success,
                    "verified_success": after.verified_success,
                },
                "failed_gates": failed_gates,
            }
        )

    decision = "hold" if any(case["failed_gates"] for case in results) else "release"
    return {
        "report_schema_version": 1,
        "rule_version": RULE_VERSION,
        "suite_id": baseline.suite_id,
        "evidence": {
            side: {
                "artifact_id": run.artifact_id,
                "agent_run_sha256": json_sha256(run.model_dump(mode="json")),
            }
            for side, run in (("baseline", baseline), ("candidate", candidate))
        },
        "recommendation": {"decision": decision},
        "cases": results,
    }

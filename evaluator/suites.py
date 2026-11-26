"""Suite v1 and the evidence produced by its subprocess runner."""

import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from .agent_runs import FixtureDigest, VersionOne
from .models import Identifier, InputModel
from .validation import InputValidationError, ValidationIssue, json_sha256, parse_json

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
FileName = Annotated[str, Field(pattern=r"^[a-z0-9_][a-z0-9_.-]{0,127}$")]
FixtureText = Annotated[str, Field(max_length=MAX_OUTPUT_BYTES // 4)]
MicroUSD = Annotated[int, Field(ge=0, le=10**12)]
ToolCounts = dict[Identifier, Annotated[int, Field(ge=0, le=10**9)]]
TrialStatus = Literal[
    "completed", "timed_out", "harness_failed", "invalid_response",
    "output_unreadable", "cost_exceeded", "budget_exhausted",
]


class Environment(InputModel):
    platform: Identifier
    machine: Identifier
    python: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    revision: Identifier


class ModelPin(InputModel):
    id: Identifier
    revision: Identifier


class HarnessPin(InputModel):
    id: Identifier
    sha256: FixtureDigest


class Acceptance(InputModel):
    output: FileName
    exact_text: FixtureText
    tool_calls: ToolCounts | None = None


class SuiteCase(InputModel):
    id: Identifier
    prompt: Identifier
    slices: list[Identifier] = Field(min_length=1, max_length=32)
    files: dict[FileName, FixtureText] = Field(max_length=32)
    acceptance: Acceptance

    def model_post_init(self, context: object) -> None:
        if len(set(self.slices)) != len(self.slices):
            raise ValueError("duplicate case slice")
        if self.acceptance.output in self.files:
            raise ValueError("acceptance output must be created during the trial")


class Limits(InputModel):
    trial_timeout_ms: int = Field(gt=0, le=3_600_000)
    trial_cost_microusd: MicroUSD
    total_cost_microusd: MicroUSD


class Suite(InputModel):
    schema_version: VersionOne
    id: Identifier
    mode: Literal["offline", "live"]
    model: ModelPin
    harness: HarnessPin
    environment: Environment
    trials: int = Field(gt=0, le=100)
    limits: Limits
    cases: list[SuiteCase] = Field(min_length=1, max_length=1000)

    def model_post_init(self, context: object) -> None:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate case ID")
        if self.limits.trial_cost_microusd > self.limits.total_cost_microusd:
            raise ValueError("total cost limit cannot fund a single trial")
        if self.mode == "live" and not self.limits.trial_cost_microusd:
            raise ValueError("live suites require a positive cost reservation")


class HarnessResponse(InputModel):
    schema_version: VersionOne
    reported_success: bool
    cost_microusd: MicroUSD
    tool_calls: ToolCounts | None = None


class Trial(InputModel):
    case_id: Identifier
    trial: int = Field(gt=0)
    status: TrialStatus
    latency_ms: int | None = Field(ge=0)
    response: HarnessResponse | None
    output_sha256: FixtureDigest | None

    def model_post_init(self, context: object) -> None:
        responded = self.status in {"completed", "cost_exceeded", "output_unreadable"}
        if responded != (self.response is not None):
            raise ValueError("response does not match trial status")
        if (self.status == "budget_exhausted") != (self.latency_ms is None):
            raise ValueError("latency does not match trial status")
        if self.status != "completed" and self.output_sha256 is not None:
            raise ValueError("output does not match trial status")


class SuiteRun(InputModel):
    schema_version: VersionOne
    suite_id: Identifier
    suite_sha256: FixtureDigest
    artifact_id: Identifier
    trials: list[Trial] = Field(min_length=1)


@dataclass
class RunBudget:
    remaining: int
    reservation: int
    exceeded: bool = False

    @classmethod
    def from_limits(cls, limits: Limits) -> "RunBudget":
        return cls(limits.total_cost_microusd, limits.trial_cost_microusd)

    def can_start(self) -> bool:
        return not self.exceeded and self.remaining >= self.reservation

    def charge(self, response: HarnessResponse | None) -> None:
        cost = response.cost_microusd if response else self.reservation
        self.remaining -= cost
        self.exceeded = cost > self.reservation


def invalid(code: str, path: str = "$") -> InputValidationError:
    return InputValidationError("INVALID_INPUT", [ValidationIssue(path, code)])


def read_contract[T: InputModel](text: str, contract: type[T], issue: str) -> T:
    data = parse_json(text)
    try:
        return contract.model_validate(data)
    except ValidationError:
        raise invalid(issue) from None


def read_suite(text: str) -> Suite:
    return read_contract(text, Suite, "INVALID_SUITE")


def suite_digest(suite: Suite) -> str:
    return json_sha256(suite.model_dump(mode="json"))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

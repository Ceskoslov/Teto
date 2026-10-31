from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .text import normalize_text


def validate_identifier(value: str) -> str:
    if not value.strip():
        raise PydanticCustomError("BLANK_IDENTIFIER", "BLANK_IDENTIFIER")
    if value != value.strip():
        raise PydanticCustomError("IDENTIFIER_WHITESPACE", "IDENTIFIER_WHITESPACE")
    return value


Identifier = Annotated[str, AfterValidator(validate_identifier)]

NonNegativeNumber = (
    Annotated[int, Field(ge=0)] | Annotated[float, Field(ge=0, allow_inf_nan=False)]
)


class InputModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ResponseObservation(InputModel):
    output: str
    latency_ms: NonNegativeNumber
    cost_usd: NonNegativeNumber


class Case(InputModel):
    id: Identifier
    prompt: str
    expected_terms: list[str]
    forbidden_terms: list[str]
    baseline: ResponseObservation
    candidate: ResponseObservation

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError("BLANK_PROMPT", "BLANK_PROMPT")
        return value

    @field_validator("expected_terms", "forbidden_terms")
    @classmethod
    def validate_terms(cls, values: list[str]) -> list[str]:
        normalized = [normalize_text(value) for value in values]
        if any(not value for value in normalized):
            raise PydanticCustomError("BLANK_TERM", "BLANK_TERM")
        if len(normalized) != len(set(normalized)):
            raise PydanticCustomError("DUPLICATE_TERM", "DUPLICATE_TERM")
        # Preserve original evidence and annotation indices.
        return values

    @model_validator(mode="after")
    def validate_annotation_overlap(self) -> Self:
        expected = {normalize_text(term) for term in self.expected_terms}
        forbidden = {normalize_text(term) for term in self.forbidden_terms}
        if expected & forbidden:
            raise PydanticCustomError("OVERLAPPING_TERMS", "OVERLAPPING_TERMS")
        return self


class EvaluationRequest(InputModel):
    product: Identifier
    dataset_version: Identifier
    baseline_artifact_id: Identifier
    candidate_artifact_id: Identifier
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError("DUPLICATE_CASE_ID", "DUPLICATE_CASE_ID")
        if self.baseline_artifact_id == self.candidate_artifact_id:
            raise PydanticCustomError(
                "ARTIFACT_IDS_MUST_DIFFER", "ARTIFACT_IDS_MUST_DIFFER"
            )
        if not any(case.expected_terms for case in self.cases):
            raise PydanticCustomError(
                "QUALITY_ANNOTATION_REQUIRED", "QUALITY_ANNOTATION_REQUIRED"
            )
        return self

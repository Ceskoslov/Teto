import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from .models import EvaluationRequest


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    code: str


class InputValidationError(Exception):
    def __init__(
        self,
        code: str,
        issues: list[ValidationIssue],
    ) -> None:
        super().__init__(code)
        self.code = code
        self.issues = tuple(issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "issues": [
                    {"path": issue.path, "code": issue.code} for issue in self.issues
                ],
            }
        }


class _JSONContractError(ValueError):
    """Reject ambiguous or nonstandard JSON."""


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result = {}

    for key, value in pairs:
        if key in result:
            raise _JSONContractError("DUPLICATE_JSON_KEY")

        result[key] = value

    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise _JSONContractError("NONFINITE_JSON_NUMBER")


# Only fixed model error codes may leave the validation boundary.
_MODEL_CODES = frozenset(
    {
        "BLANK_IDENTIFIER",
        "IDENTIFIER_WHITESPACE",
        "BLANK_PROMPT",
        "BLANK_TERM",
        "DUPLICATE_TERM",
        "OVERLAPPING_TERMS",
        "DUPLICATE_CASE_ID",
        "ARTIFACT_IDS_MUST_DIFFER",
        "QUALITY_ANNOTATION_REQUIRED",
    }
)


_PYDANTIC_CODES = {
    "missing": "REQUIRED_FIELD",
    "extra_forbidden": "UNKNOWN_FIELD",
    "model_type": "OBJECT_REQUIRED",
    "string_type": "STRING_REQUIRED",
    "list_type": "LIST_REQUIRED",
    "int_type": "NUMBER_REQUIRED",
    "float_type": "NUMBER_REQUIRED",
    "greater_than_equal": "NON_NEGATIVE_REQUIRED",
    "finite_number": "FINITE_NUMBER_REQUIRED",
    "too_short": "TOO_FEW_ITEMS",
}


# Exclude unknown field names and union labels from error paths.
_FIELDS = {
    "request": {
        "product": "leaf",
        "dataset_version": "leaf",
        "baseline_artifact_id": "leaf",
        "candidate_artifact_id": "leaf",
        "cases": "cases",
    },
    "case": {
        "id": "leaf",
        "prompt": "leaf",
        "expected_terms": "terms",
        "forbidden_terms": "terms",
        "baseline": "observation",
        "candidate": "observation",
    },
    "observation": {
        "output": "leaf",
        "latency_ms": "leaf",
        "cost_usd": "leaf",
    },
}


def _safe_path(location: tuple[Any, ...]) -> str:
    state = "request"
    path = ""

    for part in location:
        if state in {"cases", "terms"}:
            if type(part) is not int or part < 0:
                break

            path += f"[{part}]"
            state = "case" if state == "cases" else "leaf"
            continue

        fields = _FIELDS.get(state, {})

        if not isinstance(part, str) or part not in fields:
            break

        path = f"{path}.{part}" if path else part
        state = fields[part]

    return path or "$"


def _safe_error_code(error: ErrorDetails) -> str:
    error_type = error["type"]

    if error_type in _MODEL_CODES:
        return error_type
    return _PYDANTIC_CODES.get(error_type, "INVALID_VALUE")


def _model_issues(
    exception: ValidationError,
) -> list[ValidationIssue]:
    issues = []
    seen = set()

    errors = exception.errors(
        include_input=False,
        include_context=False,
        include_url=False,
    )

    for error in errors:
        issue = ValidationIssue(
            path=_safe_path(error["loc"]),
            code=_safe_error_code(error),
        )

        # Numeric union branches can produce identical issues.
        if issue not in seen:
            seen.add(issue)
            issues.append(issue)

    return issues


def validate_request(text: str) -> EvaluationRequest:
    """Parse and validate JSON for both adapters."""
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
        # Reject lone surrogates before hashing or serializing a report.
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except _JSONContractError as exception:
        raise InputValidationError(
            code="INVALID_JSON",
            issues=[
                ValidationIssue(path="$", code=str(exception)),
            ],
        ) from None
    except (ValueError, UnicodeError, RecursionError):
        raise InputValidationError(
            code="INVALID_JSON",
            issues=[
                ValidationIssue(path="$", code="MALFORMED_JSON"),
            ],
        ) from None

    try:
        return EvaluationRequest.model_validate(data)
    except ValidationError as exception:
        raise InputValidationError(
            code="INVALID_INPUT",
            issues=_model_issues(exception),
        ) from None

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class GateDefinition:
    id: str
    metric: str
    operator: str
    # Exactly one limit source is set. Decimal strings avoid float thresholds.
    fixed_limit: str | None = None
    baseline_metric: str | None = None
    multiplier: str = "1.00"


@dataclass(frozen=True)
class ReleasePolicy:
    id: str
    gates: tuple[GateDefinition, ...]
    matching: str = "nfkc-casefold-collapse-whitespace-substring-v1"
    quality: str = "expected-term-coverage-macro-mean-exclude-unannotated-v1"
    safety: str = "any-forbidden-match-per-case-v1"
    latency: str = "nearest-rank-p95-v1"
    cost: str = "arithmetic-mean-usd-per-case-v1"
    decision: str = "all-gates-must-pass-v1"
    arithmetic: str = "exact-rational-before-rounding-v1"
    reporting: str = "six-decimal-places-ties-to-even-v1"

    def snapshot(self) -> dict:
        """Return a fresh JSON-compatible snapshot for reports and hashing."""
        snapshot = asdict(self)
        snapshot["gates"] = list(snapshot["gates"])
        return snapshot


# Gate order determines report order; policy changes require a new ID.
DEFAULT_POLICY = ReleasePolicy(
    id="offline-terms-v1",
    gates=(
        GateDefinition(
            "candidate_safety",
            "candidate.safety_violation_case_count",
            "==",
            fixed_limit="0",
        ),
        GateDefinition(
            "quality_floor",
            "candidate.mean_quality",
            ">=",
            fixed_limit="0.80",
        ),
        GateDefinition(
            "quality_non_regression",
            "candidate.mean_quality",
            ">=",
            baseline_metric="baseline.mean_quality",
        ),
        GateDefinition(
            "case_quality_non_regression",
            "quality_regressed_case_count",
            "==",
            fixed_limit="0",
        ),
        GateDefinition(
            "latency_budget",
            "candidate.p95_latency_ms",
            "<=",
            baseline_metric="baseline.p95_latency_ms",
            multiplier="1.20",
        ),
        GateDefinition(
            "cost_budget",
            "candidate.mean_cost_usd",
            "<=",
            baseline_metric="baseline.mean_cost_usd",
            multiplier="1.10",
        ),
    ),
)

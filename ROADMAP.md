# Teto roadmap

Teto is evolving from a regression gate into an agent evaluation pipeline. The pipeline defines a suite, runs or imports evidence, scores individual cases, aggregates results, then applies a release policy where required. Execution, evidence, scoring, and policy have separate versions so a captured run can be scored again without executing the agent.

## v0.1 · Regression gate

Shipped. The CLI imports Nausicaa's offline task suite and compares paired Agent Run v1 artifacts. CI checks the policy in both directions against a pinned Nausicaa checkout and archives the evidence.

## v0.2 · Standalone scoring

- Add `teto score` and a versioned report for one artifact. Record the scorer version, input digests, per-case results, and metric coverage. Keep scored failures, unavailable evidence, and execution errors distinct.
- Score task success, false completion, and receipt expectations from Nausicaa. Score quote verification, expected-source coverage, and abstention from Ohmu. Reuse host verification when its source evidence is unavailable to Teto.
- Retain `teto compare`, Agent Run v1, and the text evaluator. Verify both scoring paths offline in CI. The same evidence and scorer selection must produce a byte-identical report.

## v0.3 · Reproducible runs and policy

- Define versioned suites with fixtures, acceptance criteria, slices, and pinned model, harness, and environment metadata. Add `teto run` with separate workspaces, time and cost limits, and repeated trials.
- Report task quality, tool behavior, reliability, latency, and cost by slice. Track sample counts and missing evidence. Keep deterministic PR checks small and run live suites on a separate schedule.
- Introduce versioned standalone thresholds alongside paired regression. Use hard gates for deterministic critical checks, calibrated thresholds for well-sampled metrics, and observation for exploratory metrics. Calibrate model graders against human labels before they affect a release decision.

## v0.4 · Shared evaluation service

Run the same scoring and policy logic behind an asynchronous job API. One path launches a run through a worker, and another accepts completed evidence from a Nausicaa host plugin or an Ohmu exporter. Both share idempotent submission, job status, evidence storage, and report retrieval.

## Later · Production feedback

Use approved production samples and human feedback to expand offline suites. Track canary success, errors, and cost separately from offline results, and promote reviewed failures into versioned test cases.

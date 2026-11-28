# Teto roadmap

Teto is evolving from a regression gate into an agent evaluation pipeline. The pipeline defines a suite, runs or imports evidence, scores individual cases, aggregates results, then applies a release policy where required. Execution, evidence, scoring, and policy have separate versions so a captured run can be scored again without executing the agent.

## v0.1 · Regression gate

Shipped. The CLI imports Nausicaa's offline task suite and compares paired Agent Run v1 artifacts. CI checks the policy in both directions against a pinned Nausicaa checkout and archives the evidence.

## v0.2 · Standalone scoring

Shipped. The CLI scores saved Nausicaa, Ohmu, and Agent Run v1 evidence. The report records the original evidence digests, selected scorer versions, and per-metric coverage.

- Add `teto score` and a versioned report for one artifact. Record the scorer version, input digests, per-case results, and metric coverage. Keep scored failures, unavailable evidence, and execution errors distinct.
- Score task success, false completion, and receipt expectations from Nausicaa. Score quote verification, expected-source coverage, and abstention from Ohmu. Reuse host verification when its source evidence is unavailable to Teto.
- Retain `teto compare`, Agent Run v1, and the text evaluator. Verify both scoring paths offline in CI. The same evidence and scorer selection must produce a byte-identical report.

## v0.3 · Reproducible runs and policy

Shipped. Suite v1 pins the fixtures, acceptance criteria, slices, model, harness, and environment. `teto run` captures repeated trials in fresh workspaces with timeout and cost limits. `teto score suite-run` reports quality, tool behavior, reliability, latency, cost, and coverage by slice. PR CI runs a scripted smoke suite, and live suites have a separate opt-in schedule.

`teto gate` applies a versioned standalone policy with critical checks, calibrated thresholds, and observation alongside the existing paired regression gate. Model graders require human-label calibration before entering a release policy.

## v0.4 · Shared evaluation service

Shipped. The opt-in Jobs API v1 queues worker launches from registered suite bundles and accepts completed Nausicaa, Ohmu, Agent Run v1, and Suite Run v1 evidence. Both paths share idempotent submission, durable status, evidence storage, and report retrieval. The worker uses the CLI's scorers and applies the standalone policy to suite evidence when supplied. CI compares the HTTP service's reports byte for byte with the CLI.

## Later · Production feedback

Use approved production samples and human feedback to expand offline suites. Track canary success, errors, and cost separately from offline results, and promote reviewed failures into versioned test cases.

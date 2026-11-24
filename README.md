# Teto — Agent Evaluation Pipeline

Teto scores saved agent evidence and compares baseline and candidate runs for CI. Your harness runs the agents and captures the evidence, then Teto scores it offline. The text evaluator also supports paired LLM responses through the CLI and FastAPI.

## Standalone scoring

Install with Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run --locked teto --help
```

Score the included synthetic Ohmu evidence with its original annotations:

```bash
evidence_dir="$(mktemp -d)"
uv run --locked teto score ohmu \
  --input examples/ohmu-evaluation.json --cases examples/ohmu-cases.json \
  --artifact-id ohmu-example --output "$evidence_dir/ohmu-scores.json"
```

Ohmu's evaluation script emits `evaluation.json`. Teto checks that the original annotation file matches its recorded SHA-256 and that both files contain the same case IDs. It recomputes expected-source recall using the cited source IDs and checks abstention against the annotations. The host-acceptance and quote-verification metrics use Ohmu's verdicts. A rejected answer counts against host acceptance and leaves the answer-dependent metrics unscored. An accepted abstention without quotes leaves quote verification unscored. Quote verification reflects the host's evidence checks. Semantic support requires a separate grader.

For Nausicaa, run the suite in the next section and score either mode directly:

```bash
uv run --locked teto score nausicaa \
  --input "$evidence_dir/nausicaa/summary.json" --mode task \
  --artifact-id nausicaa-task --output "$evidence_dir/nausicaa-scores.json"
```

This scores verified task success, false completion, and fixture-specific denial and unknown-receipt counts. The planned interruption is part of the suite. Unexpected turn errors yield per-case scoring errors. Use `teto score agent-run --input candidate.json --output scores.json` to score a generic Agent Run v1 artifact without receipt checks.

### Score Report v1

- The report identifies the input by its byte-level SHA-256, records scorer IDs and versions, and points to each source case with a JSON Pointer. It contains metric values and fixed reason codes. The original source files hold the prompts, answers, and raw logs.
- Each metric reports `pass`, `fail`, `unscored` for unavailable evidence or annotations, or `error` for invalid measurement or failed execution. Summary means use scored cases and report the count for every status. Repeat `--metric` to select a subset of scorers.
- `teto score` returns `0` after writing a report, including reports with negative verdicts or case errors. It returns `2` when the input cannot be scored or the report cannot be written. Use `teto compare` for the paired release gate.

## Agent regression quick start

The [Nausicaa task suite](https://github.com/Ceskoslov/Nausicaa/tree/main/crates/eval) exercises the task acceptance loop with a scripted model. To produce a sample comparison, use a sibling Nausicaa checkout with its Rust dependencies cached. Run these commands from the Teto checkout.

```bash
evidence_dir="$(mktemp -d)"
cargo run --manifest-path ../Nausicaa/Cargo.toml -p agent-harness-eval \
  --locked --offline -- --output "$evidence_dir/nausicaa"

uv run --locked teto import-nausicaa \
  --input "$evidence_dir/nausicaa/summary.json" --mode core_only \
  --artifact-id nausicaa-core --output "$evidence_dir/baseline.json"
uv run --locked teto import-nausicaa \
  --input "$evidence_dir/nausicaa/summary.json" --mode task \
  --artifact-id nausicaa-task --output "$evidence_dir/candidate.json"

uv run --locked teto compare \
  --baseline "$evidence_dir/baseline.json" \
  --candidate "$evidence_dir/candidate.json" \
  --output "$evidence_dir/report.json"
```

The scripted comparison returns `release`. Swapping the baseline and candidate returns `hold`. The importer reads offline `task-baseline-v1` summaries. It accepts the suite's deliberate interruption and rejects unexpected turn errors in the selected mode. The output contains fixture digests and verdicts, while prompts, source text, and event logs stay in the original evidence.

### Agent Run v1

Any harness can emit this UTF-8 JSON directly. The digest below is illustrative.

```json
{
  "schema_version": 1,
  "suite_id": "coding-smoke-v1",
  "artifact_id": "candidate-build-42",
  "cases": [
    {
      "id": "repair",
      "fixture_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "reported_success": true,
      "verified_success": true
    }
  ]
}
```

- Give each run a distinct build ID and use the same suite ID, case IDs, and fixture digests on both sides. Case order can differ. Hash the immutable fixture specification, including acceptance criteria and the initial state, before running either version.
- Generate the artifact in the trusted test host. Set `reported_success` from the runtime's completion status and `verified_success` from an independent assertion over the output or result. Emit a complete suite or fail the upstream job if evidence is missing.
- The `agent-regression-v1` policy holds if any baseline-verified case fails candidate verification, or if the candidate reports completion for any unverified case. Reports list each failed gate and include SHA-256 digests of both validated run objects serialized as compact UTF-8 JSON with sorted object keys and original array order.

### CI integration

Run the baseline and candidate against the same pinned suite, save both artifacts, then call `teto compare` with a fresh report path. Treat exit `0` as release, `1` as a failed regression gate, and `2` as an input or execution error. Report writes are atomic. On GitHub Actions, use a separate artifact upload step with `if: always()` to retain reports on a hold. Archive the original evidence with access appropriate to its contents.

The [Teto workflow](.github/workflows/ci.yml) runs the unit tests, compares a pinned Nausicaa suite in both directions, and scores both Nausicaa and the synthetic Ohmu example. It archives the reports and scripted evidence.

See the [roadmap](ROADMAP.md) for the standalone scoring workflow, reproducible runs, and shared service.

## Text response evaluation

Run commands from the project directory on Linux/WSL. Docker commands require a running Docker daemon. On WSL, enable Docker Desktop integration if using Docker Desktop.

```bash
uv sync --locked
uv run python -m evaluator evaluate --input examples/input.json --output examples/report.json
uv run python -m unittest discover -s tests -v
```

CLI exit codes are `0` for release, `1` for hold, and `2` for invalid input or execution failure. The output directory must exist. Writes replace the report atomically; invalid input preserves an existing report. Input and output must refer to different files. `main.py` runs the same CLI.

The supplied fixture produces `hold`: candidate safety, latency and cost gates fail. See the [sample report](examples/report.json) and [expected gate results](examples/input.expected.json).

## REST API and Docker

```bash
docker build -t llm-release-eval:local .
docker run --rm -p 127.0.0.1:8000:8000 llm-release-eval:local
```

In another terminal:

```bash
curl --fail-with-body -H 'Content-Type: application/json' \
  --data-binary @examples/input.json http://127.0.0.1:8000/evaluate
```

For a local API process, use `uv run uvicorn evaluator.api:app --host 127.0.0.1 --port 8000 --no-access-log`. Both release and hold return HTTP `200`. Invalid JSON or UTF-8 returns `400`, unsupported Content-Type returns `415`, invalid fields return `422`, and unexpected failures return a sanitized `500`. CLI and API share strict parsing, including rejection of duplicate JSON keys and nonfinite measurements. Errors contain fixed codes and schema paths; prompts, outputs and exception details are excluded from routine logs and errors. API documentation is available at `/docs`.

The Dockerfile pins base images and installs locked runtime dependencies, then starts the API as a non-root user. Runtime libraries are Pydantic, FastAPI and Uvicorn; unittest and the development dependency httpx2 support offline tests.

## Policy and evidence

The policy is `offline-terms-v1`; every gate must pass:

| Gate              | Requirement                                          |
| ----------------- | ---------------------------------------------------- |
| Candidate safety  | No candidate case matches a forbidden term           |
| Quality floor     | Candidate mean quality is at least 0.80              |
| Aggregate quality | Candidate mean quality is at least baseline          |
| Case quality      | No annotated case loses quality                      |
| Latency budget    | Candidate p95 latency is at most 1.20 times baseline |
| Cost budget       | Candidate mean cost is at most 1.10 times baseline   |

Matching applies Unicode NFKC normalization, case folding and whitespace collapsing before substring matching. Case quality is the fraction of expected terms matched; the aggregate is the mean across annotated cases. Empty expected lists yield null scores and are excluded from quality, while at least one case must have quality annotations. Any forbidden match is a safety violation. Latency uses nearest-rank p95, and cost uses the arithmetic mean over all cases. A zero baseline budget permits only a zero candidate value.

Thresholds are inclusive. Decisions use exact rational arithmetic over validated decimal representations, followed by six-place ties-to-even rounding for display. A failed gate can therefore display an observed value equal to its limit.

Reports identify the product, dataset, artifact IDs, evaluator version and complete policy. They include case findings, baseline/candidate summaries, all gate results and failed gate IDs. Findings reference annotation indices without copying raw text. SHA-256 hashes identify the validated input and policy using sorted-key, compact UTF-8 JSON; array order and integer/float distinctions are preserved. Reports omit timestamps for reproducibility. Policy changes require a new policy ID.

Substring matching is only a proxy for quality and safety: negated forbidden phrases can trigger false positives, and paraphrases can be missed. Empty forbidden lists provide no annotated safety coverage. One latency/cost observation per case gives no statistical confidence; p95 is the maximum for fewer than 20 cases. Thresholds require product-specific calibration.

A `release` recommendation means the candidate passed all gates under the current dataset and matching rules; it does not guarantee quality, safety or performance in real-world use.

## Verification and CI/CD

Offline tests cover scoring, gate boundaries, recommendations, invalid input, deterministic reports, input protection, sanitized errors and CLI/API agreement. Optional live HTTP checks exercise both recommendations and invalid requests:

```bash
uv run python scripts/verify_e2e.py --mode local --output-dir artifacts/e2e/local
uv run python scripts/verify_e2e.py --mode docker --output-dir artifacts/e2e/docker
```

Local mode uses Unix socket descriptor passing and requires Linux/WSL. Docker mode builds and starts its own container. Both retain input fixtures, CLI/API reports and a verification summary; Docker mode also records the image ID.

CI can use the CLI exit code as a release gate and retain the original input, report, policy snapshot and evaluator version together. Keep evidence in access-controlled storage because hashes do not anonymize inputs. The evaluator only recommends a decision; deployment remains a separate CI/CD action.

## Text evaluator provenance

AI coding tools (OpenAI Codex) were used to assist with implementation, code refactoring, test generation, and documentation for the original text evaluator.

The evaluation methodology, release policy, thresholds, and architectural decisions were defined and reviewed by me.

I manually reviewed the generated code and verified its behavior through unit tests, CLI/API consistency checks, and end-to-end execution using the provided sample.

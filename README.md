# Teto — Agent Evaluation Pipeline

Teto runs pinned agent evaluation suites, scores saved evidence offline, and applies standalone or paired release policies. The text evaluator also supports paired LLM responses through the CLI and FastAPI.

## Shared evaluation service

The opt-in Jobs API v1 queues suite runs and scores evidence submitted by evaluation producers. The API and worker share one SQLite database on the same host. This example configures the included scripted suite and starts the API on loopback.

```bash
uv sync --locked --python 3.13.12
jobs_dir="$(mktemp -d)"
mkdir -p "$jobs_dir/bundles/scripted"
cp examples/scripted-suite.json "$jobs_dir/bundles/scripted/suite.json"
cp examples/scripted-harness.py "$jobs_dir/bundles/scripted/harness.py"
cp examples/scripted-policy.json "$jobs_dir/bundles/scripted/policy.json"
export TETO_JOB_DB="$jobs_dir/jobs.sqlite"
job_token="$(uv run --locked python -c 'import secrets; print(secrets.token_urlsafe(32))')"

uv run --locked python -m evaluator.worker &
TETO_JOB_TOKEN="$job_token" TETO_JOB_BUNDLES="$jobs_dir/bundles" \
  uv run --locked uvicorn evaluator.api:app --host 127.0.0.1 --port 8000 --no-access-log &
```

Once the API is up, submit a run and use the returned `status_url` to poll for completion.

```bash
curl --fail-with-body -H "Authorization: Bearer $job_token" \
  -H 'Idempotency-Key: scripted-smoke-1' -H 'Content-Type: application/json' \
  --data '{"schema_version":1,"kind":"run","bundle":"scripted","artifact_id":"scripted-smoke"}' \
  http://127.0.0.1:8000/v1/jobs
```

| Method and path | Result |
| --- | --- |
| `POST /v1/jobs` | Returns `202`, a job ID, and a status URL. An equivalent submission with the same key returns the same ID. Reusing a key with different content returns `409`. |
| `GET /v1/jobs/{id}` | Returns `queued`, `running`, `succeeded`, or `failed`, with UTC timestamps and an error code when applicable. |
| `GET /v1/jobs/{id}/evidence/{name}` | Downloads stored bytes. Names are `input.json`, `cases.json`, `suite.json`, `policy.json`, and `run.json` where available. |
| `GET /v1/jobs/{id}/reports/{name}` | Downloads `score.json` or, when a suite policy is supplied, `gate.json`. A `hold` recommendation is a completed evaluation. |

All job endpoints require the bearer token. Downloads return `404` until the requested file exists. The server snapshots the registered bundle when it queues a run. Keep `suite.json`, `harness.py`, and the optional `policy.json` together under the bundle name. The worker checks the suite and harness pins and applies the same budget and timeout controls as `teto run`.

### Submit completed evidence

Send `{"schema_version":1,"kind":"import","evidence":{...}}` to the same endpoint. Encode each artifact as a JSON string containing its original UTF-8 file text, including line endings. This preserves the input digests and lets clients download the original evidence. The total HTTP request body may be up to 16 MiB.

| `evidence.format` | Other `evidence` fields |
| --- | --- |
| `nausicaa` | `input` is the offline `task-baseline-v1` summary. Include `mode` (`task` or `core_only`) and `artifact_id`. |
| `ohmu` | `input` is the exporter's `evaluation.json`. Include the original annotations as `cases` and an `artifact_id`. |
| `agent-run` | `input` is the Agent Run v1 artifact, which already contains its artifact ID. |
| `suite-run` | `input` is a Suite Run v1 artifact. Include `suite` and optionally `policy` for a gate report. |

For example, package the Ohmu fixture with Python and pipe it to the API.

```bash
uv run --locked python - <<'PY' | curl --fail-with-body \
  -H "Authorization: Bearer $job_token" -H 'Idempotency-Key: ohmu-example-1' \
  -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8000/v1/jobs
import json
from pathlib import Path

print(json.dumps({
    "schema_version": 1,
    "kind": "import",
    "evidence": {
        "format": "ohmu",
        "artifact_id": "ohmu-example",
        "input": Path("examples/ohmu-evaluation.json").read_bytes().decode("utf-8"),
        "cases": Path("examples/ohmu-cases.json").read_bytes().decode("utf-8"),
    },
}))
PY
```

### Run the service

- Set `TETO_JOB_DB` to the same absolute file path for the API and worker. The parent directory must exist and be writable by both processes. Use a private, persistent local volume to retain evidence, reports, and idempotency keys across restarts. The worker supports one active process per database.
- Set `TETO_JOB_TOKEN` on the API process. The token grants access to all jobs and stored evidence. Use HTTPS for remote clients and give the token only to trusted producers and operators. Set `TETO_JOB_BUNDLES` to a directory managed by operators when allowing suite launches. Review registered harnesses and supply any provider credentials through the worker's environment.
- Submit with a new key for a new evaluation and reuse the same key on network retries. Queued work survives restarts. On recovery, the worker marks previously claimed jobs as `failed` with `WORKER_INTERRUPTED` and keeps any stored evidence. Before resubmitting an interrupted live job with a new key, account for any provider spend from the original attempt.

## Reproducible runs and standalone policies

The included smoke suite pins Linux x86-64 and Python 3.13.12. It runs two fixtures twice with a scripted harness. Start with [the suite](examples/scripted-suite.json), [the harness](examples/scripted-harness.py), and [the policy](examples/scripted-policy.json).

```bash
uv sync --locked --python 3.13.12
evidence_dir="$(mktemp -d)"
uv run --locked teto run --require-mode offline --input examples/scripted-suite.json \
  --harness examples/scripted-harness.py --artifact-id scripted-smoke \
  --output "$evidence_dir/run.json"
uv run --locked teto score suite-run --input "$evidence_dir/run.json" \
  --suite examples/scripted-suite.json --output "$evidence_dir/score.json"
uv run --locked teto gate --input "$evidence_dir/run.json" --suite examples/scripted-suite.json \
  --policy examples/scripted-policy.json --output "$evidence_dir/gate.json"
```

### Suite and harness contract

- Suite v1 defines the fixture files, prompt, exact UTF-8 output, optional expected tool counts, slice membership, and trial count. It records the model snapshot, SHA-256 of the Python harness, and environment revision. Teto checks OS, architecture, Python patch version, and the harness digest before launching a trial. The harness owns the pinned model deployment and any dependencies identified by the environment revision.
- Each trial gets a fresh working directory. Teto invokes the pinned script with Python isolated mode and passes one UTF-8 JSON request over stdin. The request includes `schema_version`, `case_id`, `trial`, `prompt`, `output_file`, `mode`, `model`, `environment_revision`, `timeout_ms`, and `cost_limit_microusd`. The harness writes its result to the requested file and emits one JSON object on stdout with `schema_version: 1`, `reported_success`, `cost_microusd`, and optional `tool_calls`. Tool counts come from the trusted harness. Teto independently captures the output digest. Run reviewed harness code and place any execution of untrusted agents inside the harness's own isolation boundary.
- Teto terminates the trial process group on timeout. A microdollar is one millionth of a US dollar. Teto reserves the declared maximum before starting each trial, deducts reported spend, charges the full reservation when spend is unknown, and stops scheduling trials on an overrun. The harness must enforce the passed limit before billable calls, including retries, and report actual spend. A live suite requires a positive reservation.
- Keep the Suite v1 document with the Suite Run v1 evidence. Running again produces new timing observations. Rescoring the same bytes produces a byte-identical report. Reports show overall and per-slice quality, false completion, tool expectations, execution reliability, latency, and observed cost. The per-metric counts distinguish failed checks, execution errors, and unscored evidence. A trial can appear in multiple slices.

### Policy and scheduled live runs

- `teto gate` applies a policy bound to the suite SHA-256. A `critical` check requires every selected trial to pass a binary metric. A `calibrated` check records a calibration ID and evidence SHA-256, and requires minimum scored trials, distinct cases, and coverage before comparing its configured mean or latency p95. An `observe` check records the metric without a vote. Gate inputs currently use fixed verifiers and harness measurements. Model graders require human-label calibration before they can enter a release policy.
- `run` and `score` return `0` after writing evidence or a report. `gate` returns `0` for release and `1` for hold. Invalid inputs and CLI execution failures return `2`. Trial timeouts and harness errors appear in the evidence and can cause a gate to hold.
- PR CI runs the scripted smoke suite. The [live workflow](.github/workflows/live.yml) is scheduled weekly and supports manual dispatch on the default branch. Enable it with repository variable `TETO_LIVE_ENABLED=true`. Set `TETO_LIVE_SUITE` and `TETO_LIVE_HARNESS` to paths committed on the default branch, and set secret `TETO_LIVE_API_KEY` if required by the harness. The workflow archives its run and score reports.

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

See the [roadmap](ROADMAP.md) for evaluation pipeline milestones.

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

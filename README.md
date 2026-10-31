# Offline LLM release evaluation

A deterministic Python evaluator compares already generated baseline and candidate responses on the same cases. Pydantic validates input, a pure core applies the release policy, and thin CLI and FastAPI adapters expose the same report. Evaluation is offline and never deploys anything.

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


"""Run queued evaluations using the same scorers and policy as the CLI."""

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

from .job_store import JobStore
from .runner import run_suite
from .scoring import score_artifact
from .suite_policy import apply_suite_policy, check_policy_suite, get_policy
from .suite_scoring import score_suite_run
from .suites import digest, read_suite, suite_digest
from .validation import InputValidationError


def _json_file(value: dict) -> bytes:
    return (
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


def process_next(store: JobStore) -> bool:
    item = store.claim()
    if item is None:
        return False

    files = item.files
    options = item.options
    outputs = {}
    error = None
    policy = None
    policy_bytes = files.get("policy.json")
    try:
        if options["kind"] == "run":
            suite = read_suite(files["suite.json"].decode("utf-8"))
            if policy_bytes is not None:
                policy = get_policy(policy_bytes.decode("utf-8"))
                slices = {name for case in suite.cases for name in case.slices}
                check_policy_suite(policy, suite_digest(suite), slices)
            run = run_suite(suite, files["harness.py"], options["artifact_id"])
            evidence = _json_file(run.model_dump(mode="json"))
            store.record_run(item.id, evidence)
        else:
            evidence = files["input.json"]

        if "suite.json" in files:
            score = score_suite_run(
                evidence.decode("utf-8"), files["suite.json"].decode("utf-8")
            )
        else:
            cases = files.get("cases.json")
            score = score_artifact(
                evidence.decode("utf-8"),
                source=options["format"],
                artifact_id=options.get("artifact_id"),
                mode=options.get("mode"),
                annotations_text=cases.decode("utf-8") if cases is not None else None,
            )
        outputs["score.json"] = _json_file(score)
        if policy_bytes is not None:
            if policy is None:
                policy = get_policy(policy_bytes.decode("utf-8"))
            gate = apply_suite_policy(score, policy, digest(policy_bytes))
            outputs["gate.json"] = _json_file(gate)
    except InputValidationError as exception:
        error = exception.to_dict()["error"]
    except UnicodeError:
        error = {
            "code": "INVALID_INPUT",
            "issues": [{"path": "$", "code": "UTF8_REQUIRED"}],
        }
    except sqlite3.Error:
        raise
    except Exception:  # noqa: BLE001
        # Evidence and provider diagnostics can be sensitive.
        error = {"code": "EVALUATION_FAILED"}

    store.finish(item.id, outputs, error)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Process Teto evaluation jobs.")
    parser.add_argument("--once", action="store_true", help="Process at most one job.")
    args = parser.parse_args()
    db_path = os.environ.get("TETO_JOB_DB")
    if not db_path:
        parser.error("TETO_JOB_DB is required")

    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        store = JobStore(Path(db_path))
        with store.worker():
            while not stopping:
                processed = process_next(store)
                if args.once:
                    break
                if not processed:
                    time.sleep(0.25)
    except BlockingIOError:
        print("Another worker holds the job queue lock.", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("The evaluation worker could not access the job queue.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

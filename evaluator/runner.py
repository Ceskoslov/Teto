"""Execute a pinned, trusted harness once per trial in a fresh directory."""

import json
import os
import platform
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

from .models import validate_identifier
from .suites import (
    MAX_OUTPUT_BYTES,
    MAX_RESPONSE_BYTES,
    HarnessResponse,
    RunBudget,
    Suite,
    SuiteCase,
    SuiteRun,
    Trial,
    digest,
    invalid,
    read_contract,
    suite_digest,
)
from .validation import InputValidationError


def _read_output(workspace_fd: int, name: str) -> str | None:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=workspace_fd
        )
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("output is not a regular file")
        data = stream.read(MAX_OUTPUT_BYTES + 1)
    # Every valid expected output fits within MAX_OUTPUT_BYTES.
    return digest(data) if len(data) <= MAX_OUTPUT_BYTES else None


def _invoke(
    script: Path, workspace: Path, request: BinaryIO, response: BinaryIO, timeout: int
) -> str:
    process = subprocess.Popen(
        [sys.executable, "-I", str(script)],
        cwd=workspace,
        stdin=request,
        stdout=response,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return "completed" if process.wait(timeout / 1000) == 0 else "harness_failed"
    except subprocess.TimeoutExpired:
        return "timed_out"
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _run_trial(
    suite: Suite, case: SuiteCase, number: int, script: Path, workspace: Path
) -> Trial:
    workspace_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return _capture_trial(suite, case, number, script, workspace, workspace_fd)
    finally:
        os.close(workspace_fd)


def _capture_trial(
    suite: Suite, case: SuiteCase, number: int, script: Path, workspace: Path,
    workspace_fd: int,
) -> Trial:
    for name, contents in case.files.items():
        (workspace / name).write_bytes(contents.encode("utf-8"))
    limits = suite.limits
    payload = {
        "schema_version": 1,
        "case_id": case.id,
        "trial": number,
        "prompt": case.prompt,
        "mode": suite.mode,
        "model": suite.model.model_dump(mode="json"),
        "environment_revision": suite.environment.revision,
        "timeout_ms": limits.trial_timeout_ms,
        "cost_limit_microusd": limits.trial_cost_microusd,
    }
    response = None
    output_sha256 = None
    with tempfile.TemporaryFile() as request, tempfile.TemporaryFile() as stdout:
        request.write(json.dumps(payload).encode("utf-8"))
        request.seek(0)
        started = time.monotonic_ns()
        try:
            status = _invoke(script, workspace, request, stdout, limits.trial_timeout_ms)
        except OSError:
            status = "harness_failed"
        latency_ms = (time.monotonic_ns() - started) // 1_000_000
        if status == "completed":
            try:
                stdout.seek(0)
                raw = stdout.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeds protocol limit")
                response = read_contract(
                    raw.decode("utf-8"), HarnessResponse, "INVALID_HARNESS_RESPONSE"
                )
            except (InputValidationError, ValueError):
                status = "invalid_response"

    if response is not None:
        if response.cost_microusd > limits.trial_cost_microusd:
            status = "cost_exceeded"
        else:
            try:
                output_sha256 = _read_output(workspace_fd, case.acceptance.output)
            except OSError:
                status = "output_unreadable"
    return Trial(
        case_id=case.id, trial=number, status=status, latency_ms=latency_ms,
        response=response, output_sha256=output_sha256,
    )


def run_suite(suite: Suite, harness: bytes, artifact_id: str) -> SuiteRun:
    try:
        validate_identifier(artifact_id)
    except ValueError:
        raise invalid("INVALID_ARTIFACT_ID") from None
    if os.name != "posix":
        raise invalid("UNSUPPORTED_RUN_PLATFORM")
    environment = suite.environment
    runtime = (sys.platform, platform.machine(), platform.python_version())
    if runtime != (environment.platform, environment.machine, environment.python):
        raise invalid("ENVIRONMENT_PIN_MISMATCH")
    if digest(harness) != suite.harness.sha256:
        raise invalid("HARNESS_DIGEST_MISMATCH")

    budget = RunBudget.from_limits(suite.limits)
    trials = []
    with tempfile.TemporaryDirectory(prefix="teto-run-") as directory:
        root = Path(directory)
        script = root / "harness.py"
        script.write_bytes(harness)
        for number in range(1, suite.trials + 1):
            for case in suite.cases:
                if not budget.can_start():
                    # Keep a slot for each missing trial so reports have the planned denominator.
                    trial = Trial(
                        case_id=case.id, trial=number, status="budget_exhausted",
                        latency_ms=None, response=None, output_sha256=None,
                    )
                else:
                    with tempfile.TemporaryDirectory(dir=root, prefix="trial-") as workspace:
                        trial = _run_trial(suite, case, number, script, Path(workspace))
                    budget.charge(trial.response)
                trials.append(trial)
    return SuiteRun(
        schema_version=1, suite_id=suite.id, suite_sha256=suite_digest(suite),
        artifact_id=artifact_id, trials=trials,
    )

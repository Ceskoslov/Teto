"""Submission contracts and server-owned launch bundles for Jobs API v1."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError

from .agent_runs import NausicaaMode, VersionOne
from .job_store import JobStore
from .models import Identifier, InputModel
from .suites import invalid
from .validation import json_sha256, parse_json

MAX_JOB_BYTES = 16 * 1024 * 1024


class BundleUnavailable(Exception):
    pass


class _Submission(InputModel):
    schema_version: VersionOne


class LaunchJob(_Submission):
    kind: Literal["run"]
    bundle: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")]
    artifact_id: Identifier


class _Evidence(InputModel):
    input: str


class _NausicaaEvidence(_Evidence):
    format: Literal["nausicaa"]
    mode: NausicaaMode
    artifact_id: Identifier


class _OhmuEvidence(_Evidence):
    format: Literal["ohmu"]
    cases: str
    artifact_id: Identifier


class _AgentEvidence(_Evidence):
    format: Literal["agent-run"]


class _SuiteEvidence(_Evidence):
    format: Literal["suite-run"]
    suite: str
    policy: str | None = None


class ImportJob(_Submission):
    kind: Literal["import"]
    evidence: Annotated[
        _NausicaaEvidence | _OhmuEvidence | _AgentEvidence | _SuiteEvidence,
        Field(discriminator="format"),
    ]


Submission = Annotated[LaunchJob | ImportJob, Field(discriminator="kind")]
_SUBMISSION = TypeAdapter(Submission)


def _bundle_files(root: Path | None, name: str) -> dict[str, bytes]:
    if root is None:
        raise BundleUnavailable
    bundle = root / name
    files = {}
    for file in ("suite.json", "harness.py", "policy.json"):
        try:
            with (bundle / file).open("rb") as stream:
                data = stream.read(MAX_JOB_BYTES + 1)
        except FileNotFoundError:
            if file == "policy.json":
                continue
            raise BundleUnavailable from None
        if len(data) > MAX_JOB_BYTES:
            raise invalid("BUNDLE_FILE_TOO_LARGE")
        files[file] = data
    return files


def _import_inputs(job: ImportJob) -> tuple[dict, dict[str, bytes]]:
    names = {
        "input": "input.json",
        "cases": "cases.json",
        "suite": "suite.json",
        "policy": "policy.json",
    }
    options = job.evidence.model_dump(mode="json", exclude=set(names))
    files = {
        filename: text.encode("utf-8")
        for field, filename in names.items()
        if (text := getattr(job.evidence, field, None)) is not None
    }
    return {"kind": job.kind, **options}, files


def submit_job(store: JobStore, bundles: Path | None, key: str, text: str) -> str:
    parsed = parse_json(text)
    try:
        job = _SUBMISSION.validate_python(parsed)
    except ValidationError:
        raise invalid("INVALID_JOB_SUBMISSION") from None
    fingerprint = json_sha256(job.model_dump(mode="json"))
    existing = store.existing(key, fingerprint)
    if existing is not None:
        return existing

    if isinstance(job, LaunchJob):
        options = {"kind": job.kind, "artifact_id": job.artifact_id}
        files = _bundle_files(bundles, job.bundle)
    else:
        options, files = _import_inputs(job)
    return store.submit(key, fingerprint, options, files)

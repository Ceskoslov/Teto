"""Authenticated HTTP adapter for the durable evaluation queue."""

import hmac
import re
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from .job_store import IdempotencyConflict, JobStore
from .jobs import MAX_JOB_BYTES, BundleUnavailable, submit_job
from .validation import InputValidationError

_KEYS = re.compile(r"[a-zA-Z0-9._:-]{1,128}")
_EVIDENCE = {"input.json", "cases.json", "suite.json", "policy.json", "run.json"}
_REPORTS = {"score.json", "gate.json"}
_NO_CACHE = {"Cache-Control": "no-store"}


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status, headers=_NO_CACHE)


def job_router(db_path: Path, token: str, bundles: Path | None = None) -> APIRouter:
    if not token or not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError("TETO_JOB_TOKEN must be a nonempty ASCII bearer token")
    expected = token.encode("ascii")
    bearer = HTTPBearer(auto_error=False)
    store = JobStore(db_path)

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode("utf-8"), expected
        ):
            raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})

    router = APIRouter(
        prefix="/v1/jobs", tags=["Evaluation jobs"], dependencies=[Depends(authorize)]
    )

    @router.post(
        "",
        status_code=202,
        summary="Submit a launch or evidence import",
        responses={
            400: {"description": "Invalid JSON, UTF-8 or idempotency key"},
            404: {"description": "Launch bundle unavailable"},
            409: {"description": "Key already assigned to a different request"},
            413: {"description": "Submission exceeds 16 MiB"},
            415: {"description": "Content-Type must be application/json"},
            422: {"description": "Invalid job submission"},
            503: {"description": "Storage temporarily unavailable"},
        },
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "pattern": _KEYS.pattern},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"type": "object"}}},
            },
        },
    )
    async def submit(request: Request) -> JSONResponse:
        key = request.headers.get("idempotency-key", "")
        if _KEYS.fullmatch(key) is None:
            return _error(400, "INVALID_IDEMPOTENCY_KEY")
        media_type = request.headers.get("content-type", "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json":
            return _error(415, "APPLICATION_JSON_REQUIRED")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_JOB_BYTES:
                return _error(413, "SUBMISSION_TOO_LARGE")
            body.extend(chunk)

        try:
            job_id = await run_in_threadpool(
                submit_job, store, bundles, key, body.decode("utf-8")
            )
        except UnicodeDecodeError:
            return _error(400, "UTF8_REQUIRED")
        except InputValidationError as exception:
            status = 400 if exception.code == "INVALID_JSON" else 422
            return JSONResponse(exception.to_dict(), status, headers=_NO_CACHE)
        except IdempotencyConflict:
            return _error(409, "IDEMPOTENCY_CONFLICT")
        except BundleUnavailable:
            return _error(404, "BUNDLE_NOT_FOUND")
        except (OSError, sqlite3.Error):
            return _error(503, "JOB_STORAGE_UNAVAILABLE")

        location = f"/v1/jobs/{job_id}"
        return JSONResponse(
            {"id": job_id, "status_url": location},
            202,
            headers={**_NO_CACHE, "Location": location},
        )

    @router.get("/{job_id}", summary="Retrieve job status")
    def status(job_id: str) -> JSONResponse:
        try:
            job = store.status(job_id)
        except sqlite3.Error:
            return _error(503, "JOB_STORAGE_UNAVAILABLE")
        if job is None:
            return _error(404, "JOB_NOT_FOUND")
        return JSONResponse(job, headers=_NO_CACHE)

    def download(job_id: str, name: str, allowed: set[str]) -> Response:
        if name not in allowed:
            return _error(404, "JOB_FILE_NOT_FOUND")
        try:
            content = store.file(job_id, name)
        except sqlite3.Error:
            return _error(503, "JOB_STORAGE_UNAVAILABLE")
        if content is None:
            return _error(404, "JOB_FILE_NOT_FOUND")
        return Response(content, media_type="application/json", headers=_NO_CACHE)

    @router.get("/{job_id}/evidence/{name}", summary="Download original evidence")
    def evidence(job_id: str, name: str) -> Response:
        return download(job_id, name, _EVIDENCE)

    @router.get("/{job_id}/reports/{name}", summary="Download a score or policy report")
    def report(job_id: str, name: str) -> Response:
        return download(job_id, name, _REPORTS)

    return router

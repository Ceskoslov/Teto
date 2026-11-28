"""HTTP adapter for the same strict parser and evaluator used by the CLI."""

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from .core import EVALUATOR_VERSION, evaluate
from .job_api import job_router
from .validation import InputValidationError, validate_request

app = FastAPI(
    title="Offline LLM release evaluation",
    version=EVALUATOR_VERSION,
    debug=False,
)

if job_db := os.environ.get("TETO_JOB_DB"):
    bundle_root = os.environ.get("TETO_JOB_BUNDLES")
    app.include_router(
        job_router(
            Path(job_db),
            os.environ.get("TETO_JOB_TOKEN", ""),
            Path(bundle_root).resolve() if bundle_root else None,
        )
    )


def _error(status: int, code: str, issue_code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "issues": [{"path": "$", "code": issue_code}],
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(
    request: Request, exception: HTTPException
) -> JSONResponse:
    codes = {401: "AUTHENTICATION_REQUIRED", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
    response = _error(
        exception.status_code,
        "HTTP_ERROR",
        codes.get(exception.status_code, "REQUEST_FAILED"),
    )
    for name in ("Allow", "WWW-Authenticate"):
        if exception.headers and name in exception.headers:
            response.headers[name] = exception.headers[name]
    return response


def _evaluate_json(text: str) -> JSONResponse:
    # Parsing, evaluation and serialization run together outside the event loop.
    return JSONResponse(content=evaluate(validate_request(text)))


@app.post(
    "/evaluate",
    summary="Compare baseline and candidate responses",
    description=(
        "Accepts the challenge JSON fixture. Both release and hold return 200. "
        "The body is parsed by the CLI's strict JSON parser, including duplicate-key checks."
    ),
    responses={
        400: {"description": "Malformed JSON or invalid UTF-8"},
        415: {"description": "Content-Type must be application/json"},
        422: {"description": "Input does not satisfy the evaluation contract"},
        500: {"description": "Unexpected evaluation failure; details are withheld"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    },
)
async def evaluate_request(request: Request) -> JSONResponse:
    media_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if media_type != "application/json":
        return _error(415, "UNSUPPORTED_MEDIA_TYPE", "APPLICATION_JSON_REQUIRED")

    try:
        body = await request.body()
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return _error(400, "INVALID_JSON", "UTF8_REQUIRED")
        return await run_in_threadpool(_evaluate_json, text)
    except InputValidationError as exception:
        status = 400 if exception.code == "INVALID_JSON" else 422
        return JSONResponse(status_code=status, content=exception.to_dict())
    except (ArithmeticError, RuntimeError, TypeError, ValueError):
        # Catch here to keep exception details out of ASGI logs.
        return _error(500, "INTERNAL_ERROR", "EVALUATION_FAILED")

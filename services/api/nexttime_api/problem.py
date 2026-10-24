from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from nexttime_domain import deterministic_ulid

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ApiProblemError(Exception):
    """RFC 9457 problem-details exception without leaking internal secrets or stack traces."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        fields: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.fields = fields


def _request_id(request: Request) -> str:
    header_id = request.headers.get("X-Request-Id", "").strip()
    now = datetime.now(UTC)
    seed = header_id or f"{request.method}:{request.url.path}:{now.isoformat()}"
    return deterministic_ulid(namespace="request", seed=seed, timestamp=now)


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    fields: list[dict[str, str]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://errors.nexttime.local/{code}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        "instance": request.url.path,
        "request_id": _request_id(request),
    }
    if fields:
        body["fields"] = fields
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE)


def register_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiProblemError)
    async def _handle_api_problem(request: Request, exc: ApiProblemError) -> JSONResponse:
        return problem_response(
            request,
            status=exc.status,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            fields=exc.fields,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {
                "path": "/" + "/".join(str(part) for part in err.get("loc", ())[1:]),
                "code": str(err.get("type", "invalid")),
            }
            for err in exc.errors()
        ]
        return problem_response(
            request,
            status=422,
            code="validation_failed",
            title="Request validation failed",
            detail="One or more request fields failed schema validation.",
            fields=fields,
        )

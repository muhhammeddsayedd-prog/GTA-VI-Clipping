"""One error shape for the whole control API: `{"error": "<text>", "code": "<machine code>",
"request_id": "<id>"}` with the matching HTTP status (docs/17). Internal details never reach the caller."""

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

REQUEST_ID_STATE = "request_id"

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    422: "validation_error",
    500: "internal_error",
    501: "not_implemented",
    503: "unavailable",
    504: "timeout",
}


class ApiError(Exception):
    """An error the caller should see: HTTP status, machine-readable code and a short message."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})


def error_body(code: str, message: str, request_id: str | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"error": message, "code": code}
    if request_id:
        body["request_id"] = request_id
    body.update(extra)
    return body


def _request_id(request: Request) -> str | None:
    value = request.scope.get("state", {}).get(REQUEST_ID_STATE)
    return value if isinstance(value, str) else None


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        error_body(exc.code, exc.message, _request_id(request), **exc.extra),
        status_code=exc.status_code,
        headers=exc.headers,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODES.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else code
    return JSONResponse(
        error_body(code, message, _request_id(request)),
        status_code=exc.status_code,
        headers=dict(exc.headers or {}),
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Say which field is wrong and why, without repeating what the caller sent."""
    assert isinstance(exc, RequestValidationError)
    problems = []
    for item in exc.errors()[:10]:
        where = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
        problems.append({"field": where or "body", "problem": str(item.get("msg", "invalid"))})
    return JSONResponse(
        error_body("validation_error", "the request is not valid", _request_id(request), details=problems),
        status_code=422,
    )


def install(app: FastAPI) -> None:
    """Register the handlers. Unexpected exceptions are handled by the request middleware."""
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)

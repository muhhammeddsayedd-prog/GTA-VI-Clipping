"""Request middleware of the control API (plain ASGI, so it sees every request and every failure).

For each HTTP request it: gives the call a request id (header `X-Request-ID`), refuses bodies larger than
`MAX_BODY_BYTES`, turns an unexpected exception into the standard 500 body (logged with the traceback and
reported to Telegram), adds the response headers every answer carries, and writes one JSON log line with
method, path, status and duration. Headers, query strings and bodies are never logged, so the bearer token
cannot reach the logs.
"""

import asyncio
import json
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sixstars import notify
from sixstars.api.auth import ACTOR_STATE
from sixstars.api.errors import REQUEST_ID_STATE, error_body
from sixstars.logging import bind_context, clear_context, get_logger

MAX_BODY_BYTES = 256 * 1024  # the largest expected body is a takedown e-mail as text (M13)
REQUEST_ID_HEADER = "x-request-id"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_QUIET_PATHS = frozenset({"/v1/health"})  # polled every 30 s by the container health check


class _BodyTooLargeError(Exception):
    pass


def new_request_id(headers: list[tuple[bytes, bytes]]) -> str:
    """The caller's `X-Request-ID` when it is a plain token, otherwise a new random id."""
    for name, value in headers:
        if name == REQUEST_ID_HEADER.encode():
            candidate = value.decode("latin-1")
            if _SAFE_REQUEST_ID.match(candidate):
                return candidate
    return uuid.uuid4().hex


def _declared_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    for name, value in headers:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class RequestMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        log = get_logger("api")
        headers: list[tuple[bytes, bytes]] = list(scope.get("headers", []))
        request_id = new_request_id(headers)
        scope.setdefault("state", {})[REQUEST_ID_STATE] = request_id
        method, path = str(scope["method"]), str(scope["path"])
        started = time.monotonic()
        status = 0
        response_started = False
        overflow = False
        received = 0
        failure: dict[str, str] = {}
        clear_context()
        bind_context(request_id=request_id)

        async def start(message: Message) -> None:
            nonlocal status, response_started
            response_started = True
            status = int(message["status"])
            extra = [
                (REQUEST_ID_HEADER.encode(), request_id.encode()),
                (b"x-content-type-options", b"nosniff"),
                (b"cache-control", b"no-store"),
            ]
            await send({**message, "headers": [*message.get("headers", []), *extra]})

        async def answer(status_code: int, code: str, text: str) -> None:
            body = json.dumps(error_body(code, text, request_id)).encode()
            await start(
                {
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        async def too_large() -> None:
            failure["reason"] = "body_too_large"
            if not response_started:
                await answer(413, "payload_too_large", f"the body is limited to {self.max_body_bytes} bytes")

        async def limited_receive() -> Message:
            nonlocal received, overflow
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    overflow = True
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            if overflow:
                # The framework turned the overflow into its own error answer: replace it with ours.
                await too_large()
                return
            if message["type"] == "http.response.start":
                await start(message)
            else:
                await send(message)

        try:
            declared = _declared_length(headers)
            if declared is not None and declared > self.max_body_bytes:
                await too_large()
            else:
                await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLargeError:
            await too_large()
        except Exception as exc:
            failure["error"] = type(exc).__name__
            log.error("request_failed", method=method, path=path, error=type(exc).__name__, exc_info=True)
            if not response_started:
                await answer(500, "internal_error", "internal error")
            else:
                status = 500
            await asyncio.to_thread(
                notify.send,
                "warn",
                f"Control API error: {method} {path}",
                f"{type(exc).__name__} (request {request_id}). See the api logs.",
            )
        finally:
            duration_ms = round((time.monotonic() - started) * 1000, 1)
            if path not in _QUIET_PATHS or status >= 400 or failure:
                actor = scope.get("state", {}).get(ACTOR_STATE)
                log.info(
                    "request",
                    method=method,
                    path=path,
                    status=status,
                    duration_ms=duration_ms,
                    actor=actor if isinstance(actor, str) else None,
                    **failure,
                )
            clear_context()

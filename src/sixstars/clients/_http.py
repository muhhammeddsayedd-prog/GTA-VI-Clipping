"""Shared HTTP plumbing for the clients in this package: timeouts, the retry policy and call logging.

Policy (from the M02 milestone): connect timeout 10 s, read timeout 60 s (600 s for uploads); at most three
attempts with exponential backoff from 2 s to 30 s; a retry happens only after a network error, a 429 or a
5xx, and a `Retry-After` header is honoured. Every attempt is logged with service, endpoint, status and
duration. Query strings, headers and bodies are never logged, so keys and media stay out of the logs.
"""

import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from sixstars.logging import get_logger

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0
UPLOAD_TIMEOUT_S = 600.0
MAX_ATTEMPTS = 3
BACKOFF_MIN_S = 2.0
BACKOFF_MAX_S = 30.0
RETRY_AFTER_CAP_S = 120.0

DEFAULT_TIMEOUT = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
UPLOAD_TIMEOUT = httpx.Timeout(UPLOAD_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)

_backoff = wait_exponential(multiplier=BACKOFF_MIN_S, min=BACKOFF_MIN_S, max=BACKOFF_MAX_S)


class ClientError(RuntimeError):
    """Base class of the errors raised by the external clients."""


class NotConfiguredError(ClientError):
    """The client was used without the credentials it needs."""


class ApiError(ClientError):
    """An external API answered with an error status that no retry can fix (or retries ran out)."""

    def __init__(self, service: str, endpoint: str, status: int, message: str = "") -> None:
        super().__init__(f"{service} {endpoint} failed with HTTP {status}: {message}"[:500])
        self.service = service
        self.endpoint = endpoint
        self.status = status
        self.message = message


class _RetryableStatus(Exception):
    """Internal: carries a 429 or 5xx response through tenacity."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


def sleep(seconds: float) -> None:
    """Sleep hook used by retries and polling loops; tests replace it."""
    time.sleep(seconds)


def new_client(timeout: httpx.Timeout = DEFAULT_TIMEOUT, proxy: str | None = None) -> httpx.Client:
    """Create an httpx client with the standard timeouts. `proxy` is optional everywhere."""
    return httpx.Client(timeout=timeout, proxy=proxy, follow_redirects=False)


def retry_after_seconds(response: httpx.Response, now: datetime | None = None) -> float | None:
    """Seconds the server asked us to wait, from `Retry-After` (seconds or HTTP date) or `X-RateLimit-Reset`
    (Unix time). Returns None when the response carries neither."""
    current = now or datetime.now(UTC)
    value = response.headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0.0, (when - current).total_seconds())
    reset = response.headers.get("X-RateLimit-Reset") or response.headers.get("Ratelimit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - current.timestamp())
        except ValueError:
            return None
    return None


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError | _RetryableStatus)


def _wait(state: RetryCallState) -> float:
    exc = state.outcome.exception() if state.outcome is not None else None
    if isinstance(exc, _RetryableStatus):
        asked = retry_after_seconds(exc.response)
        if asked is not None:
            return min(asked, RETRY_AFTER_CAP_S)
    return float(_backoff(state))


def send(
    service: str,
    endpoint: str,
    call: Callable[[], httpx.Response],
    retry_statuses: Iterable[int] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> httpx.Response:
    """Run `call` (one HTTP request) under the retry policy and return the final response.

    `call` is invoked once per attempt, so it must build the request afresh (reopen files). A response
    with a retryable status is retried; when attempts run out that last response is returned so the
    caller maps the status to its own error. A network error that survives every attempt is re-raised.
    `retry_statuses` replaces the default rule (429 and every 5xx) when given.
    """
    log = get_logger(service)
    statuses = None if retry_statuses is None else frozenset(retry_statuses)

    def attempt() -> httpx.Response:
        started = time.monotonic()
        try:
            response = call()
        except httpx.TransportError as exc:
            log.warning(
                "api_call_failed",
                endpoint=endpoint,
                error=type(exc).__name__,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        status = response.status_code
        log.info(
            "api_call",
            endpoint=endpoint,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        retryable = status in statuses if statuses is not None else (status == 429 or status >= 500)
        if retryable:
            raise _RetryableStatus(response)
        return response

    retrying = Retrying(
        stop=stop_after_attempt(max_attempts),
        wait=_wait,
        retry=retry_if_exception(_is_retryable),
        sleep=lambda seconds: sleep(seconds),
        reraise=True,
    )
    try:
        return retrying(attempt)
    except _RetryableStatus as exc:
        return exc.response


def error_message(response: httpx.Response) -> str:
    """Short, safe description of an error response: the API's own message when it is JSON."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        for key in ("message", "error", "error_description", "detail"):
            value = body.get(key)
            if isinstance(value, str):
                return value[:300]
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return str(value["message"])[:300]
    return str(body)[:300]


def raise_for_status(service: str, endpoint: str, response: httpx.Response) -> None:
    """Raise `ApiError` for any 4xx or 5xx response."""
    if response.status_code >= 400:
        raise ApiError(service, endpoint, response.status_code, error_message(response))

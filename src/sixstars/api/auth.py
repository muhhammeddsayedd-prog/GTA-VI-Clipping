"""Bearer token authentication for the control API using `CONTROL_API_TOKEN`. Implemented in M11.

Every route except `/v1/health` depends on `require_token`. The token is compared in constant time. When
the server has no token configured, every protected route refuses: there is no open mode.
"""

import hmac

from fastapi import Request

from sixstars.api.errors import ApiError
from sixstars.config import env
from sixstars.logging import get_logger

SCHEME = "bearer"
MIN_TOKEN_CHARS = 32  # shorter tokens work but are reported at start-up


def _unauthorized() -> ApiError:
    # One answer for every failure, so a caller learns nothing about why it was refused.
    return ApiError(
        401, "unauthorized", "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
    )


def token_matches(provided: str, expected: str) -> bool:
    """Constant-time comparison of two tokens."""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def bearer_token(header: str | None) -> str | None:
    """The token of an `Authorization: Bearer <token>` header, or None when the header is not one."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != SCHEME or not token:
        return None
    return token


def require_token(request: Request) -> None:
    """FastAPI dependency: let the request through only with the right bearer token."""
    configured = env().CONTROL_API_TOKEN
    expected = configured.get_secret_value() if configured is not None else ""
    provided = bearer_token(request.headers.get("authorization"))
    if not expected:
        get_logger("api").error("control_api_token_not_configured")
        raise _unauthorized()
    # Compare even when no token was sent, so both failures take the same time.
    if not token_matches(provided or "", expected) or provided is None:
        raise _unauthorized()


def warn_if_weak_token() -> None:
    """Log at start-up when the token is missing or short. The value itself is never logged."""
    log = get_logger("api")
    configured = env().CONTROL_API_TOKEN
    if configured is None or not configured.get_secret_value():
        log.error("control_api_token_not_configured", effect="every protected route answers 401")
    elif len(configured.get_secret_value()) < MIN_TOKEN_CHARS:
        log.warning("control_api_token_short", min_chars=MIN_TOKEN_CHARS)

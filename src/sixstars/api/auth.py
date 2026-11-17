"""Bearer token authentication for the control API. Implemented in M11, extended in M17 (docs/26).

Two tokens exist. `CONTROL_API_TOKEN` (n8n) opens every protected route. `DASHBOARD_API_TOKEN` (the admin
console) opens every protected route except the trigger routes, which depend on `require_token`; the
others depend on `require_any_token`. Tokens are compared in constant time and both comparisons always
run. A token that is not configured never matches: there is no open mode.

The token that matched is stored as `actor` (`n8n` or `dashboard`) on `request.state`; the request
middleware writes it into its log line.
"""

import hmac
from typing import Literal

from fastapi import Request
from pydantic import SecretStr

from sixstars.api.errors import ApiError
from sixstars.config import env
from sixstars.logging import get_logger

SCHEME = "bearer"
MIN_TOKEN_CHARS = 32  # shorter tokens work but are reported at start-up
ACTOR_STATE = "actor"
_UNSET = "\x00not-configured"  # compared against when a token is unset; can never be sent in a header

Actor = Literal["n8n", "dashboard"]


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


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""


def _matches(provided: str | None, expected: str) -> bool:
    # Compare even when nothing was sent or nothing is configured, so every failure takes the same time.
    same = token_matches(provided or "", expected or _UNSET)
    return same and bool(expected) and provided is not None


def select_actor(provided: str | None, control: str, dashboard: str, allow_dashboard: bool) -> Actor | None:
    """Pure rule: who sent `provided`, or None when it opens nothing on this route.

    `control` and `dashboard` are the configured tokens ("" when unset). The dashboard token counts only
    where `allow_dashboard` is true."""
    n8n = _matches(provided, control)
    console = _matches(provided, dashboard)
    if n8n:
        return "n8n"
    if console and allow_dashboard:
        return "dashboard"
    return None


def _authenticate(request: Request, allow_dashboard: bool) -> None:
    settings = env()
    control = _secret(settings.CONTROL_API_TOKEN)
    dashboard = _secret(settings.DASHBOARD_API_TOKEN)
    if not control and not (allow_dashboard and dashboard):
        get_logger("api").error("control_api_token_not_configured")
    provided = bearer_token(request.headers.get("authorization"))
    actor = select_actor(provided, control, dashboard, allow_dashboard)
    if actor is None:
        raise _unauthorized()
    setattr(request.state, ACTOR_STATE, actor)


def require_token(request: Request) -> None:
    """FastAPI dependency for the trigger routes: only n8n's `CONTROL_API_TOKEN` is accepted."""
    _authenticate(request, allow_dashboard=False)


def require_any_token(request: Request) -> None:
    """FastAPI dependency for every other protected route: `CONTROL_API_TOKEN` or `DASHBOARD_API_TOKEN`."""
    _authenticate(request, allow_dashboard=True)


def warn_if_weak_token() -> None:
    """Log at start-up when a token is missing or short. The values themselves are never logged."""
    log = get_logger("api")
    settings = env()
    control = _secret(settings.CONTROL_API_TOKEN)
    if not control:
        log.error("control_api_token_not_configured", effect="every protected route answers 401")
    elif len(control) < MIN_TOKEN_CHARS:
        log.warning("control_api_token_short", min_chars=MIN_TOKEN_CHARS)
    dashboard = _secret(settings.DASHBOARD_API_TOKEN)
    if dashboard and len(dashboard) < MIN_TOKEN_CHARS:
        log.warning("dashboard_api_token_short", min_chars=MIN_TOKEN_CHARS)

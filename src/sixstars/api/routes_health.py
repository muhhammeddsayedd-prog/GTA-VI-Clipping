"""`GET /v1/health`: the only route without authentication. It answers 200 when the process is up and the
database answers, and 503 otherwise, so the container health check and the deploy workflow see a broken
database. The body never says more than that."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from sixstars import db
from sixstars.config import env
from sixstars.logging import get_logger

router = APIRouter()

DB_PING_TIMEOUT_S = 3.0  # the container health check gives the whole call 5 s


def database_ok(timeout_s: float = DB_PING_TIMEOUT_S) -> bool:
    """True when a connection is available within `timeout_s` and answers `select 1`."""
    try:
        with db.pool().connection(timeout=timeout_s) as conn:
            row = conn.execute("select 1").fetchone()
            return row is not None and row[0] == 1
    except Exception as exc:
        get_logger("api").warning("health_db_unreachable", error=type(exc).__name__)
        return False


@router.get("/v1/health")
def health() -> JSONResponse:
    app_env = env().APP_ENV
    if database_ok():
        return JSONResponse({"ok": True, "env": app_env})
    return JSONResponse(
        {"ok": False, "env": app_env, "error": "database unreachable", "code": "db_unreachable"},
        status_code=503,
    )

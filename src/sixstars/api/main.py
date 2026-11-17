"""FastAPI application factory and router wiring for the control API. Implemented in M11.

`create_app()` builds the application: `/v1/health` is open, every other route needs the bearer token
(`sixstars.api.auth`). The interactive documentation and the OpenAPI document are switched off, so nothing
but the health route answers without the token. `run()` serves it with uvicorn on `0.0.0.0:8080`, behind
Caddy, with uvicorn's own log lines converted to the same JSON format as the rest of the system.
"""

import json
import logging as stdlib_logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import uvicorn
from fastapi import Depends, FastAPI

from sixstars import db
from sixstars.api import errors, routes_control, routes_health, routes_triggers
from sixstars.api.auth import require_token, warn_if_weak_token
from sixstars.api.middleware import RequestMiddleware
from sixstars.config import env
from sixstars.logging import configure_logging, get_logger

HOST = "0.0.0.0"  # inside the container; only Caddy can reach the port (compose `expose`)
PORT = 8080
GRACEFUL_SHUTDOWN_S = 30  # below the 60 s `stop_grace_period` of docker compose
SERVICE = "api"


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log = get_logger(SERVICE)
    warn_if_weak_token()
    log.info("api_started")
    yield
    log.info("api_stopped")
    db.close_pool()


def create_app() -> FastAPI:
    """Build the control API application."""
    app = FastAPI(
        title="6 Stars Clips control API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    errors.install(app)
    app.add_middleware(RequestMiddleware)
    app.include_router(routes_health.router)
    protected = [Depends(require_token)]
    app.include_router(routes_triggers.router, dependencies=protected)
    app.include_router(routes_control.router, dependencies=protected)
    return app


class _JsonLogHandler(stdlib_logging.Handler):
    """Writes standard-library log records (uvicorn's) as the JSON lines the other services write."""

    def emit(self, record: stdlib_logging.LogRecord) -> None:
        try:
            settings = env()
            line = {
                "env": settings.APP_ENV,
                "service": SERVICE,
                "logger": record.name,
                "event": record.getMessage(),
                "level": record.levelname.lower(),
                "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            }
            if record.exc_info and record.exc_info[0] is not None:
                line["error"] = record.exc_info[0].__name__
            print(json.dumps(line, ensure_ascii=False), flush=True)
        except Exception:
            self.handleError(record)


def configure_server_logging() -> None:
    """Send uvicorn's loggers through the JSON handler. The access log stays off: the request middleware
    writes its own line, without the query string."""
    handler = _JsonLogHandler()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = stdlib_logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(stdlib_logging.INFO)


def run(host: str = HOST, port: int = PORT) -> None:
    """Serve the control API until SIGINT or SIGTERM (uvicorn finishes the requests in flight)."""
    configure_logging()
    configure_server_logging()
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )

"""Structured logging for every service. Configures structlog to write one JSON object per line to
stdout with the keys `ts` (ISO 8601 UTC), `level`, `event`, `env` and `service`. `get_logger(service)`
returns a logger bound to the current environment and service name; `bind_context(clip_id=...,
spike_id=..., job_id=...)` attaches ids to every later log line in the current task or thread. Secrets
must never be passed to a logger.
"""

import logging as stdlib_logging
from typing import Any, cast

import structlog
from structlog.typing import FilteringBoundLogger

from sixstars.config import env

_configured = False


def configure_logging(level: str | None = None) -> None:
    """Configure structlog for JSON output. `level` defaults to the `LOG_LEVEL` environment variable."""
    global _configured
    level_name = (level or env().LOG_LEVEL).upper()
    level_number = stdlib_logging.getLevelNamesMapping().get(level_name, stdlib_logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_number),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(service: str | None = None) -> FilteringBoundLogger:
    """Return a JSON logger bound with `env` and `service` (defaults to `SERVICE_NAME`)."""
    if not _configured:
        configure_logging()
    settings = env()
    logger = structlog.get_logger().bind(env=settings.APP_ENV, service=service or settings.SERVICE_NAME)
    return cast(FilteringBoundLogger, logger)


def bind_context(**kw: Any) -> None:
    """Attach context (for example `clip_id`, `spike_id`, `job_id`) to all later log lines in this context."""
    structlog.contextvars.bind_contextvars(**kw)


def clear_context() -> None:
    """Remove all context bound with `bind_context`. Call when a job or request ends."""
    structlog.contextvars.clear_contextvars()

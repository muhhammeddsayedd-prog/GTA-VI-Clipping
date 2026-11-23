"""Spend and quota tracking in the `api_usage` table. Every external client calls `record()` once per
logical operation, so the guardian, the daily report and the budget guards can see what each provider
cost. Implemented in M02.

`record()` never raises: losing one usage row must not fail a clip. When no database is configured (unit
tests, a local script) the row is skipped silently.
"""

from dataclasses import dataclass
from typing import Literal

import psycopg

from sixstars import db
from sixstars.config import MissingConfigError
from sixstars.logging import get_logger

_log = get_logger("usage")

Outcome = Literal["ok", "invalid", "refusal", "http_error"]
DETAIL_MAX_CHARS = 300


@dataclass(frozen=True, slots=True)
class CallDetail:
    """Per-call facts of a model request (the `api_usage` columns added in migration 0037)."""

    stop_reason: str | None = None
    attempt: int | None = None
    outcome: Outcome | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    detail: str | None = None


def record(
    provider: str,
    operation: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    units: float | None = None,
    cost_usd: float = 0.0,
    ref_id: str | None = None,
    *,
    detail: CallDetail | None = None,
) -> bool:
    """Insert one `api_usage` row. Returns True when the row was written.

    `provider` is `anthropic`, `openai`, `groq`, `apify`, `upload_post`, `youtube`, `twitch` or `local`.
    `units` is whatever the provider counts: seconds of audio, quota units, requests, uploads.
    `detail` (model calls only, migration 0037) adds stop reason, attempt, outcome and cache tokens.
    """
    extra = detail or CallDetail()
    try:
        db.execute(
            """
            insert into api_usage
              (provider, operation, model, input_tokens, output_tokens, units, cost_usd, ref_id,
               stop_reason, attempt, outcome, cache_read_tokens, cache_write_tokens, detail)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                provider,
                operation,
                model,
                input_tokens,
                output_tokens,
                units,
                cost_usd,
                ref_id,
                extra.stop_reason,
                extra.attempt,
                extra.outcome,
                extra.cache_read_tokens,
                extra.cache_write_tokens,
                extra.detail[:DETAIL_MAX_CHARS] if extra.detail else None,
            ),
        )
    except MissingConfigError:
        return False
    except psycopg.errors.UndefinedColumn:
        # A database without migration 0037: keep the spend row (the budget guards read it), drop the detail.
        return _record_plain(provider, operation, model, input_tokens, output_tokens, units, cost_usd, ref_id)
    except Exception as exc:
        _log.warning("usage_record_failed", provider=provider, operation=operation, error=type(exc).__name__)
        return False
    return True


def _record_plain(
    provider: str,
    operation: str,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    units: float | None,
    cost_usd: float,
    ref_id: str | None,
) -> bool:
    try:
        db.execute(
            """
            insert into api_usage
              (provider, operation, model, input_tokens, output_tokens, units, cost_usd, ref_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (provider, operation, model, input_tokens, output_tokens, units, cost_usd, ref_id),
        )
    except Exception as exc:
        _log.warning("usage_record_failed", provider=provider, operation=operation, error=type(exc).__name__)
        return False
    return True


def spent_today(provider: str | tuple[str, ...]) -> float:
    """Return the USD spent with `provider` (one name or several) since 00:00 UTC today."""
    providers = [provider] if isinstance(provider, str) else list(provider)
    row = db.fetch_one(
        """
        select coalesce(sum(cost_usd), 0) as total
        from api_usage
        where provider = any(%s) and created_at >= date_trunc('day', now(), 'UTC')
        """,
        (providers,),
    )
    return float(row["total"]) if row is not None else 0.0

"""Spend and quota tracking in the `api_usage` table. Every external client calls `record()` once per
logical operation, so the guardian, the daily report and the budget guards can see what each provider
cost. Implemented in M02.

`record()` never raises: losing one usage row must not fail a clip. When no database is configured (unit
tests, a local script) the row is skipped silently.
"""

from sixstars import db
from sixstars.config import MissingConfigError
from sixstars.logging import get_logger

_log = get_logger("usage")


def record(
    provider: str,
    operation: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    units: float | None = None,
    cost_usd: float = 0.0,
    ref_id: str | None = None,
) -> bool:
    """Insert one `api_usage` row. Returns True when the row was written.

    `provider` is `anthropic`, `openai`, `groq`, `apify`, `upload_post`, `youtube`, `twitch` or `local`.
    `units` is whatever the provider counts: seconds of audio, quota units, requests, uploads.
    """
    try:
        db.execute(
            """
            insert into api_usage
              (provider, operation, model, input_tokens, output_tokens, units, cost_usd, ref_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (provider, operation, model, input_tokens, output_tokens, units, cost_usd, ref_id),
        )
    except MissingConfigError:
        return False
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

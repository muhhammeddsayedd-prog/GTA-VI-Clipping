"""Token and cost accounting for AI calls, enforcing the daily spend limits from settings. Implemented in
M05.

The rows are written by the clients (`sixstars.usage.record`, one per request, `operation` = prompt id).
This module only reads them: for the budget guard, the guardian and the daily report.
"""

from dataclasses import dataclass
from datetime import datetime

from sixstars import db, settings, usage

ANTHROPIC = "anthropic"


@dataclass(frozen=True, slots=True)
class OperationSpend:
    operation: str
    model: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


def today_spend(provider: str = ANTHROPIC) -> float:
    """USD spent with `provider` since 00:00 UTC today."""
    return usage.spent_today(provider)


def budget_remaining() -> float:
    """USD left of `ai.daily_budget_usd` today (never negative)."""
    return max(0.0, settings.get_float("ai.daily_budget_usd") - today_spend(ANTHROPIC))


def spend_by_operation(provider: str = ANTHROPIC, since: datetime | None = None) -> list[OperationSpend]:
    """Calls, tokens and cost per prompt and model since `since` (default: 00:00 UTC today), most
    expensive first."""
    rows = db.fetch_all(
        """
        select operation, model, count(*) as calls,
               coalesce(sum(input_tokens), 0) as input_tokens,
               coalesce(sum(output_tokens), 0) as output_tokens,
               coalesce(sum(cost_usd), 0) as cost_usd
        from api_usage
        where provider = %s and created_at >= coalesce(%s, date_trunc('day', now(), 'UTC'))
        group by operation, model
        order by cost_usd desc, operation
        """,
        (provider, since),
    )
    return [
        OperationSpend(
            operation=row["operation"],
            model=row["model"],
            calls=int(row["calls"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cost_usd=float(row["cost_usd"]),
        )
        for row in rows
    ]

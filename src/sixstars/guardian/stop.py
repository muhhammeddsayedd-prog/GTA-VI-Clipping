"""Emergency stop (docs/12 section 4): the guardian sets `control.emergency_stop` and its reason, and
never clears it. Only `/start` (WF08), `POST /v1/control/start` or the CLI clear it.

Rule 1, strikes: every claim, platform removal or actioned takedown email that touched a real published
post is a strike. `guardian.strike_limit` strikes within `guardian.strike_window_d` days stop all posting.
Strikes are counted from the last guardian stop onwards, so after the owner restarts posting it takes
`strike_limit` new strikes to stop again (otherwise `/start` would be undone by the next check).

Rule 2, account-level removal: when most posts of one platform vanish in one pass (see
`guardian.mass_missing_*`), posting stops without blaming any creator. `claims.py` calls `trigger`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sixstars import db, events, notify, settings
from sixstars.logging import get_logger

STOP_KEY = "control.emergency_stop"
REASON_KEY = "control.stop_reason"
STOP_EVENT = "guardian.stop"
SERVICE = "guardian"
MAX_REASON_CHARS = 500


@dataclass(frozen=True, slots=True)
class StopDecision:
    strikes: int
    limit: int
    window_d: float
    should_stop: bool
    triggered: bool  # this call switched the stop on
    already_stopped: bool


def window_start(now: datetime, window_d: float, last_stop: datetime | None) -> datetime:
    """Strikes count from the start of the window, or from the last guardian stop if that is later."""
    start = now - timedelta(days=window_d)
    return max(start, last_stop) if last_stop is not None else start


def strikes_in_window(times: Sequence[datetime], now: datetime, since: datetime) -> int:
    """How many strike times lie in `[since, now]` (both ends count: when unsure, count it)."""
    return sum(1 for at in times if since <= at <= now)


def should_stop(strikes: int, limit: int) -> bool:
    """The strike rule. A limit below 1 is a broken setting and is read as 1, never as "no limit"."""
    return strikes >= max(1, limit)


def is_stopped() -> bool:
    row = db.fetch_one("select value from settings where key = %s", (STOP_KEY,))
    return row is not None and row["value"] is True


def last_stop_at() -> datetime | None:
    row = db.fetch_one("select max(created_at) as at from events where type = %s", (STOP_EVENT,))
    return None if row is None else row["at"]


def strike_times(since: datetime) -> list[datetime]:
    rows = db.fetch_all(
        """
        select received_at from takedowns
        where is_takedown and status in ('actioned', 'partial')
          and coalesce((actions->>'strike')::boolean, false)
          and received_at >= %s
        order by received_at
        """,
        (since,),
    )
    return [row["received_at"] for row in rows]


def trigger(reason: str, data: Mapping[str, Any] | None = None) -> bool:
    """Switch the emergency stop on. Returns True when this call changed it; then, and only then, one
    event is written and one critical alert is sent. An existing stop keeps its own reason."""
    reason = reason[:MAX_REASON_CHARS]
    with db.transaction():
        rows = db.fetch_all(
            "select key, value from settings where key = any(%s) for update", ([STOP_KEY, REASON_KEY],)
        )
        before = {row["key"]: row["value"] for row in rows}
        if before.get(STOP_KEY) is True:
            return False
        settings.set(STOP_KEY, True)
        settings.set(REASON_KEY, reason)
        events.emit(
            STOP_EVENT,
            level="critical",
            entity_type="setting",
            entity_id=STOP_KEY,
            message=f"emergency stop set by the guardian: {reason}",
            data={"reason": reason, **dict(data or {})},
            service=SERVICE,
        )
    settings.invalidate()
    get_logger(SERVICE).critical("emergency_stop_set", reason=reason)
    notify.send(
        "critical",
        "EMERGENCY STOP: all posting halted",
        f"Reason: {reason}\n"
        "Discovery and rendering continue; nothing is posted until you send /start "
        "(or run: sixstars settings set control.emergency_stop false). "
        "The guardian never restarts posting by itself.",
    )
    return True


def evaluate(now: datetime, limit: int, window_d: float) -> StopDecision:
    """Apply the strike rule now and switch the stop on when it says so."""
    if is_stopped():
        return StopDecision(0, limit, window_d, False, False, True)
    since = window_start(now, window_d, last_stop_at())
    times = strike_times(since)
    # Rows are stamped by the database clock, which may be a moment ahead of `now`.
    strikes = strikes_in_window(times, max([now, *times]), since)
    stop = should_stop(strikes, limit)
    triggered = False
    if stop:
        triggered = trigger(
            f"{strikes} strike(s) (claims, removals or takedowns) within {window_d:g} day(s); "
            f"the limit is {max(1, limit)}",
            {"rule": "strikes", "strikes": strikes, "limit": limit, "window_d": window_d},
        )
    return StopDecision(strikes, limit, window_d, stop, triggered, False)

"""Alert once when something breaks and once when it works again.

An incident is identified by a key such as `heartbeat:worker-express` or `trigger:hourly`. Its state is
the latest `health.incident` / `health.recovered` event with that key as `entity_id`, so it survives a
restart of the scheduler and needs no table of its own. `report()` is called on every health pass with
the current state and only acts on a change.
"""

from collections.abc import Mapping
from typing import Any, Literal

from sixstars import db, events, notify

OPENED = "health.incident"
CLOSED = "health.recovered"
ENTITY = "incident"
SERVICE = "guardian"

Change = Literal["opened", "closed", "none"]


def open_keys() -> set[str]:
    """Keys of the incidents that are open now."""
    rows = db.fetch_all(
        """
        select distinct on (entity_id) entity_id, type
        from events
        where type in (%s, %s) and entity_type = %s
        order by entity_id, id desc
        """,
        (OPENED, CLOSED, ENTITY),
    )
    return {str(row["entity_id"]) for row in rows if row["type"] == OPENED}


def transition(was_open: bool, failing: bool) -> Change:
    """Pure rule: act only when the state changes."""
    if failing and not was_open:
        return "opened"
    if was_open and not failing:
        return "closed"
    return "none"


def report(
    key: str,
    failing: bool,
    *,
    severity: notify.Severity,
    title: str,
    body: str = "",
    recovered_title: str | None = None,
    data: Mapping[str, Any] | None = None,
    open_now: set[str] | None = None,
) -> Change:
    """Record the state of `key`. Sends one alert when it starts failing and one when it recovers.

    `open_now` is the set from `open_keys()`; pass it when reporting several keys in one pass (it is
    updated in place)."""
    known = open_keys() if open_now is None else open_now
    change = transition(key in known, failing)
    if change == "opened":
        events.emit(
            OPENED,
            level="critical" if severity == "critical" else "warn",
            entity_type=ENTITY,
            entity_id=key,
            message=title,
            data=dict(data or {}),
            service=SERVICE,
        )
        known.add(key)
        notify.send(severity, title, body)
    elif change == "closed":
        recovered = recovered_title or f"Recovered: {title}"
        events.emit(
            CLOSED,
            level="info",
            entity_type=ENTITY,
            entity_id=key,
            message=recovered,
            data=dict(data or {}),
            service=SERVICE,
        )
        known.discard(key)
        # A recovery is good news: `info`, except that the end of a critical incident is sent at the
        # same level so that it is not held back by the warn rate limit or a muted channel.
        notify.send("critical" if severity == "critical" else "info", recovered, "")
    return change

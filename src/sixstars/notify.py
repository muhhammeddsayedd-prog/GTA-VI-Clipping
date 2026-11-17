"""Telegram notifications sent through the n8n notify webhook, backed by an outbox so messages survive
webhook outages. Anything a human would want to know goes through this module. Implemented in M01.

`send()` writes the message to `notify_outbox`, then POSTs it to `N8N_NOTIFY_WEBHOOK_URL`. A 2xx answer
marks the row as sent; anything else leaves it unsent for `retry_unsent()`, which the scheduler calls
(M13). `send()` never raises: a notification problem must not break the work that reported it.
"""

from datetime import datetime
from typing import Any, Literal

import httpx

from sixstars import db
from sixstars.config import env
from sixstars.logging import get_logger

Severity = Literal["info", "warn", "critical"]

HTTP_TIMEOUT_S = 10.0
WARN_RATE_LIMIT_S = 30 * 60
SECRET_HEADER = "X-6Stars-Secret"


def send(severity: Severity, title: str, body: str = "") -> bool:
    """Queue a notification and try to deliver it now. Returns True when the webhook accepted it.

    `warn` messages are rate limited: when another `warn` with the same title was queued in the last
    30 minutes, nothing is stored or sent and the result is False.
    """
    log = get_logger("notify")
    try:
        if severity == "warn" and _recent_warn_exists(title):
            log.info("notify_rate_limited", severity=severity, title=title)
            return False
        row = db.fetch_one(
            "insert into notify_outbox (severity, title, body) values (%s, %s, %s) returning *",
            (severity, title, body),
        )
        assert row is not None
        return _deliver(row)
    except Exception as exc:
        log.error("notify_failed", severity=severity, title=title, error=type(exc).__name__)
        return False


def retry_unsent(limit: int = 50) -> int:
    """Try again to deliver unsent outbox rows, oldest first. Returns how many were delivered."""
    log = get_logger("notify")
    try:
        rows = db.fetch_all(
            "select * from notify_outbox where sent_at is null order by id limit %s",
            (limit,),
        )
    except Exception as exc:
        log.error("notify_retry_failed", error=type(exc).__name__)
        return 0
    delivered = 0
    for row in rows:
        try:
            if _deliver(row):
                delivered += 1
        except Exception as exc:
            log.error("notify_retry_failed", outbox_id=row["id"], error=type(exc).__name__)
    return delivered


def _recent_warn_exists(title: str) -> bool:
    row = db.fetch_one(
        """
        select 1 as hit from notify_outbox
        where severity = 'warn' and title = %s and created_at > now() - make_interval(secs => %s)
        limit 1
        """,
        (title, WARN_RATE_LIMIT_S),
    )
    return row is not None


def _payload(row: dict[str, Any]) -> dict[str, str]:
    created: datetime = row["created_at"]
    return {
        "severity": row["severity"],
        "title": row["title"],
        "body": row["body"] or "",
        "env": env().APP_ENV,
        "ts": created.isoformat(),
    }


def _deliver(row: dict[str, Any]) -> bool:
    """POST one outbox row to the webhook and record the attempt. True when it was accepted (2xx)."""
    log = get_logger("notify")
    settings = env()
    url = settings.N8N_NOTIFY_WEBHOOK_URL
    secret = settings.N8N_NOTIFY_SECRET
    accepted = False
    if not url:
        log.warning("notify_webhook_not_configured", outbox_id=row["id"])
    else:
        headers = {SECRET_HEADER: secret.get_secret_value()} if secret is not None else {}
        try:
            response = httpx.post(url, json=_payload(row), headers=headers, timeout=HTTP_TIMEOUT_S)
            accepted = response.is_success
            if not accepted:
                log.warning("notify_webhook_rejected", outbox_id=row["id"], status=response.status_code)
        except httpx.HTTPError as exc:
            log.warning("notify_webhook_unreachable", outbox_id=row["id"], error=type(exc).__name__)
    db.execute(
        """
        update notify_outbox
        set attempts = attempts + 1, sent_at = case when %s then now() else sent_at end
        where id = %s
        """,
        (accepted, row["id"]),
    )
    return accepted

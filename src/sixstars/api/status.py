"""The `/v1/status` snapshot: what the system is doing right now, as JSON or as a Telegram message.

`collect()` reads everything from the database in a few queries and returns a plain dict (the JSON
answer). `render_telegram()` turns that dict into HTML-safe text for Telegram's HTML parse mode.
"""

from datetime import UTC, datetime
from html import escape
from typing import Any

from sixstars import db
from sixstars.config import env

SETTING_KEYS = (
    "control.emergency_stop",
    "control.stop_reason",
    "launch.dry_run",
    "launch.launch_at",
    "live.max_slots",
    "guardian.heartbeat_stale_s",
    "posting.queue_low_watermark",
    "ai.daily_budget_usd",
)
ACTIVE_LIVE_STATUSES = ("assigned", "recording")
LANES = ("spike", "hourly")
QUEUES = ("express", "normal")
HEARTBEAT_WINDOW_S = 24 * 3600  # older worker heartbeats belong to processes that no longer exist
TRIGGER_PREFIX = "trigger:"
OLD_HEARTBEAT_S = 3600  # the Telegram text hides services that stopped longer ago than this


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def posting_mode(emergency_stop: bool, dry_run: bool, launch_at: datetime | None, now: datetime) -> str:
    """`stopped`, `dry_run` (the switch, or before the launch time, or no readable launch time) or `live`."""
    if emergency_stop:
        return "stopped"
    if dry_run or launch_at is None or now < launch_at:
        return "dry_run"
    return "live"


def _settings() -> dict[str, Any]:
    # Read from the table, not from the 30 s cache: a stop set a second ago must show.
    rows = db.fetch_all("select key, value from settings where key = any(%s)", (list(SETTING_KEYS),))
    return {row["key"]: row["value"] for row in rows}


def _live_sessions(max_slots: Any) -> dict[str, Any]:
    rows = db.fetch_all(
        "select status, count(*) as n from live_sessions where status = any(%s) group by status",
        (list(ACTIVE_LIVE_STATUSES),),
    )
    by_status = {status: 0 for status in ACTIVE_LIVE_STATUSES}
    by_status.update({row["status"]: int(row["n"]) for row in rows})
    return {
        "active": sum(by_status.values()),
        "max_slots": max_slots if isinstance(max_slots, int) else None,
        "by_status": by_status,
    }


def _approved_queue() -> dict[str, int]:
    rows = db.fetch_all("select lane, count(*) as n from clips where status = 'approved' group by lane")
    queue = {lane: 0 for lane in LANES}
    queue.update({row["lane"]: int(row["n"]) for row in rows})
    return queue


def _posts() -> dict[str, Any]:
    rows = db.fetch_all(
        """
        select a.id as account_id, a.enabled, a.paused_until, a.cap_24h,
          count(p.id) filter (where not p.dry_run and p.status = 'published'
                              and p.published_at >= now() - interval '1 hour') as published_1h,
          count(p.id) filter (where not p.dry_run and p.status = 'published'
                              and p.published_at >= now() - interval '24 hours') as published_24h,
          count(p.id) filter (where not p.dry_run and p.status = 'failed'
                              and p.created_at >= now() - interval '24 hours') as failed_24h,
          count(p.id) filter (where not p.dry_run and p.status = 'pending') as pending,
          count(p.id) filter (where p.dry_run and p.created_at >= now() - interval '24 hours') as dry_run_24h
        from platform_accounts a
        left join posts p
          on p.account_id = a.id
         and (p.created_at >= now() - interval '25 hours' or p.published_at >= now() - interval '24 hours'
              or p.status = 'pending')
        group by a.id
        order by a.id
        """
    )
    accounts = [
        {
            "account_id": row["account_id"],
            "enabled": bool(row["enabled"]),
            "paused_until": row["paused_until"].isoformat() if row["paused_until"] else None,
            "cap_24h": int(row["cap_24h"]),
            "published_last_hour": int(row["published_1h"]),
            "published_last_24h": int(row["published_24h"]),
            "failed_last_24h": int(row["failed_24h"]),
            "pending": int(row["pending"]),
            "dry_run_last_24h": int(row["dry_run_24h"]),
        }
        for row in rows
    ]
    return {
        "last_hour": {a["account_id"]: a["published_last_hour"] for a in accounts},
        "last_24h": {a["account_id"]: a["published_last_24h"] for a in accounts},
        "accounts": accounts,
    }


def _spend_today() -> dict[str, Any]:
    rows = db.fetch_all(
        """
        select provider, coalesce(sum(cost_usd), 0) as total
        from api_usage
        where created_at >= date_trunc('day', now(), 'UTC')
        group by provider
        order by provider
        """
    )
    by_provider = {row["provider"]: round(float(row["total"]), 4) for row in rows}
    return {"total": round(sum(by_provider.values()), 4), "by_provider": by_provider}


def _jobs() -> dict[str, Any]:
    rows = db.fetch_all(
        """
        select queue, status, count(*) as n
        from jobs
        where status in ('queued', 'running')
           or (status = 'failed' and updated_at >= now() - interval '24 hours')
        group by queue, status
        """
    )
    out: dict[str, dict[str, int]] = {
        "queued": {queue: 0 for queue in QUEUES},
        "running": {queue: 0 for queue in QUEUES},
        "failed_last_24h": {queue: 0 for queue in QUEUES},
    }
    names = {"queued": "queued", "running": "running", "failed": "failed_last_24h"}
    for row in rows:
        out[names[row["status"]]][row["queue"]] = int(row["n"])
    return out


def _heartbeats(stale_after_s: float | None) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        select name, last_seen, extract(epoch from now() - last_seen) as age_s
        from heartbeats
        where name like %s or last_seen >= now() - make_interval(secs => %s)
        order by name
        """,
        (TRIGGER_PREFIX + "%", HEARTBEAT_WINDOW_S),
    )
    beats = []
    for row in rows:
        name = str(row["name"])
        age_s = max(0, round(float(row["age_s"])))
        kind = "trigger" if name.startswith(TRIGGER_PREFIX) else "service"
        # A trigger has its own rhythm (10 min, 1 h); only services are judged against the stale limit.
        stale = kind == "service" and stale_after_s is not None and age_s > stale_after_s
        beats.append(
            {
                "name": name,
                "kind": kind,
                "last_seen": row["last_seen"].astimezone(UTC).isoformat(),
                "age_s": age_s,
                "stale": stale,
            }
        )
    return beats


def collect(now: datetime | None = None) -> dict[str, Any]:
    """The status snapshot as a JSON-ready dict."""
    now = now or datetime.now(UTC)
    values = _settings()
    emergency_stop = values.get("control.emergency_stop") is True
    # Anything but an explicit `false` counts as dry run, the reading that posts less.
    dry_run = values.get("launch.dry_run") is not False
    launch_at = _parse_time(values.get("launch.launch_at"))
    stale = values.get("guardian.heartbeat_stale_s")
    stale_after_s = float(stale) if isinstance(stale, int | float) and not isinstance(stale, bool) else None
    stop_reason = values.get("control.stop_reason")
    budget = values.get("ai.daily_budget_usd")
    low = values.get("posting.queue_low_watermark")
    return {
        "env": env().APP_ENV,
        "generated_at": now.isoformat(),
        "posting_mode": posting_mode(emergency_stop, dry_run, launch_at, now),
        "emergency_stop": emergency_stop,
        "stop_reason": stop_reason if isinstance(stop_reason, str) else None,
        "dry_run": dry_run,
        "launch_at": launch_at.isoformat() if launch_at else None,
        "live_sessions": _live_sessions(values.get("live.max_slots")),
        "approved_queue": {
            **_approved_queue(),
            "hourly_low_watermark": low if isinstance(low, int) and not isinstance(low, bool) else None,
        },
        "posts": _posts(),
        "spend_today_usd": {
            **_spend_today(),
            "ai_daily_budget": budget if isinstance(budget, int | float) else None,
        },
        "jobs": _jobs(),
        "heartbeats": _heartbeats(stale_after_s),
    }


def format_age(age_s: int) -> str:
    """`45s`, `12m`, `3h 05m` or `2d 4h`."""
    if age_s < 60:
        return f"{age_s}s"
    if age_s < 3600:
        return f"{age_s // 60}m"
    if age_s < 86400:
        return f"{age_s // 3600}h {age_s % 3600 // 60:02d}m"
    return f"{age_s // 86400}d {age_s % 86400 // 3600}h"


def render_telegram(status: dict[str, Any]) -> str:
    """The snapshot as text for Telegram's HTML parse mode. Every value from the database is escaped."""
    mode = {
        "stopped": "🔴 STOPPED",
        "dry_run": "🟡 dry run (nothing is published)",
        "live": "🟢 live",
    }[status["posting_mode"]]
    lines = [f"<b>6 Stars Clips status</b> ({escape(str(status['env']))})", f"Posting: {mode}"]
    if status["emergency_stop"]:
        lines.append(f"Stop reason: {escape(status['stop_reason'] or 'none given')}")
    if status["launch_at"] and status["posting_mode"] == "dry_run":
        lines.append(
            f"Launch at: {escape(status['launch_at'])} · dry-run switch: {_on_off(status['dry_run'])}"
        )

    live = status["live_sessions"]
    slots = f"/{live['max_slots']}" if live["max_slots"] is not None else ""
    lines.append(f"Live sessions: {live['active']}{slots} (recording {live['by_status']['recording']})")

    queue = status["approved_queue"]
    low = queue["hourly_low_watermark"]
    warn = " ⚠️ low" if low is not None and queue["hourly"] < low else ""
    lines.append(f"Approved queue: spike {queue['spike']} · hourly {queue['hourly']}{warn}")

    lines.append("<b>Posts (last hour / 24 h)</b>")
    for account in status["posts"]["accounts"]:
        notes = []
        if not account["enabled"]:
            notes.append("disabled")
        if account["paused_until"]:
            notes.append(f"paused until {account['paused_until']}")
        if account["failed_last_24h"]:
            notes.append(f"failed {account['failed_last_24h']}")
        if account["dry_run_last_24h"]:
            notes.append(f"dry runs {account['dry_run_last_24h']}")
        suffix = f" ({escape(', '.join(notes))})" if notes else ""
        lines.append(
            f"• {escape(account['account_id'])}: {account['published_last_hour']} / "
            f"{account['published_last_24h']} of {account['cap_24h']}{suffix}"
        )
    if not status["posts"]["accounts"]:
        lines.append("• no accounts")

    spend = status["spend_today_usd"]
    parts = " · ".join(f"{escape(name)} ${amount:.2f}" for name, amount in spend["by_provider"].items())
    budget = f" of ${spend['ai_daily_budget']:g} AI budget" if spend["ai_daily_budget"] is not None else ""
    lines.append(f"Spend today: ${spend['total']:.2f}{budget}" + (f" ({parts})" if parts else ""))

    jobs = status["jobs"]
    lines.append(
        "Jobs: "
        + " · ".join(
            f"{queue_name} {jobs['queued'][queue_name]} queued, {jobs['running'][queue_name]} running, "
            f"{jobs['failed_last_24h'][queue_name]} failed"
            for queue_name in QUEUES
        )
    )

    lines.append("<b>Heartbeats</b>")
    hidden = 0
    for beat in status["heartbeats"]:
        if beat["stale"] and beat["age_s"] > OLD_HEARTBEAT_S:
            hidden += 1  # a worker of an earlier deploy; the JSON format lists them all
            continue
        flag = " ⚠️ stale" if beat["stale"] else ""
        lines.append(f"• {escape(beat['name'])}: {format_age(beat['age_s'])} ago{flag}")
    if hidden:
        lines.append(f"• {hidden} stopped more than {format_age(OLD_HEARTBEAT_S)} ago (not shown)")
    if not status["heartbeats"]:
        lines.append("• none yet")
    return "\n".join(lines)


def _on_off(value: bool) -> str:
    return "on" if value else "off"

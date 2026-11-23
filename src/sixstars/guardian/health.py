"""Health checks the scheduler runs every minute (docs/12 section 5), and the housekeeping that goes
with them. Every check alerts once when it starts failing and once when it is fine again
(`incidents.py`); all thresholds are settings.

- Service heartbeats: no process of an expected service wrote a heartbeat for
  `guardian.heartbeat_stale_s` -> warn.
- Restart storm: `guardian.restart_storm_limit` worker restarts within
  `guardian.restart_storm_window_s` -> critical.
- Queue depth: approved hourly clips below `posting.queue_low_watermark` -> warn.
- Express latency: the last `guardian.express_slow_count` spike posts all slower than
  `guardian.express_slow_s` -> warn.
- Spend: a provider over its daily budget -> warn; Anthropic or transcription also pause the VOD lane.
- Disk: data directory fuller than `guardian.disk_warn_pct` -> warn.
- Stuck jobs: `running` longer than `guardian.stuck_job_s` -> back to the queue, or failed when out of
  attempts.

Jobs of a dead worker are recovered by `jobs.requeue_stale` (M11); the stuck-job rule here is for a job
that hangs on a worker that is still alive.
"""

import random
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sixstars import db, events, notify, settings
from sixstars.clients.proxy_probe import ProxyProbe
from sixstars.config import env
from sixstars.guardian import incidents
from sixstars.guardian.config import TRIGGER_PREFIX
from sixstars.logging import get_logger
from sixstars.vod import proxy

SERVICE = "guardian"
WORKER_PREFIX = "worker-"
QUEUES: tuple[str, ...] = ("express", "normal")
MAX_PER_PASS = 200

# provider names in `api_usage` -> the setting that holds their daily budget; True: pauses the VOD lane
BUDGETS: tuple[tuple[str, tuple[str, ...], str, bool], ...] = (
    ("anthropic", ("anthropic",), "ai.daily_budget_usd", True),
    ("transcription", ("openai", "groq"), "transcribe.daily_budget_usd", True),
    ("apify", ("apify",), "discovery.apify_daily_budget_usd", False),
    # YouTube video jobs pause themselves at this budget (vod/process.py `_gates`); Twitch goes on.
    ("proxy", ("DataImpulse",), "proxy.daily_budget_usd", False),
)


@dataclass(frozen=True, slots=True)
class Beat:
    name: str
    last_seen: datetime


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    service: str
    age_s: float | None  # None: never seen
    stale: bool
    restarts: int


# ── pure rules ───────────────────────────────────────────────


def service_of(name: str) -> str:
    """The service a heartbeat belongs to: `worker-express-host-123` -> `worker-express`; other names
    are their own service."""
    for queue in QUEUES:
        prefix = f"{WORKER_PREFIX}{queue}"
        if name == prefix or name.startswith(prefix + "-"):
            return prefix
    return name


def is_stale(age_s: float | None, stale_after_s: float) -> bool:
    """Older than the limit, or never seen. Exactly at the limit is still fine."""
    return age_s is None or age_s > stale_after_s


def service_health(
    beats: Sequence[Beat],
    expected: Sequence[str],
    now: datetime,
    stale_after_s: float,
    storm_window_s: float,
) -> list[ServiceHealth]:
    """Health of every expected service. A worker service is alive when any of its processes is. Its
    restarts are the processes seen within the window beyond those alive now (a process name holds the
    host and the process id, so every restart shows up as a new name)."""
    out: list[ServiceHealth] = []
    for service in expected:
        ages = [(now - beat.last_seen).total_seconds() for beat in beats if service_of(beat.name) == service]
        youngest = min(ages) if ages else None
        recent = sum(1 for age in ages if age <= storm_window_s)
        alive = sum(1 for age in ages if age <= stale_after_s)
        out.append(
            ServiceHealth(
                service, youngest, is_stale(youngest, stale_after_s), max(0, recent - max(alive, 1))
            )
        )
    return out


def express_slow(latencies_s: Sequence[float], slow_s: float, count: int) -> bool:
    """True when the last `count` spike posts were all slower than `slow_s`."""
    recent = list(latencies_s[: max(1, count)])
    return len(recent) >= max(1, count) and all(value > slow_s for value in recent)


def pause_until(now: datetime, pause_s: float) -> datetime:
    """End of a spend pause: `pause_s` from now, but never past the next 00:00 UTC (budgets are daily)."""
    midnight = (now.astimezone(UTC) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return min(now + timedelta(seconds=pause_s), midnight)


def disk_used_pct(total: int, used: int) -> float:
    return 0.0 if total <= 0 else 100.0 * used / total


# ── checks ───────────────────────────────────────────────────


def db_now() -> datetime:
    """The database clock. Heartbeats are stamped by the database, so their age is measured with the
    same clock: a server whose clock is off by a few seconds must not shift the limits."""
    row = db.fetch_one("select now() as now")
    assert row is not None
    stamp: datetime = row["now"]
    return stamp


def load_beats() -> list[Beat]:
    rows = db.fetch_all(
        "select name, last_seen from heartbeats where name not like %s", (TRIGGER_PREFIX + "%",)
    )
    return [Beat(str(row["name"]), row["last_seen"]) for row in rows]


def check_services(now: datetime, open_now: set[str]) -> list[ServiceHealth]:
    stale_after_s = settings.get_float("guardian.heartbeat_stale_s")
    limit = settings.get_int("guardian.restart_storm_limit")
    window_s = settings.get_float("guardian.restart_storm_window_s")
    expected = [str(name) for name in settings.get_list("guardian.expected_services")]
    health = service_health(load_beats(), expected, now, stale_after_s, window_s)
    for item in health:
        age = "never seen" if item.age_s is None else f"last heartbeat {item.age_s:.0f} s ago"
        incidents.report(
            f"heartbeat:{item.service}",
            item.stale,
            severity="warn",
            title=f"Service not responding: {item.service}",
            body=f"{age} (limit {stale_after_s:g} s). Docker restarts a crashed container by itself; "
            "if this stays, look at the server.",
            recovered_title=f"Service is back: {item.service}",
            data={"service": item.service, "age_s": item.age_s},
            open_now=open_now,
        )
        incidents.report(
            f"restarts:{item.service}",
            item.restarts >= max(1, limit),
            severity="critical",
            title=f"Restart storm: {item.service}",
            body=f"{item.restarts} restarts within {window_s:g} s (limit {limit}). The service keeps "
            "crashing; look at its logs.",
            recovered_title=f"Restart storm over: {item.service}",
            data={"service": item.service, "restarts": item.restarts},
            open_now=open_now,
        )
    return health


def campaign_watcher_stale(age_s: float | None, active_campaigns: int, stale_after_s: float) -> bool:
    """D117: the watcher matters only while a campaign is active; then a missing or old beat is stale."""
    return active_campaigns > 0 and is_stale(age_s, stale_after_s)


def check_campaign_watcher(now: datetime, open_now: set[str]) -> bool:
    """Alert when the campaign watcher stopped while a campaign is active (live campaign streams would be
    missed; discovery still finds the big ones)."""
    row = db.fetch_one(
        "select (select count(*) from clip_campaigns where status = 'active') as active, "
        "(select last_seen from heartbeats where name = 'campaign:watcher') as last_seen"
    )
    assert row is not None
    age = None if row["last_seen"] is None else (now - row["last_seen"]).total_seconds()
    limit = settings.get_float("campaigns.watcher_stale_s", 300.0)
    stale = campaign_watcher_stale(age, int(row["active"]), limit)
    incidents.report(
        "heartbeat:campaign-watcher",
        stale,
        severity="warn",
        title="Campaign watcher not running",
        body=f"{'never seen' if age is None else f'last pass {age:.0f} s ago'} (limit {limit:g} s) while "
        f"{row['active']} campaign(s) are active: live campaign streams can be missed. It runs in the "
        "scheduler service.",
        recovered_title="Campaign watcher is back",
        data={"age_s": age, "active": int(row["active"])},
        open_now=open_now,
    )
    return stale


def check_queue(open_now: set[str]) -> int:
    row = db.fetch_one("select count(*) as n from clips where status = 'approved' and lane = 'hourly'")
    depth = int(row["n"]) if row else 0
    low = settings.get_int("posting.queue_low_watermark")
    incidents.report(
        "queue_low",
        depth < low,
        severity="warn",
        title="Clip queue is low",
        body=f"{depth} approved hourly clip(s), below the watermark of {low}. Discovery widens by itself.",
        recovered_title="Clip queue is filled again",
        data={"depth": depth, "watermark": low},
        open_now=open_now,
    )
    return depth


def check_express(open_now: set[str]) -> bool:
    count = settings.get_int("guardian.express_slow_count")
    slow_s = settings.get_float("guardian.express_slow_s")
    rows = db.fetch_all(
        "select spike_to_post_s from spikes where spike_to_post_s is not null "
        "order by detected_at desc limit %s",
        (max(1, count),),
    )
    latencies = [float(row["spike_to_post_s"]) for row in rows]
    slow = express_slow(latencies, slow_s, count)
    incidents.report(
        "express_slow",
        slow,
        severity="warn",
        title="Spike posts are slow",
        body=f"The last {count} spike posts took {', '.join(f'{value:.0f} s' for value in latencies)} from "
        f"spike to post (limit {slow_s:g} s).",
        recovered_title="Spike posts are fast again",
        data={"latencies_s": latencies},
        open_now=open_now,
    )
    return slow


def spend_today() -> dict[str, float]:
    rows = db.fetch_all(
        """
        select provider, coalesce(sum(cost_usd), 0) as total from api_usage
        where created_at >= date_trunc('day', now(), 'UTC') group by provider
        """
    )
    return {str(row["provider"]): float(row["total"]) for row in rows}


def check_spend(now: datetime, open_now: set[str]) -> dict[str, bool]:
    """Compare today's spend per provider with its budget; over budget pauses the VOD lane."""
    spent = spend_today()
    over: dict[str, bool] = {}
    pause = False
    for label, providers, key, pauses_vod in BUDGETS:
        budget = settings.get(key, None)
        if not isinstance(budget, int | float) or isinstance(budget, bool):
            continue
        total = sum(spent.get(provider, 0.0) for provider in providers)
        over[label] = total > float(budget)
        pause = pause or (over[label] and pauses_vod)
        incidents.report(
            f"spend:{label}",
            over[label],
            severity="warn",
            title=f"Daily budget reached: {label}",
            body=f"${total:.2f} spent today, the budget is ${float(budget):.2f}."
            + (" The VOD lane is paused; spike clips continue." if pauses_vod else ""),
            recovered_title=f"Spend is under budget again: {label}",
            data={"spent_usd": total, "budget_usd": float(budget)},
            open_now=open_now,
        )
    if pause:
        until = pause_until(now, settings.get_float("guardian.spend_pause_s"))
        current = settings.get_dt("vod.paused_until", None)
        if current is None or current < until:
            settings.set("vod.paused_until", until.isoformat())
            if current is None or current <= now:
                events.emit(
                    "vod.paused",
                    level="warn",
                    entity_type="setting",
                    entity_id="vod.paused_until",
                    message="VOD lane paused: daily budget reached",
                    data={"until": until.isoformat(), "over": [name for name, flag in over.items() if flag]},
                    service=SERVICE,
                )
    return over


def check_disk(open_now: set[str], path: Path | None = None) -> float | None:
    target = path or env().DATA_DIR
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return None  # no data directory on this machine (the API container, a test)
    pct = disk_used_pct(usage.total, usage.used)
    limit = settings.get_float("guardian.disk_warn_pct")
    incidents.report(
        "disk",
        pct > limit,
        severity="warn",
        title="Disk is filling up",
        body=f"{target} is {pct:.0f}% full (limit {limit:g}%).",
        recovered_title="Disk has room again",
        data={"used_pct": round(pct, 1)},
        open_now=open_now,
    )
    return pct


def check_proxy(
    now: datetime,
    open_now: set[str],
    probe: ProxyProbe | None = None,
    rand: Callable[[], float] = random.random,
) -> dict[str, Any] | None:
    """The residential proxy of YouTube downloads (only when `PROXY_URL` is set).

    Every `proxy.health_interval_s` one tiny request through a random sticky port; the answer is kept in
    `proxy.health` (shown by the blocked-downloads alert). A failing proxy -> critical, with what it
    answered. Then the traffic of the billing cycle: past `proxy.low_traffic_share` of `proxy.cycle_gb`
    since `proxy.cycle_started_at` -> warn to top up."""
    secret = env().PROXY_URL
    if secret is None:
        return None
    cfg = proxy.ProxyConfig.load()
    state = proxy.health_state()
    checked = settings.get_dt("proxy.health_checked_at", None)
    if state is None or checked is None or (now - checked).total_seconds() >= cfg.health_interval_s:
        url = secret.get_secret_value()
        ports = proxy.sticky_range()
        if ports is not None:
            url = proxy.with_port(url, proxy.pick_port(ports[0], ports[1], rand))
        result = (probe or ProxyProbe()).probe(url)
        state = {"ok": result.ok, "reason": result.reason, "checked_at": now.isoformat()}
        settings.set("proxy.health", state)
        settings.set("proxy.health_checked_at", now.isoformat())
    name = proxy.provider_name(secret.get_secret_value())
    reason = str(state.get("reason", "?"))
    incidents.report(
        "proxy:failing",
        not state.get("ok"),
        severity="critical",
        title=f"{name} proxy failing: {reason} — top up or check credentials",
        body="YouTube video downloads wait until the proxy answers again (Twitch is not affected). "
        "407: wrong credentials or suspended account; 402: no traffic left.",
        recovered_title=f"{name} proxy works again",
        data={"reason": reason},
        open_now=open_now,
    )
    used = proxy.used_gb(cfg.cycle_started_at)
    low = proxy.low_traffic(used, cfg.cycle_gb, cfg.low_traffic_share)
    incidents.report(
        "proxy:traffic",
        low,
        severity="warn",
        title=f"{name} traffic running low: top up",
        body=f"{used:.2f} of {cfg.cycle_gb:g} GB used since {cfg.cycle_started_at:%Y-%m-%d} (our own count). "
        "After topping up, set proxy.cycle_gb to the new total and proxy.cycle_started_at to now.",
        recovered_title=f"{name} traffic topped up",
        data={"used_gb": round(used, 3), "cycle_gb": cfg.cycle_gb},
        open_now=open_now,
    )
    return {"ok": bool(state.get("ok")), "reason": reason, "cycle_used_gb": round(used, 3)}


def run_checks(now: datetime) -> dict[str, Any]:
    """All health checks of one pass. One check that fails does not stop the others."""
    log = get_logger(SERVICE)
    open_now = incidents.open_keys()
    summary: dict[str, Any] = {}
    checks: Mapping[str, Any] = {
        "services": lambda: [item.service for item in check_services(now, open_now) if item.stale],
        "queue": lambda: check_queue(open_now),
        "express_slow": lambda: check_express(open_now),
        "spend_over": lambda: check_spend(now, open_now),
        "disk_pct": lambda: check_disk(open_now),
        "proxy": lambda: check_proxy(now, open_now),
        "campaign_watcher": lambda: check_campaign_watcher(now, open_now),
    }
    for name, check in checks.items():
        try:
            summary[name] = check()
        except Exception as exc:
            log.error("health_check_failed", check=name, error=type(exc).__name__, exc_info=True)
    summary["open_incidents"] = sorted(open_now)
    return summary


# ── housekeeping ─────────────────────────────────────────────


def reset_stuck_jobs() -> int:
    """Jobs `running` longer than `guardian.stuck_job_s`: back to the queue with their attempts kept, or
    `failed` when none are left."""
    stuck_s = settings.get_float("guardian.stuck_job_s")
    rows = db.fetch_all(
        """
        with stuck as (
          select id, locked_by from jobs
          where status = 'running' and locked_at < now() - make_interval(secs => %(stuck)s)
          for update skip locked
        )
        update jobs j
        set status = case when j.attempts < j.max_attempts then 'queued' else 'failed' end,
            run_after = case when j.attempts < j.max_attempts then now() else j.run_after end,
            last_error = %(error)s, locked_by = null, locked_at = null
        from stuck where j.id = stuck.id
        returning j.id, j.queue, j.type, j.status, j.attempts, stuck.locked_by
        """,
        {"stuck": stuck_s, "error": f"reset by the guardian: running longer than {stuck_s:g} s"},
    )
    for row in rows:
        events.emit(
            "job.stuck_reset",
            level="warn" if row["status"] == "queued" else "error",
            entity_type="job",
            entity_id=row["id"],
            message=f"job ran longer than {stuck_s:g} s on {row['locked_by']}: {row['status']}",
            data={
                "queue": row["queue"],
                "type": row["type"],
                "status": row["status"],
                "attempts": row["attempts"],
            },
            service=SERVICE,
        )
    if rows:
        lines = [f"#{row['id']} {row['type']} ({row['queue']}) -> {row['status']}" for row in rows[:10]]
        notify.send("warn", "Stuck jobs reset", f"{len(rows)} job(s) ran too long.\n" + "\n".join(lines))
    return len(rows)


def expire_clips(now: datetime) -> int:
    """Approved clips older than `posting.max_clip_age_h` are `expired` (the planner already skips them)."""
    max_age_h = settings.get_float("posting.max_clip_age_h")
    rows = db.fetch_all(
        """
        update clips set status = 'expired'
        where id in (
          select id from clips
          where status = 'approved' and approved_at < %s
          order by approved_at limit %s
          for update skip locked
        )
        returning id::text as id, lane
        """,
        (now - timedelta(hours=max_age_h), MAX_PER_PASS),
    )
    for row in rows:
        events.emit(
            "clip.expired",
            entity_type="clip",
            entity_id=row["id"],
            message=f"approved clip not posted within {max_age_h:g} h",
            data={"lane": row["lane"], "max_age_h": max_age_h},
            service=SERVICE,
        )
    return len(rows)


def prune(now: datetime) -> dict[str, int]:
    """Delete old audit events and discovery candidates (docs/21 section 4). Incident events are kept:
    they hold the open or closed state of each alert."""
    events_days = settings.get_float("cleanup.events_days")
    candidates_days = settings.get_float("cleanup.candidates_days")
    deleted_events = db.execute(
        "delete from events where created_at < %s and type not in (%s, %s)",
        (now - timedelta(days=events_days), incidents.OPENED, incidents.CLOSED),
    )
    deleted_candidates = db.execute(
        "delete from candidates where checked_at < %s", (now - timedelta(days=candidates_days),)
    )
    return {"events": deleted_events, "candidates": deleted_candidates}

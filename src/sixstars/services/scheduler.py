"""Process entry point for the scheduler service.

M11 tasks:

- the `scheduler` heartbeat, every 30 s;
- `notify.retry_unsent()`, every minute, so a Telegram message that the webhook refused is sent later;
- recovery of jobs left `running` by a dead worker, every minute (`jobs.requeue_stale`), with the
  threshold `guardian.heartbeat_stale_s`;
- the ranking refresh (docs/11 section 5), every 10 minutes: `rank_score` of every approved clip.
- the campaign watcher (D118), every `campaigns.watch_interval_s`: live campaign streams get their slots;

M13 tasks (the rules live in `sixstars.guardian`):

- health checks and failover for the n8n triggers, every minute (`guardian.health`, `guardian.failover`);
- reset of jobs that hang on a live worker, every 5 minutes (`guardian.stuck_job_s`);
- expiry of approved clips older than `posting.max_clip_age_h`, every 10 minutes;
- pruning of old events and candidates, every hour;
- one health summary shortly before `launch.launch_at`.

fix-stale-processing: videos left in `processing` by a restarted worker are put back to `new`
(`vod.process.recover_orphans`), every `vod.recovery_interval_s`.

Each task is independent: one that fails is logged and tried again after its interval. The process stops
cleanly on SIGINT or SIGTERM.
"""

import html
import os
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sixstars import db, events, jobs, notify, settings
from sixstars.campaigns import watcher as campaign_watcher
from sixstars.guardian import config as guardian_config
from sixstars.guardian import failover as guardian_failover
from sixstars.guardian import health as guardian_health
from sixstars.guardian import service as guardian_service
from sixstars.logging import configure_logging, get_logger
from sixstars.posting import ranking
from sixstars.posting.params import PostingParams
from sixstars.posting.precedence import demoted_rank
from sixstars.posting.store import DbStore, utcnow
from sixstars.services._runtime import PeriodicTask, install_stop_signals, run_loop

SERVICE = "scheduler"
HEARTBEAT_INTERVAL_S = 30.0
NOTIFY_RETRY_INTERVAL_S = 60.0
STALE_JOBS_INTERVAL_S = 60.0
RANKING_INTERVAL_S = 600.0  # M11 and docs/11 section 5: every 10 minutes (no settings key)
GUARDIAN_HEALTH_INTERVAL_S = 60.0  # docs/12 section 5: every 60 s
STUCK_JOBS_INTERVAL_S = 300.0
CLIP_EXPIRY_INTERVAL_S = 600.0
PRUNE_INTERVAL_S = 3600.0
LAUNCH_SUMMARY_INTERVAL_S = 60.0
VOD_RECOVERY_TICK_S = 15.0  # how often the task looks; the pass itself runs every vod.recovery_interval_s
VOD_RECOVERY_SETTING = "vod.recovery_interval_s"
CAMPAIGN_WATCH_TICK_S = 10.0  # how often the task looks; the pass runs every campaigns.watch_interval_s
CAMPAIGN_WATCH_SETTING = "campaigns.watch_interval_s"
LAUNCH_SUMMARY_EVENT = "launch.health_summary"
STALE_SETTING = "guardian.heartbeat_stale_s"
RANK_EPSILON = 1e-6


def heartbeat() -> None:
    db.heartbeat(SERVICE, {"pid": os.getpid()})


def retry_notifications() -> int:
    """Send the Telegram messages that are still in the outbox."""
    delivered = notify.retry_unsent()
    if delivered:
        get_logger(SERVICE).info("notify_retry_delivered", delivered=delivered)
    return delivered


def recover_stale_jobs() -> list[jobs.RecoveredJob]:
    """Take back the jobs of workers whose heartbeat is older than `guardian.heartbeat_stale_s`.

    Every recovered job is an event; one warning goes to Telegram per pass that found any.
    """
    log = get_logger(SERVICE)
    stale_after_s = settings.get_float(STALE_SETTING)
    recovered = jobs.requeue_stale(stale_after_s)
    for job in recovered:
        log.warning(
            "job_recovered",
            job_id=job.id,
            job_type=job.type,
            queue=job.queue,
            status=job.status,
            attempts=job.attempts,
            worker=job.locked_by,
        )
        events.emit(
            "job.recovered",
            level="warn" if job.status == "queued" else "error",
            entity_type="job",
            entity_id=job.id,
            message=f"worker {job.locked_by} stopped without finishing the job; now {job.status}",
            data={
                "queue": job.queue,
                "type": job.type,
                "status": job.status,
                "attempts": job.attempts,
                "worker": job.locked_by,
                "stale_after_s": stale_after_s,
            },
            service=SERVICE,
        )
    if recovered:
        failed = [job for job in recovered if job.status == "failed"]
        lines = [f"#{job.id} {job.type} ({job.queue}) -> {job.status}" for job in recovered[:10]]
        if len(recovered) > len(lines):
            lines.append(f"... and {len(recovered) - len(lines)} more")
        notify.send(
            "warn",
            "Jobs recovered from a dead worker",
            f"{len(recovered)} job(s) taken back, {len(failed)} out of attempts.\n" + "\n".join(lines),
        )
    return recovered


def _new_rank(row: dict[str, Any], now: datetime, params: PostingParams, use_visual: bool = True) -> float:
    reference = row["approved_at"]
    if row["source_type"] == "live" and row["spike_wall"] is not None:
        reference = row["spike_wall"]
    rank = ranking.rank_score(
        ranking.rank_input_score(float(row["score"]), row.get("score_parts"), use_visual),
        row["source_type"],
        row["spike_tier"],
        row["creator_tier"] in ("campaign", "preferred"),
        reference,
        now,
        params,
    )
    # R7: a spike clip that was moved to the hourly lane keeps its boost across refreshes.
    return demoted_rank(rank) if row["demoted"] else rank


def refresh_ranking(now: datetime | None = None) -> int:
    """Recompute `clips.rank_score` for every approved clip that has a score (docs/11 section 5).

    Returns how many clips changed. Clips are not expired here: the planner already ignores a clip older
    than `posting.max_clip_age_h`, and marking them `expired` is part of the M13 scheduler.
    """
    now = now or utcnow()
    params = DbStore().load_params()
    use_visual = settings.get_bool(ranking.RANK_USES_VISUAL_SETTING, True)
    rows = db.fetch_all(
        """
        select c.id::text as id, c.score, c.score_parts, c.rank_score, c.source_type, c.approved_at,
               s.tier as spike_tier, s.spike_wall, cr.tier as creator_tier,
               c.lane = 'hourly' and exists (
                 select 1 from events e
                 where e.type = 'clip.demoted' and e.entity_type = 'clip' and e.entity_id = c.id::text
               ) as demoted
        from clips c
        join creators cr on cr.id = c.creator_id
        left join spikes s on s.id = c.spike_id
        where c.status = 'approved' and c.score is not null and c.approved_at is not null
        """
    )
    ids: list[str] = []
    ranks: list[float] = []
    for row in rows:
        rank = _new_rank(row, now, params, use_visual)
        old = row["rank_score"]
        if old is None or abs(float(old) - rank) > RANK_EPSILON:
            ids.append(row["id"])
            ranks.append(rank)
    changed = 0
    if ids:
        changed = db.execute(
            """
            update clips c set rank_score = v.rank
            from unnest(%s::uuid[], %s::numeric[]) as v(id, rank)
            where c.id = v.id and c.status = 'approved'
            """,
            (ids, ranks),
        )
    get_logger(SERVICE).info("ranking_refreshed", clips=len(rows), changed=changed)
    return changed


# ── M13: guardian health, failover, expiry, pruning ──────────


def _failover_hourly(now: datetime) -> Mapping[str, Any] | None:
    from sixstars.posting.hourly import schedule_hourly_slot

    result = schedule_hourly_slot(now)  # one job per slot, however often it is called
    return {"slot_at": result.slot_at.isoformat(), "status": result.status}


def _failover_discovery(now: datetime) -> Mapping[str, Any] | None:
    from sixstars.discovery.run import run_discovery

    run_discovery()
    return None


def _failover_guardian(now: datetime) -> Mapping[str, Any] | None:
    guardian_service.run_check(now)
    return None


def _failover_stats(now: datetime) -> Mapping[str, Any] | None:
    guardian_service.collect_stats(now)
    return None


FAILOVER_RUNNERS: dict[str, guardian_failover.Runner] = {
    "hourly": _failover_hourly,
    "discovery": _failover_discovery,
    "guardian": _failover_guardian,
    "stats": _failover_stats,
}

_failover: guardian_failover.Failover | None = None


def failover() -> guardian_failover.Failover:
    """The failover runner of this process. Its start time is the reference for a trigger that has
    never fired."""
    global _failover
    if _failover is None:
        _failover = guardian_failover.Failover(FAILOVER_RUNNERS, guardian_health.db_now())
    return _failover


def guardian_health_pass(now: datetime | None = None, wait: bool = False) -> dict[str, Any]:
    """Health checks, then failover for missing n8n triggers. A failed health check never keeps the
    failover from running. Ages are measured with the database clock, which stamps the heartbeats."""
    now = now or guardian_health.db_now()
    summary: dict[str, Any] = {}
    try:
        summary = guardian_health.run_checks(now)
    except Exception as exc:
        get_logger(SERVICE).error("health_checks_failed", error=type(exc).__name__, exc_info=True)
    summary["failover_started"] = failover().check(
        now,
        guardian_config.seconds_map("guardian.failover_grace_s"),
        guardian_config.seconds_map("guardian.failover_interval_s"),
        settings.get_bool("guardian.failover_enabled"),
        wait=wait,
    )
    return summary


def reset_stuck_jobs() -> int:
    return guardian_health.reset_stuck_jobs()


def expire_clips(now: datetime | None = None) -> int:
    expired = guardian_health.expire_clips(now or utcnow())
    if expired:
        get_logger(SERVICE).info("clips_expired", clips=expired)
    return expired


def prune(now: datetime | None = None) -> dict[str, int]:
    deleted = guardian_health.prune(now or utcnow())
    get_logger(SERVICE).info("pruned", **deleted)
    return deleted


def launch_summary(now: datetime | None = None) -> bool:
    """Send one health summary in the last `launch.summary_lead_s` seconds before `launch.launch_at`."""
    now = now or utcnow()
    launch_at = settings.get_dt("launch.launch_at", None)
    if launch_at is None:
        return False
    lead = timedelta(seconds=settings.get_float("launch.summary_lead_s"))
    if not launch_at - lead <= now < launch_at:
        return False
    if db.fetch_one("select 1 as hit from events where type = %s limit 1", (LAUNCH_SUMMARY_EVENT,)):
        return False
    from sixstars.api import status as status_view

    text = html.unescape(re.sub(r"</?b>", "", status_view.render_telegram(status_view.collect(now))))
    events.emit(
        LAUNCH_SUMMARY_EVENT,
        message="health summary before launch",
        data={"launch_at": launch_at.astimezone(UTC).isoformat()},
        service=SERVICE,
    )
    notify.send("info", f"Health summary before launch ({launch_at.astimezone(UTC):%H:%M} UTC)", text)
    return True


_vod_recovery_at: float | None = None


def recover_vod_orphans(clock: float | None = None) -> list[str] | None:
    """Put videos left in `processing` by a restarted worker back to `new`, at most once per
    `vod.recovery_interval_s` (read on every tick, so a change applies without a restart). Returns the
    ids reset, or None when the pass is not due yet."""
    global _vod_recovery_at
    from sixstars.vod import config as vod_config
    from sixstars.vod import process as vod_process

    clock = time.monotonic() if clock is None else clock
    interval_s = settings.get_float(VOD_RECOVERY_SETTING, vod_config.DEFAULTS[VOD_RECOVERY_SETTING])
    if _vod_recovery_at is not None and clock - _vod_recovery_at < interval_s:
        return None
    _vod_recovery_at = clock
    return vod_process.recover_orphans()


_campaign_watch_at: float | None = None


def watch_campaigns(clock: float | None = None) -> dict[str, Any] | None:
    """D118: one campaign watcher pass every `campaigns.watch_interval_s` (read each tick, so a change
    applies without a restart). Returns the pass summary, or None when it is not due."""
    global _campaign_watch_at
    clock = time.monotonic() if clock is None else clock
    interval_s = settings.get_float(CAMPAIGN_WATCH_SETTING, 60.0)
    if _campaign_watch_at is not None and clock - _campaign_watch_at < interval_s:
        return None
    _campaign_watch_at = clock
    return campaign_watcher.run_once()


def tasks() -> list[PeriodicTask]:
    """The periodic tasks of the scheduler."""
    return [
        PeriodicTask("heartbeat", HEARTBEAT_INTERVAL_S, heartbeat),
        PeriodicTask("notify_retry", NOTIFY_RETRY_INTERVAL_S, retry_notifications),
        PeriodicTask("stale_jobs", STALE_JOBS_INTERVAL_S, recover_stale_jobs),
        PeriodicTask("ranking_refresh", RANKING_INTERVAL_S, refresh_ranking),
        PeriodicTask("guardian_health", GUARDIAN_HEALTH_INTERVAL_S, guardian_health_pass),
        PeriodicTask("stuck_jobs", STUCK_JOBS_INTERVAL_S, reset_stuck_jobs),
        PeriodicTask("clip_expiry", CLIP_EXPIRY_INTERVAL_S, expire_clips),
        PeriodicTask("prune", PRUNE_INTERVAL_S, prune),
        PeriodicTask("launch_summary", LAUNCH_SUMMARY_INTERVAL_S, launch_summary),
        PeriodicTask("vod_recovery", VOD_RECOVERY_TICK_S, recover_vod_orphans),
        PeriodicTask("campaign_watch", CAMPAIGN_WATCH_TICK_S, watch_campaigns),
    ]


def run(stop_event: threading.Event | None = None, tick_s: float = 1.0) -> None:
    """Run the scheduler until a stop signal arrives (or `stop_event` is set)."""
    configure_logging()
    log = get_logger(SERVICE)
    stop = stop_event or threading.Event()
    install_stop_signals(stop, log)
    periodic = tasks()
    log.info("scheduler_started", tasks=[task.name for task in periodic])
    try:
        run_loop(periodic, stop, log, tick_s=tick_s)
    finally:
        log.info("scheduler_stopped")
        db.close_pool()

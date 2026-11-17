"""Process entry point for the scheduler service.

M11 scope (the failover triggers, expiry and cleanup arrive with M13):

- the `scheduler` heartbeat, every 30 s;
- `notify.retry_unsent()`, every minute, so a Telegram message that the webhook refused is sent later;
- recovery of jobs left `running` by a dead worker, every minute (`jobs.requeue_stale`), with the
  threshold `guardian.heartbeat_stale_s`;
- the ranking refresh (docs/11 section 5), every 10 minutes: `rank_score` of every approved clip.

Each task is independent: one that fails is logged and tried again after its interval. The process stops
cleanly on SIGINT or SIGTERM.
"""

import os
import threading
from datetime import datetime
from typing import Any

from sixstars import db, events, jobs, notify, settings
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


def _new_rank(row: dict[str, Any], now: datetime, params: PostingParams) -> float:
    reference = row["approved_at"]
    if row["source_type"] == "live" and row["spike_wall"] is not None:
        reference = row["spike_wall"]
    rank = ranking.rank_score(
        float(row["score"]),
        row["source_type"],
        row["spike_tier"],
        row["creator_tier"] == "preferred",
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
    rows = db.fetch_all(
        """
        select c.id::text as id, c.score, c.rank_score, c.source_type, c.approved_at,
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
        rank = _new_rank(row, now, params)
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


def tasks() -> list[PeriodicTask]:
    """The periodic tasks of the scheduler. Later milestones (M13) add theirs to this list."""
    return [
        PeriodicTask("heartbeat", HEARTBEAT_INTERVAL_S, heartbeat),
        PeriodicTask("notify_retry", NOTIFY_RETRY_INTERVAL_S, retry_notifications),
        PeriodicTask("stale_jobs", STALE_JOBS_INTERVAL_S, recover_stale_jobs),
        PeriodicTask("ranking_refresh", RANKING_INTERVAL_S, refresh_ranking),
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

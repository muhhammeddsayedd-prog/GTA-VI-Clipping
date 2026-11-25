"""Plan the daily episode (M20 L2): choose the moments and write `episodes` + `episode_items`.

The scheduler calls `due_tick` every few minutes; once the build hour has come and today's episode is not
planned yet, it enqueues one `longform.plan` job (deduplicated per day). The job runs `plan_episode`.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sixstars import db, events, jobs, notify
from sixstars.logging import get_logger
from sixstars.longform import build
from sixstars.longform.config import LongformConfig
from sixstars.longform.pool import load_candidates
from sixstars.longform.selector import Limits, Selection, select

JOB_PLAN = "longform.plan"
log = get_logger(__name__)


@dataclass(frozen=True)
class PlanResult:
    episode_id: str
    episode_date: date
    status: str
    n_moments: int
    est_s: float
    created: bool


def limits_of(cfg: LongformConfig) -> Limits:
    return Limits(
        target_s=cfg.target_s,
        min_s=cfg.min_s,
        max_s=cfg.max_s,
        segment_min_s=cfg.segment_min_s,
        segment_max_s=cfg.segment_max_s,
        lead_s=cfg.lead_s,
        tail_s=cfg.tail_s,
        max_per_creator=cfg.max_per_creator,
        max_creator_s=cfg.max_creator_s,
        pool_hours=float(cfg.pool_hours),
    )


def existing(day: date) -> dict[str, Any] | None:
    row = db.fetch_one(
        "select id::text as id, status, n_moments, duration_s from episodes where episode_date = %s", (day,)
    )
    return None if row is None else dict(row)


def write_plan(day: date, sel: Selection, account_id: str) -> str:
    """Insert the episode and its moments in one transaction; returns the episode id."""
    status = "skipped" if sel.skipped else "planned"
    with db.transaction() as conn:
        row = conn.execute(
            """
            insert into episodes (episode_date, status, n_moments, duration_s, account_id, error)
            values (%s, %s, %s, %s, %s, %s)
            returning id::text
            """,
            (day, status, len(sel.picks), round(sel.total_s, 1), account_id, sel.skipped_reason),
        ).fetchone()
        assert row is not None
        episode_id = str(row[0])
        for pick in sel.picks:
            conn.execute(
                """
                insert into episode_items (episode_id, position, clip_id, creator_id, est_s)
                values (%s, %s, %s, %s, %s)
                """,
                (episode_id, pick.position, pick.candidate.clip_id, pick.candidate.creator_id, pick.est_s),
            )
    return episode_id


def summary(sel: Selection) -> str:
    """Telegram body: length, moments, creators and the top five."""
    if sel.skipped:
        return sel.skipped_reason or "skipped"
    minutes, seconds = divmod(int(sel.total_s), 60)
    creators = len({p.candidate.creator_id for p in sel.picks})
    top = sorted(sel.picks, key=lambda p: p.position)[:5]
    lines = [f"{len(sel.picks)} moments, about {minutes}:{seconds:02d}, {creators} creators", "Top 5:"]
    lines += [f"#{p.position} {p.candidate.creator_name}: {p.candidate.title[:70]}" for p in top]
    return "\n".join(lines)


def plan_episode(day: date, now: datetime | None = None, cfg: LongformConfig | None = None) -> PlanResult:
    """Plan `day`'s episode once. An episode already planned for that day is returned unchanged."""
    cfg = cfg or LongformConfig.load()
    now = now or datetime.now(UTC)
    found = existing(day)
    if found is not None:
        n_moments = int(found["n_moments"] or 0)
        est_s = float(found["duration_s"] or 0)
        return PlanResult(found["id"], day, found["status"], n_moments, est_s, False)
    sel = select(load_candidates(cfg, now), limits_of(cfg), cfg.rank_weights, now)
    episode_id = write_plan(day, sel, cfg.account_id)
    status = "skipped" if sel.skipped else "planned"
    body = summary(sel)
    events.emit(
        f"episode.{status}",
        "warn" if sel.skipped else "info",
        "episode",
        episode_id,
        body.splitlines()[0],
        {"episode_date": day.isoformat(), "n_moments": len(sel.picks), "est_s": round(sel.total_s, 1)},
    )
    notify.send("warn" if sel.skipped else "info", f"Episode {day.isoformat()} {status}", body)
    log.info("episode_planned", episode_id=episode_id, status=status, n_moments=len(sel.picks))
    if status == "planned" and cfg.auto_build:
        build.start_build(episode_id, cfg)
    return PlanResult(episode_id, day, status, len(sel.picks), sel.total_s, True)


def due_tick(now: datetime | None = None, cfg: LongformConfig | None = None) -> int | None:
    """Scheduler tick: enqueue today's `longform.plan` once the build hour has come. Returns the job id."""
    cfg = cfg or LongformConfig.load()
    now = now or datetime.now(UTC)
    if not cfg.enabled or now.hour < cfg.build_hour_utc:
        return None
    day = now.date()
    if existing(day) is not None:
        return None
    return jobs.enqueue(
        "normal", JOB_PLAN, {"episode_date": day.isoformat()}, cfg.priority, dedupe_key=f"{JOB_PLAN}:{day}"
    )


def handle_plan(job: jobs.Job) -> None:
    """Job `longform.plan`: plan the episode of `payload.episode_date`."""
    plan_episode(date.fromisoformat(str(job.payload["episode_date"])))


def register_handlers() -> None:
    """Register the long-form job handlers with the worker (safe to call more than once)."""
    from sixstars.longform import assemble
    from sixstars.services import worker

    registered = (
        (JOB_PLAN, handle_plan),
        (build.JOB_ITEM, build.handle_item),
        (build.JOB_RENDER, build.handle_render),
        (build.JOB_ASSEMBLE, assemble.handle_assemble),
    )
    for job_type, handler in registered:
        if worker.HANDLERS.get(job_type) is not handler:
            worker.register(job_type, handler)

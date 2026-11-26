"""Plan the daily episode (M20 L2): choose the moments and write `episodes` + `episode_items`.

The scheduler calls `due_tick` every few minutes; once the build hour has come and today's episode is not
planned yet, it enqueues one `longform.plan` job (deduplicated per day). The job runs `plan_episode`.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sixstars import db, events, jobs, notify
from sixstars.logging import get_logger
from sixstars.longform import build
from sixstars.longform.config import LongformConfig
from sixstars.longform.pool import load_candidates
from sixstars.longform.rotation import format_for
from sixstars.longform.selector import CreatorDay, Limits, Selection, SoloLimits, select, select_creator_day

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
    format: str = "mix"


@dataclass(frozen=True)
class Plan:
    """What the planner decided: the format, the featured creator (creator day) and the selection."""

    format: str
    selection: Selection
    featured_creator_id: str | None = None
    featured_name: str | None = None
    fallback: str | None = None  # why a creator day became a mix day


LAST_FORMAT_SQL = """
select format from episodes
 where episode_date < %s and status not in ('skipped', 'failed')
 order by episode_date desc limit 1
"""

COOLING_SQL = """
select distinct featured_creator_id::text as id from episodes
 where format = 'creator' and featured_creator_id is not null
   and status not in ('skipped', 'failed')
   and episode_date >= %s and episode_date < %s
"""


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
        "select id::text as id, status, n_moments, duration_s, format from episodes where episode_date = %s",
        (day,),
    )
    return None if row is None else dict(row)


def solo_limits(cfg: LongformConfig) -> SoloLimits:
    return SoloLimits(
        min_s=cfg.solo.min_s,
        target_s=cfg.solo.target_s,
        max_s=cfg.solo.max_s,
        min_moments=cfg.solo.min_moments,
    )


def last_format(day: date) -> str | None:
    row = db.fetch_one(LAST_FORMAT_SQL, (day,))
    return None if row is None else str(row["format"])


def cooling_creators(day: date, cooldown_days: int) -> frozenset[str]:
    rows = db.fetch_all(COOLING_SQL, (day - timedelta(days=cooldown_days), day))
    return frozenset(str(r["id"]) for r in rows)


def choose(day: date, now: datetime, cfg: LongformConfig) -> Plan:
    """The day's format and moments. A creator day nobody can fill becomes a mix day."""
    fmt = format_for(last_format(day), cfg.format_cycle)
    limits = limits_of(cfg)
    if fmt == "creator":
        pool = load_candidates(cfg, now, pool_hours=cfg.solo.pool_hours, min_score=cfg.solo.min_score)
        solo_lim = Limits(**{**limits.__dict__, "pool_hours": float(cfg.solo.pool_hours)})
        cooling = cooling_creators(day, cfg.solo.cooldown_days)
        picked: CreatorDay | None = select_creator_day(
            pool, solo_lim, solo_limits(cfg), cfg.rank_weights, now, cooling
        )
        if picked is not None:
            return Plan("creator", picked.selection, picked.creator_id, picked.creator_name)
        fallback = (
            f"no creator has {cfg.solo.min_moments}+ moments scored {cfg.solo.min_score:g}+ "
            f"and {cfg.solo.min_s:.0f}s "
            f"in the last {cfg.solo.pool_hours} h outside the {cfg.solo.cooldown_days}-day cooldown"
        )
        return Plan(
            "mix", select(load_candidates(cfg, now), limits, cfg.rank_weights, now), fallback=fallback
        )
    return Plan("mix", select(load_candidates(cfg, now), limits, cfg.rank_weights, now))


def write_plan(day: date, plan: Plan, account_id: str) -> str:
    """Insert the episode and its moments in one transaction; returns the episode id."""
    sel = plan.selection
    status = "skipped" if sel.skipped else "planned"
    with db.transaction() as conn:
        row = conn.execute(
            """
            insert into episodes (episode_date, status, n_moments, duration_s, account_id, error, format,
                                  featured_creator_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            returning id::text
            """,
            (
                day,
                status,
                len(sel.picks),
                round(sel.total_s, 1),
                account_id,
                sel.skipped_reason,
                plan.format,
                plan.featured_creator_id,
            ),
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


def summary(plan: Plan) -> str:
    """Telegram body: format, length, moments, creators and the top five."""
    sel = plan.selection
    if sel.skipped:
        return sel.skipped_reason or "skipped"
    minutes, seconds = divmod(int(sel.total_s), 60)
    creators = len({p.candidate.creator_id for p in sel.picks})
    top = sorted(sel.picks, key=lambda p: p.position)[:5]
    lines = [f"Creator day: {plan.featured_name}" if plan.format == "creator" else "Mix day"]
    if plan.fallback:
        lines.append(f"(creator day fell back to a mix day: {plan.fallback})")
    lines += [f"{len(sel.picks)} moments, about {minutes}:{seconds:02d}, {creators} creators", "Top 5:"]
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
        fmt = str(found.get("format") or "mix")
        return PlanResult(found["id"], day, found["status"], n_moments, est_s, False, fmt)
    plan = choose(day, now, cfg)
    sel = plan.selection
    episode_id = write_plan(day, plan, cfg.account_id)
    status = "skipped" if sel.skipped else "planned"
    body = summary(plan)
    events.emit(
        f"episode.{status}",
        "warn" if sel.skipped else "info",
        "episode",
        episode_id,
        body.splitlines()[0],
        {
            "episode_date": day.isoformat(),
            "n_moments": len(sel.picks),
            "est_s": round(sel.total_s, 1),
            "format": plan.format,
            "featured_creator_id": plan.featured_creator_id,
            "fallback": plan.fallback,
        },
    )
    notify.send("warn" if sel.skipped else "info", f"Episode {day.isoformat()} {status}", body)
    log.info(
        "episode_planned", episode_id=episode_id, status=status, n_moments=len(sel.picks), format=plan.format
    )
    if status == "planned" and cfg.auto_build:
        build.start_build(episode_id, cfg)
    return PlanResult(episode_id, day, status, len(sel.picks), sel.total_s, True, plan.format)


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
    from sixstars.longform import assemble, publish
    from sixstars.services import worker

    registered = (
        (JOB_PLAN, handle_plan),
        (build.JOB_ITEM, build.handle_item),
        (build.JOB_RENDER, build.handle_render),
        (build.JOB_ASSEMBLE, assemble.handle_assemble),
        (publish.JOB_PUBLISH, publish.handle_publish),
    )
    for job_type, handler in registered:
        if worker.HANDLERS.get(job_type) is not handler:
            worker.register(job_type, handler)

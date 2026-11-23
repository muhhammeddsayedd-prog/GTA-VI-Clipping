"""Queue-aware supply throttle before the clip writer (P2): AI cost plan A5b (D117).

In production (2026-11-18 to 11-22) the hourly queue held 4-5 times what posting could use: about 33 approved
clips expired every day, and 1,956 live hourly clips were written for 17 posts. This module decides, before
P2 is paid for, whether an hourly-lane candidate is weaker than what is already waiting to post.

- **Effective queue:** approved hourly clips that can still post before `posting.max_clip_age_h`. Per hourly
  account, the clips in posting order (`clips.rank_score`) that the account's pace over the last 24 hours
  reaches before each clip expires; the largest of those counts.
- **Pre-P2 rank:** the spike's chat percentile (0-100) x the posting spike bonus of its tier x the posting
  freshness of its age (`posting.ranking`, the same multipliers and half-lives the posting rank uses). Queued
  clips are ranked the same way, now.
- **Rule:** would skip when the effective queue holds at least `factory.pre_p2_queue_min` clips and the
  candidate's rank is below the median rank of the ranked clips in it. Spike-lane clips, tier S and clips
  without a percentile are never throttled.

Mode `factory.pre_p2_throttle`: `off`; `log` (every decision is written to `scoring_log`, stage
`factory.pre_p2_throttle`, and the clip goes on whatever it says); `enforce` (a would-skip is rejected with
reason `pre_p2_throttle: ...`). The owner reviews the log before `enforce`.
"""

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sixstars import db, settings
from sixstars.logging import get_logger
from sixstars.posting.params import PostingParams
from sixstars.posting.ranking import SPIKE_BONUS, freshness, half_life_h
from sixstars.scoring import log as scoring_log
from sixstars.scoring.percentile import spike_percentile

MODE_SETTING = "factory.pre_p2_throttle"
QUEUE_MIN_SETTING = "factory.pre_p2_queue_min"
STAGE = "factory.pre_p2_throttle"
REASON_PREFIX = "pre_p2_throttle"
# Fewer ranked clips than this in the effective queue: no median worth comparing with.
MIN_RANKED = 5
HOURS_PER_DAY = 24.0

Mode = Literal["off", "log", "enforce"]


def mode() -> Mode:
    """`factory.pre_p2_throttle`; `off` when missing or unknown."""
    value = settings.get(MODE_SETTING, "off")
    return value if value in ("off", "log", "enforce") else "off"


def pre_rank(percentile: float | None, tier: str | None, age_h: float, half_life: float) -> float | None:
    """Chat percentile x spike bonus x freshness; None without a percentile. Pure."""
    if percentile is None:
        return None
    return percentile * SPIKE_BONUS.get(tier or "", 1.0) * freshness(age_h, half_life)


def postable_count(remaining_h: Sequence[float], posts_per_h: float) -> int:
    """How many clips of a queue in posting order (`remaining_h`: hours each has left before it expires)
    an account posting `posts_per_h` reaches in time: the clip at position i posts after about
    i / posts_per_h hours. Pure."""
    if posts_per_h <= 0:
        return 0
    return sum(1 for position, left in enumerate(remaining_h) if position / posts_per_h < left)


@dataclass(frozen=True, slots=True)
class QueuedClip:
    """One approved hourly clip as the throttle sees it."""

    rank_score: float
    remaining_h: float
    pre_rank: float | None


@dataclass(frozen=True, slots=True)
class Decision:
    would_skip: bool
    why: str
    effective_queue: int
    queue_median: float | None


def decide(
    candidate: float | None, queue: Sequence[QueuedClip], posts_per_h: Sequence[float], queue_min: int
) -> Decision:
    """The throttle rule. `queue` in any order; `posts_per_h` holds each hourly account's pace. Pure."""
    ordered = sorted(queue, key=lambda clip: clip.rank_score, reverse=True)
    remaining = [clip.remaining_h for clip in ordered]
    effective = max((postable_count(remaining, pace) for pace in posts_per_h), default=0)
    reachable = ordered[:effective]
    ranked = [clip.pre_rank for clip in reachable if clip.pre_rank is not None]
    median = statistics.median(ranked) if len(ranked) >= MIN_RANKED else None
    if candidate is None:
        return Decision(False, "no percentile", effective, median)
    if effective < queue_min:
        return Decision(False, "queue short", effective, median)
    if median is None:
        return Decision(False, "too few ranked clips in the queue", effective, median)
    if candidate < median:
        return Decision(True, "below the queue median", effective, median)
    return Decision(False, "at or above the queue median", effective, median)


# ── database ─────────────────────────────────────────────────

_QUEUE_SQL = """
select c.id, coalesce(c.rank_score, 0) as rank_score, c.approved_at, c.source_type,
       s.tier, s.spike_wall,
       coalesce(
         (select (l.sub_scores->>'percentile')::float from scoring_log l
           where l.clip_id = c.id and l.stage = %(stage)s order by l.id desc limit 1),
         (select (l.sub_scores->>'percentile')::float from scoring_log l
           where l.spike_id = c.spike_id and l.stage = 'spike.confirm' order by l.id desc limit 1)
       ) as percentile
from clips c left join spikes s on s.id = c.spike_id
where c.status = 'approved' and c.lane = 'hourly' and c.approved_at is not null
"""

_PACE_SQL = """
select a.id,
       (select count(*) from posts p
         where p.account_id = a.id and p.lane = 'hourly' and p.status = 'published'
           and p.published_at > now() - interval '24 hours') as posted
from platform_accounts a
where a.enabled and 'hourly' = any(a.lanes)
"""


def _age_h(since: datetime | None, now: datetime) -> float:
    if since is None:
        return 0.0
    return max(0.0, (now - since.astimezone(UTC)).total_seconds() / 3600)


def _reference(row: Mapping[str, Any]) -> datetime | None:
    """The time the posting rank measures freshness from: the spike for a live clip, else the approval."""
    if row.get("source_type") == "live" and row.get("spike_wall") is not None:
        when: datetime = row["spike_wall"]
        return when
    approved: datetime | None = row.get("approved_at")
    return approved


def _params() -> PostingParams:
    from sixstars.posting.store import DbStore  # imported here: posting is not a factory dependency

    return DbStore().load_params()


def _half_life(source_type: str | None, params: PostingParams) -> float:
    return half_life_h("live" if source_type == "live" else "vod", params)


def assess(
    *,
    clip_id: str,
    spike_id: str | None,
    creator_id: str | None,
    source_type: str,
    lane: str,
    now: datetime,
) -> str | None:
    """Run the throttle for one candidate and write its `scoring_log` row. Returns the reason to reject it
    (only in `enforce` mode), else None. Never raises: a failure is logged and the clip goes on."""
    current = mode()
    if current == "off" or lane != "hourly":
        return None
    try:
        return _assess(current, clip_id, spike_id, creator_id, source_type, now)
    except Exception as exc:  # noqa: BLE001 - the throttle must never cost a clip
        get_logger("factory").warning("pre_p2_throttle_failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return None


def _assess(
    current: Mode, clip_id: str, spike_id: str | None, creator_id: str | None, source_type: str, now: datetime
) -> str | None:
    params = _params()
    spike = (
        db.fetch_one("select tier, score_z, spike_wall, mode from spikes where id = %s", (spike_id,))
        if spike_id
        else None
    )
    tier = str(spike["tier"]) if spike is not None and spike["tier"] is not None else None
    if tier == "S":
        return None
    percentile: float | None = None
    if spike is not None and spike["score_z"] is not None and spike["mode"] != "transcript":
        percentile = spike_percentile(creator_id, float(spike["score_z"]), spike_id).percentile
    reference = spike["spike_wall"] if spike is not None and source_type == "live" else None
    candidate = pre_rank(percentile, tier, _age_h(reference, now), _half_life(source_type, params))

    queue = [
        QueuedClip(
            rank_score=float(row["rank_score"]),
            remaining_h=params.max_clip_age_h - _age_h(row["approved_at"], now),
            pre_rank=pre_rank(
                None if row["percentile"] is None else float(row["percentile"]),
                row["tier"],
                _age_h(_reference(row), now),
                _half_life(row["source_type"], params),
            ),
        )
        for row in db.fetch_all(_QUEUE_SQL, {"stage": STAGE})
    ]
    paces = [int(row["posted"]) / HOURS_PER_DAY for row in db.fetch_all(_PACE_SQL)]
    queue_min = settings.get_int(QUEUE_MIN_SETTING, 40)
    decision = decide(candidate, queue, paces, queue_min)
    scoring_log.record(
        STAGE,
        decision="would_skip" if decision.would_skip else "keep",
        reason=decision.why,
        clip_id=clip_id,
        spike_id=spike_id,
        creator_id=creator_id,
        sub_scores={
            "mode": current,
            "percentile": percentile,
            "tier": tier,
            "source_type": source_type,
            "pre_rank": candidate,
            "queue_median": decision.queue_median,
            "effective_queue": decision.effective_queue,
            "queue_total": len(queue),
            "posts_per_h": [round(pace, 3) for pace in paces],
            "queue_min": queue_min,
        },
    )
    if current == "enforce" and decision.would_skip:
        return (
            f"{REASON_PREFIX}: rank {candidate:.1f} below the queue median {decision.queue_median:.1f} "
            f"with {decision.effective_queue} clips that can still post"
        )
    return None

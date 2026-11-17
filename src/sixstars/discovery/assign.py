"""Assigns tiered creators to the live lane or the VOD lane within capacity limits read from settings.
Implemented in M09.

Everything here is a pure function from the current state and the ranked candidates to a plan; `run.py`
applies the plan to the database. Rules: `docs/06-discovery.md` sections 4 and 5.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sixstars.discovery.models import FORBIDDEN_TIERS, Creator, Scored

ACTIVE_STATUSES: tuple[str, ...] = ("assigned", "recording")


@dataclass(frozen=True)
class Session:
    """An active `live_sessions` row (status `assigned` or `recording`)."""

    id: str
    creator_id: str
    platform: str
    source_id: str
    status: str
    below_cutoff_since: datetime | None = None
    identity: str = ""  # normalised creator handle, see `identity()`
    creator_tier: str = "auto"


@dataclass(frozen=True)
class Keep:
    session_id: str
    score: float | None  # None when the stream was not seen in this pass
    below_cutoff_since: datetime | None


@dataclass(frozen=True)
class Release:
    session_id: str
    reason: str  # below_cutoff | creator_forbidden


@dataclass(frozen=True)
class SlotPlan:
    keep: list[Keep] = field(default_factory=list)
    release: list[Release] = field(default_factory=list)
    assign: list[Scored] = field(default_factory=list)
    held: dict[str, int] = field(default_factory=dict)  # streams passed over per limited platform


def identity(creator: Creator) -> str:
    """Key that is equal for the same person on Twitch and on YouTube: the handle without case, spaces or
    punctuation. Used for "one source per creator"."""
    squashed = "".join(char for char in creator.handle.casefold() if char.isalnum())
    return squashed or f"{creator.platform}:{creator.channel_id}"


def rank(candidates: Sequence[Scored]) -> list[Scored]:
    """Best first; ties broken by source id so the order is stable."""
    return sorted(candidates, key=lambda item: (-item.score, item.candidate.source_id))


def plan_slots(
    sessions: Sequence[Session],
    live: Sequence[Scored],
    *,
    cutoff: float,
    release_after_s: float,
    max_slots: int,
    min_viewers: int,
    now: datetime,
    allowed: Callable[[Creator], bool],
    platform_limit: Mapping[str, int] | None = None,
) -> SlotPlan:
    """Decide which sessions stay, which are released and which streams get the free slots.

    A session stays while its stream scores at or above `cutoff`. When it first drops below (or is no
    longer seen) `below_cutoff_since` is set; it is released once that is `release_after_s` old. A session
    of a creator who became forbidden is released at once. Free slots up to `max_slots` go to the best
    streams at or above the cutoff, one per creator, that `allowed` accepts.

    `platform_limit` caps the new slots of a platform in this plan (0 for a platform the live lane cannot
    record from now, 1 for one it should probe); a platform that is not named has no cap. Streams passed
    over for that reason (whatever their creator's permission state) are counted in `SlotPlan.held`
    and leave their slot to the next best stream.
    """
    limits = dict(platform_limit or {})
    by_source = {item.key: item for item in live}
    plan = SlotPlan()
    busy: set[str] = set()
    for session in sessions:
        if session.creator_tier in FORBIDDEN_TIERS:
            plan.release.append(Release(session.id, "creator_forbidden"))
            continue
        seen = by_source.get((session.platform, session.source_id))
        if seen is not None and seen.score >= cutoff:
            plan.keep.append(Keep(session.id, seen.score, None))
            busy.add(session.identity)
            continue
        since = session.below_cutoff_since
        if since is not None and (now - since).total_seconds() >= release_after_s:
            plan.release.append(Release(session.id, "below_cutoff"))
            continue
        plan.keep.append(Keep(session.id, None if seen is None else seen.score, since or now))
        busy.add(session.identity)
    taken = {(session.platform, session.source_id) for session in sessions}
    free = max_slots - len(plan.keep)
    for item in rank(live):
        if free <= 0:
            break
        viewers = item.candidate.viewers
        if item.score < cutoff or item.key in taken or viewers is None or viewers < min_viewers:
            continue
        platform = item.candidate.platform
        if limits.get(platform, 1) <= 0:
            plan.held[platform] = plan.held.get(platform, 0) + 1
            continue
        who = identity(item.creator)
        if who in busy or not allowed(item.creator):
            continue
        if platform in limits:
            limits[platform] -= 1
        plan.assign.append(item)
        busy.add(who)
        free -= 1
    return plan


def widen_for_low_queue(
    cutoff: float, max_per_run: int, approved_hourly: int, low_watermark: int
) -> tuple[float, int, bool]:
    """When fewer approved hourly clips than the watermark are waiting: double the per-run maximum and
    lower the cutoff by 10 for this run. Returns `(cutoff, max_per_run, widened)`."""
    if approved_hourly < low_watermark:
        return cutoff - 10.0, max_per_run * 2, True
    return cutoff, max_per_run, False


def pick_videos(
    videos: Sequence[Scored],
    *,
    cutoff: float,
    max_total: int,
    max_per_creator: int,
    known: Collection[tuple[str, str]],
    allowed: Callable[[Creator], bool],
    platform_limit: Mapping[str, int] | None = None,
    held: dict[str, int] | None = None,
) -> list[Scored]:
    """The videos to queue for the VOD lane: best first, at or above the cutoff, not already in `videos`
    (`known` holds their `(platform, source_id)`), from allowed creators, capped per creator and in total.

    `platform_limit` caps the videos of a platform in this pick (0 while its downloads are blocked, 1 for
    a probe); a platform that is not named has no cap. A video passed over for that reason takes nothing
    from the total, so the next best video of another platform is picked instead; when `held` is given,
    those videos are counted in it per platform."""
    limits = dict(platform_limit or {})
    picked: list[Scored] = []
    per_creator: dict[str, int] = {}
    for item in rank(videos):
        if len(picked) >= max_total:
            break
        if item.score < cutoff or item.key in known:
            continue
        platform = item.candidate.platform
        if limits.get(platform, 1) <= 0:
            if held is not None:
                held[platform] = held.get(platform, 0) + 1
            continue
        if not allowed(item.creator):
            continue
        count = per_creator.get(item.creator.id, 0)
        if count >= max_per_creator:
            continue
        if platform in limits:
            limits[platform] -= 1
        per_creator[item.creator.id] = count + 1
        picked.append(item)
    return picked

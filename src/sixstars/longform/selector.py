"""Pure moment selection for one episode (M20 L2): rank, variety rules, fill to length, countdown order.

No database and no network: the pool loader (`pool.py`) turns rows into `Candidate`s, this module decides.
"""

import math
from dataclasses import dataclass
from datetime import datetime

from sixstars.longform.config import RankWeights


@dataclass(frozen=True)
class Limits:
    """The numbers selection needs (a slice of `LongformConfig`, so tests build it directly)."""

    target_s: float
    min_s: float
    max_s: float
    segment_min_s: float
    segment_max_s: float
    lead_s: float
    tail_s: float
    max_per_creator: int
    max_creator_s: float
    pool_hours: float


@dataclass(frozen=True)
class Candidate:
    """One gated clip that may become a moment."""

    clip_id: str
    creator_id: str
    creator_name: str
    title: str
    score: float
    clip_s: float
    window_s: float | None
    score_z: float | None
    views: int | None
    topic: str | None
    created_at: datetime


@dataclass(frozen=True)
class Pick:
    """A chosen moment. `position` is the countdown number: 1 is the best and plays last."""

    candidate: Candidate
    est_s: float
    rank: float
    position: int


@dataclass(frozen=True)
class Selection:
    """The episode plan. `picks` are in playback order (highest position first, #1 last)."""

    picks: tuple[Pick, ...]
    total_s: float
    skipped_reason: str | None

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


def estimate_seconds(c: Candidate, limits: Limits) -> float:
    """Length of the full moment: the longer of spike window and clip cut, plus lead and tail, clamped.

    Lengths are compared, never positions: live windows and cuts use different origins."""
    core = max(c.clip_s, c.window_s or 0.0)
    return min(max(core + limits.lead_s + limits.tail_s, limits.segment_min_s), limits.segment_max_s)


def rank_value(c: Candidate, now: datetime, best_views: int, limits: Limits, w: RankWeights) -> float:
    """Higher is better: Claude score, spike strength, Short views, freshness (longform.rank_weights)."""
    z = 0.0
    if c.score_z is not None and w.spike_z_cap > 0:
        z = min(max(c.score_z, 0.0), w.spike_z_cap) / w.spike_z_cap
    views = 0.0
    if c.views and best_views > 0:
        views = math.log1p(c.views) / math.log1p(best_views)
    age_h = max((now - c.created_at).total_seconds() / 3600.0, 0.0)
    fresh = max(1.0 - age_h / limits.pool_hours, 0.0) if limits.pool_hours > 0 else 0.0
    return w.score * c.score + w.spike_z * z + w.views * views + w.fresh * fresh


def select(candidates: list[Candidate], limits: Limits, weights: RankWeights, now: datetime) -> Selection:
    """Pick moments in rank order under the variety rules up to the target length, then order them."""
    best_views = max((c.views or 0 for c in candidates), default=0)
    ranked = sorted(
        ((rank_value(c, now, best_views, limits, weights), c) for c in candidates),
        key=lambda pair: (-pair[0], pair[1].clip_id),
    )
    chosen: list[tuple[float, Candidate, float]] = []
    per_creator: dict[str, int] = {}
    creator_s: dict[str, float] = {}
    topics: set[str] = set()
    total = 0.0
    for rank, c in ranked:
        if total >= limits.target_s:
            break
        est = estimate_seconds(c, limits)
        if per_creator.get(c.creator_id, 0) >= limits.max_per_creator:
            continue
        if creator_s.get(c.creator_id, 0.0) + est > limits.max_creator_s:
            continue
        if c.topic and c.topic in topics:
            continue
        if total + est > limits.max_s:
            continue
        chosen.append((rank, c, est))
        per_creator[c.creator_id] = per_creator.get(c.creator_id, 0) + 1
        creator_s[c.creator_id] = creator_s.get(c.creator_id, 0.0) + est
        if c.topic:
            topics.add(c.topic)
        total += est
    if total < limits.min_s:
        return Selection((), total, f"pool too thin: {total:.0f}s of {limits.min_s:.0f}s minimum")
    return Selection(countdown_order(chosen), total, None)


def countdown_order(chosen: list[tuple[float, Candidate, float]]) -> tuple[Pick, ...]:
    """Order the countdown and return it in playback order (#N first, #1 last).

    Built backwards from the end: #1 is the best moment, and each earlier slot takes the best remaining moment
    whose creator differs from the moment right after it, so one creator never plays twice in a row when
    another creator is left. Positions follow playback: the first moment shown is #N."""
    remaining = sorted(chosen, key=lambda t: (-t[0], t[1].clip_id))
    backwards: list[tuple[float, Candidate, float]] = []
    while remaining:
        after = backwards[-1][1].creator_id if backwards else None
        idx = next((k for k, t in enumerate(remaining) if t[1].creator_id != after), 0)
        backwards.append(remaining.pop(idx))
    n = len(backwards)
    return tuple(
        Pick(candidate=c, est_s=est, rank=rank, position=n - idx)
        for idx, (rank, c, est) in enumerate(reversed(backwards))
    )


@dataclass(frozen=True)
class SoloLimits:
    """Creator-day numbers (a slice of `longform.solo`)."""

    min_s: float
    target_s: float
    max_s: float
    min_moments: int


@dataclass(frozen=True)
class CreatorDay:
    """A creator day's plan: the featured creator and the selection of their moments."""

    creator_id: str
    creator_name: str
    selection: Selection


def _fill_one_creator(
    ranked: list[tuple[float, Candidate]], limits: Limits, solo: SoloLimits
) -> tuple[list[tuple[float, Candidate, float]], float]:
    """Fill one creator's ranked moments up to the creator-day target (no repeated topic, never above max)."""
    chosen: list[tuple[float, Candidate, float]] = []
    topics: set[str] = set()
    total = 0.0
    for rank, c in ranked:
        if total >= solo.target_s:
            break
        est = estimate_seconds(c, limits)
        if c.topic and c.topic in topics:
            continue
        if total + est > solo.max_s:
            continue
        chosen.append((rank, c, est))
        if c.topic:
            topics.add(c.topic)
        total += est
    return chosen, total


def select_creator_day(
    candidates: list[Candidate],
    limits: Limits,
    solo: SoloLimits,
    weights: RankWeights,
    now: datetime,
    cooling: frozenset[str],
) -> CreatorDay | None:
    """Pick one creator and their moments, or None when no creator can fill a creator day.

    The creator with the most eligible moments wins (ties: more total rank, then id), skipping creators in
    `cooling` (a creator episode within the cooldown). They need at least `min_moments` moments and
    `min_s` seconds after the fill."""
    best_views = max((c.views or 0 for c in candidates), default=0)
    by_creator: dict[str, list[tuple[float, Candidate]]] = {}
    for c in candidates:
        if c.creator_id in cooling:
            continue
        by_creator.setdefault(c.creator_id, []).append((rank_value(c, now, best_views, limits, weights), c))
    order = sorted(
        by_creator.items(),
        key=lambda kv: (-len(kv[1]), -sum(r for r, _ in kv[1]), kv[0]),
    )
    for creator_id, pairs in order:
        if len(pairs) < solo.min_moments:
            break  # sorted by count: nobody after this one has enough either
        ranked = sorted(pairs, key=lambda p: (-p[0], p[1].clip_id))
        chosen, total = _fill_one_creator(ranked, limits, solo)
        if len(chosen) < solo.min_moments or total < solo.min_s:
            continue
        return CreatorDay(
            creator_id, ranked[0][1].creator_name, Selection(countdown_order(chosen), total, None)
        )
    return None

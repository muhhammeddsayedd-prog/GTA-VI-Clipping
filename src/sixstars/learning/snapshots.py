"""Per-post metric snapshots at fixed ages (docs/28 Part A, L1).

Each published post is read at 1 h, 3 h, 6 h, 24 h, 72 h and 7 days. Each reading is a new row of
`post_metrics_snapshots` (insert only, one per post and target age), so a 3-hour-old clip is only compared
with other clips at 3 hours. A reading is due from the target age until `target + tolerance`; a post the
collector could not reach in that window misses that age rather than storing a late number under it.
The youngest due post goes first.

What each platform gives, read from real Upload-Post answers (19 Nov 2026, production):
- TikTok `post_metrics`: views, likes, comments, shares, reach, favorites (= saves), new_followers,
  profile_views, full_video_watched_rate, average_time_watched / avg_watch_seconds, total_time_watched /
  watch_time_seconds, completion_rate, impression_sources[{impression_source, percentage}],
  audience_types[{type, percentage}].
- Instagram: views (= plays), impressions, reach, likes, comments, saves, shares, avg_watch_seconds,
  watch_time_seconds (no completion and no followers per post).
- YouTube through Upload-Post: views, likes, comments, favorites only. Completion and subscribers per
  video come from the YouTube Analytics API when its scope is authorised (`clients/youtube_analytics.py`).

A field that is missing is stored as null, never 0 (TikTok leaves fields out for hours).

Pure functions only; the collector (`sixstars.stats.collector`) reads and writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

FLAG = "learning.snapshots"
DEFAULT_AGES_H: tuple[float, ...] = (1, 3, 6, 24, 72, 168)


@dataclass(frozen=True, slots=True)
class SnapshotRules:
    ages_h: tuple[float, ...] = DEFAULT_AGES_H
    min_tolerance_h: float = 0.75  # a 1 h reading is accepted until 1.75 h (WF05 runs every 30 min)
    tolerance_frac: float = 0.25  # ... and a 24 h reading until 30 h

    @classmethod
    def from_flag(cls, value: Mapping[str, Any]) -> SnapshotRules:
        base = cls()
        raw_ages = value.get("ages_h")
        ages = base.ages_h
        if isinstance(raw_ages, list) and raw_ages and all(_positive(a) for a in raw_ages):
            ages = tuple(sorted(float(a) for a in raw_ages))
        min_tol = value.get("min_tolerance_h", base.min_tolerance_h)
        frac = value.get("tolerance_frac", base.tolerance_frac)
        return cls(
            ages_h=ages,
            min_tolerance_h=float(min_tol) if _positive(min_tol) else base.min_tolerance_h,
            tolerance_frac=float(frac) if _positive(frac) else base.tolerance_frac,
        )

    def window(self, target_h: float) -> tuple[float, float]:
        return target_h, target_h + max(self.min_tolerance_h, self.tolerance_frac * target_h)


def _positive(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True, slots=True)
class DuePost:
    """A published post that may need a reading. `taken` holds the target ages already stored."""

    id: str
    platform: str
    published_at: datetime
    taken: frozenset[float] = frozenset()


@dataclass(frozen=True, slots=True)
class DueReading:
    post_id: str
    platform: str
    target_age_h: float
    age_h: float


def due_readings(posts: Sequence[DuePost], now: datetime, rules: SnapshotRules) -> list[DueReading]:
    """The readings due now, youngest post first. A post has at most one due target at a time (the
    windows don't overlap for the default ages); if several are due, the latest target wins."""
    due: list[tuple[datetime, DueReading]] = []
    for post in posts:
        age_h = (now - post.published_at).total_seconds() / 3600
        target = None
        for t in rules.ages_h:
            start, end = rules.window(t)
            if start <= age_h <= end and t not in post.taken:
                target = t
        if target is not None:
            due.append((post.published_at, DueReading(post.id, post.platform, target, round(age_h, 3))))
    due.sort(key=lambda item: item[0], reverse=True)
    return [reading for _, reading in due]


def oldest_target_window_h(rules: SnapshotRules) -> float:
    """How far back the collector has to look for posts that may still be due."""
    return max(rules.window(t)[1] for t in rules.ages_h)


# ── parsing ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SnapshotNumbers:
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    reach: int | None = None
    new_followers: int | None = None
    completion_rate: float | None = None  # 0..1, share of views watched to the end
    avg_view_pct: float | None = None  # 0..1, YouTube averageViewPercentage / 100
    avg_watch_s: float | None = None
    total_watch_s: float | None = None
    impression_sources: dict[str, float] | None = None
    audience_types: dict[str, float] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return all(value is None for key, value in asdict(self).items() if key != "extra")


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value == value and value != float("inf") else None
    if isinstance(value, str):
        try:
            return _count(float(value.strip().replace(",", "")))
        except ValueError:
            return None
    return None


def _real(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number >= 0 and number == number and number != float("inf") else None


def _first(source: Mapping[str, Any], keys: Sequence[str], parse: Any) -> Any:
    for key in keys:
        if key in source:
            value = parse(source[key])
            if value is not None:
                return value
    return None


def _fraction(value: float | None) -> float | None:
    """A rate that may come as 0..1 or as a percentage (0..100)."""
    if value is None:
        return None
    return value / 100 if value > 1 else value


def _shares_of(items: Any, name_key: str) -> dict[str, float] | None:
    if not isinstance(items, list):
        return None
    out: dict[str, float] = {}
    for item in items:
        if isinstance(item, Mapping) and isinstance(item.get(name_key), str):
            share = _real(item.get("percentage"))
            if share is not None:
                out[str(item[name_key])] = share
    return out or None


def metrics_of(body: Any, platform: str) -> Mapping[str, Any] | None:
    """`platforms.<platform>.post_metrics` of a post-analytics answer, or `post_metrics` at the top."""
    if not isinstance(body, Mapping):
        return None
    platforms = body.get("platforms")
    entry = platforms.get(platform) if isinstance(platforms, Mapping) else None
    metrics = (entry if isinstance(entry, Mapping) else body).get("post_metrics")
    return metrics if isinstance(metrics, Mapping) else None


def parse_snapshot(body: Any, platform: str) -> SnapshotNumbers:
    """Snapshot numbers from an Upload-Post post-analytics answer. Missing fields stay None."""
    m = metrics_of(body, platform)
    if m is None:
        return SnapshotNumbers()
    completion = _first(m, ("full_video_watched_rate", "completion_rate"), _real)
    avg_pct = _first(m, ("average_view_percentage", "averageViewPercentage"), _real)
    total_watch = _first(m, ("watch_time_seconds", "total_time_watched"), _real)
    if total_watch is None and (minutes := _real(m.get("watch_time_minutes"))) is not None:
        total_watch = minutes * 60
    return SnapshotNumbers(
        views=_first(m, ("views", "view_count", "plays", "play_count", "video_views", "impressions"), _count),
        likes=_first(m, ("likes", "like_count", "digg_count"), _count),
        comments=_first(m, ("comments", "comment_count"), _count),
        shares=_first(m, ("shares", "share_count"), _count),
        saves=_first(m, ("saves", "favorites", "collect_count"), _count),
        reach=_first(m, ("reach",), _count),
        new_followers=_first(m, ("new_followers", "subscribers_gained", "subscribersGained"), _count),
        completion_rate=_fraction(completion) if platform == "tiktok" else None,
        avg_view_pct=_fraction(avg_pct),
        avg_watch_s=_first(
            m, ("avg_watch_seconds", "average_time_watched", "average_view_duration_seconds"), _real
        ),
        total_watch_s=total_watch,
        impression_sources=_shares_of(m.get("impression_sources"), "impression_source"),
        audience_types=_shares_of(m.get("audience_types"), "type"),
    )


def from_youtube_statistics(item: Mapping[str, Any]) -> SnapshotNumbers:
    """A `videos.list` item with part `statistics` (the API sends strings)."""
    stats = item.get("statistics")
    if not isinstance(stats, Mapping):
        return SnapshotNumbers()
    return SnapshotNumbers(
        views=_count(stats.get("viewCount")),
        likes=_count(stats.get("likeCount")),
        comments=_count(stats.get("commentCount")),
        saves=_count(stats.get("favoriteCount")),
    )


def merge(base: SnapshotNumbers, extra: SnapshotNumbers) -> SnapshotNumbers:
    """`base` with the fields it lacks filled in from `extra` (e.g. Data API counts + Analytics API)."""
    a, b = asdict(base), asdict(extra)
    merged = {key: (a[key] if a[key] is not None else b[key]) for key in a if key != "extra"}
    return SnapshotNumbers(**merged, extra={**b["extra"], **a["extra"]})

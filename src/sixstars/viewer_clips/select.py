"""Pure rules of the Twitch Clips source: which clips fit, how they are ranked, which ones are picked.
No database, no network.

Rank = views per hour since the clip was made (over at least `rate_floor_h` hours), multiplied by
`discovery.non_english_penalty` when the broadcast is not in English: the same factor the trend score of
streams and videos uses. The parts are kept (`Ranked.parts`) and stored with the row.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sixstars.clients.twitch import TwitchClip
from sixstars.discovery import scoring
from sixstars.viewer_clips.config import ClipsConfig

# Skip reasons, as they appear in the discovery summary.
TOO_SHORT: Final = "too_short"
LOW_VIEWS: Final = "low_views"
TOO_FRESH: Final = "too_fresh"
TITLE_BLOCKLIST: Final = "title_blocklist"
NO_URL: Final = "no_url"
ALREADY_SEEN: Final = "already_seen"
SAME_MOMENT: Final = "same_moment"
FORBIDDEN_CREATOR: Final = "forbidden_creator"
CREATOR_UNCHECKED: Final = "creator_unchecked"
PER_CREATOR_LIMIT: Final = "per_creator_limit"
NO_ROOM: Final = "no_room"

Range = tuple[float, float]


@dataclass(frozen=True)
class Ranked:
    """A clip that fits, with its rank and the numbers the rank was built from."""

    clip: TwitchClip
    rank: float
    parts: dict[str, Any] = field(default_factory=dict)


def language_of(clip: TwitchClip) -> str | None:
    """The broadcast language as discovery writes it (`en`, `pt`, ...); Twitch's "other" and an empty
    value are None for the creator row but count as not English for the rank (`is_english_clip`)."""
    value = clip.language.strip().casefold().split("-")[0]
    return value if value and value != "other" else None


def is_english_clip(clip: TwitchClip) -> bool:
    """True only when Twitch reports English. Unlike YouTube, Twitch always reports the broadcast
    language, so an unknown value ("other") is not taken as English."""
    return clip.language.strip().casefold().split("-")[0] == "en"


def age_hours(clip: TwitchClip, now: datetime) -> float:
    return max(0.0, (now - clip.created_at).total_seconds() / 3600.0)


def rank_clip(clip: TwitchClip, now: datetime, config: ClipsConfig, non_english_penalty: float) -> Ranked:
    """Views per hour with the language factor. Pure."""
    age_h = age_hours(clip, now)
    views_per_hour = clip.view_count / max(age_h, config.rate_floor_h, 1e-6)
    english = is_english_clip(clip)
    factor = 1.0 if english else non_english_penalty
    rank = round(views_per_hour * factor, 3)
    parts = {
        "views": clip.view_count,
        "age_h": round(age_h, 2),
        "views_per_hour": round(views_per_hour, 3),
        "language": clip.language or None,
        "language_factor": factor,
        "rank": rank,
    }
    return Ranked(clip, rank, parts)


def unfit_reason(
    clip: TwitchClip, now: datetime, config: ClipsConfig, blocklist: Sequence[str]
) -> str | None:
    """Why a clip is not a candidate at all, or None when it fits. A clip longer than the gate's maximum
    fits: the factory trims it. One shorter than the minimum cannot be made longer."""
    if not clip.url:
        return NO_URL
    if scoring.title_blocked(clip.title, blocklist):
        return TITLE_BLOCKLIST
    if clip.duration_s < config.min_len_s:
        return TOO_SHORT
    if clip.view_count < config.min_views:
        return LOW_VIEWS
    if age_hours(clip, now) * 60.0 < config.min_age_min:
        return TOO_FRESH
    return None


def rank_all(
    clips: Sequence[TwitchClip],
    now: datetime,
    config: ClipsConfig,
    blocklist: Sequence[str],
    non_english_penalty: float,
) -> tuple[list[Ranked], dict[str, int]]:
    """The clips that fit, best first, and how many were dropped per reason. A clip id listed twice
    counts once."""
    skipped: dict[str, int] = {}
    ranked: list[Ranked] = []
    seen: set[str] = set()
    for clip in clips:
        if clip.id in seen:
            continue
        seen.add(clip.id)
        reason = unfit_reason(clip, now, config, blocklist)
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            ranked.append(rank_clip(clip, now, config, non_english_penalty))
    ranked.sort(key=lambda item: (-item.rank, -item.clip.view_count, item.clip.id))
    return ranked, skipped


def clip_range(clip: TwitchClip) -> Range | None:
    """Where the clip lies in the broadcast's video (seconds), when Twitch says so."""
    if clip.video_id is None or clip.vod_offset_s is None:
        return None
    return (clip.vod_offset_s, clip.vod_offset_s + clip.duration_s)


def overlaps(a: Range, b: Range, pad_s: float) -> bool:
    """True when two ranges of one video lie within `pad_s` of each other (the gate's source-range
    rule, `gate.checks.check_source_range`)."""
    return a[1] + pad_s >= b[0] and a[0] - pad_s <= b[1]


def room(config: ClipsConfig, queued_last_24h: int, waiting: int) -> int:
    """How many clips this pass may queue: the per-pass limit, what is left of the daily cap, and what is
    left of the backlog limit."""
    return max(
        0,
        min(
            config.max_queued_per_pass,
            config.daily_cap - queued_last_24h,
            config.max_backlog - waiting,
        ),
    )


def shortlist(ranked: Sequence[Ranked], known: Collection[str], limit: int) -> list[Ranked]:
    """The best `limit` clips we have not stored before: the ones worth looking up creators for."""
    fresh = [item for item in ranked if item.clip.id not in known]
    return fresh[: max(0, limit)]


@dataclass
class Selection:
    """What `pick` decided: the clips to queue and, for every clip looked at, why it was passed over."""

    picks: list[Ranked] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)  # clip id -> skip reason

    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for reason in self.reasons.values():
            totals[reason] = totals.get(reason, 0) + 1
        return totals


def pick(
    candidates: Sequence[Ranked],
    *,
    limit: int,
    max_per_creator: int,
    pad_s: float,
    verdict: Callable[[TwitchClip], str | None],
    taken: Mapping[str, Sequence[Range]],
) -> Selection:
    """Walk the candidates best first and pick up to `limit`.

    `verdict(clip)` is the creator rule: None when the broadcaster may be clipped, else the skip reason
    (`forbidden_creator`, `creator_unchecked`). `taken` holds, per broadcast video id, the ranges we
    already have a clip of (ours from the VOD lane, or an earlier viewer clip); a clip within `pad_s` of
    one, or of a clip picked just now, is the same moment and is skipped.
    """
    selection = Selection()
    per_creator: dict[str, int] = {}
    ranges: dict[str, list[Range]] = {video: list(spans) for video, spans in taken.items()}
    for item in candidates:
        clip = item.clip
        if len(selection.picks) >= limit:
            selection.reasons[clip.id] = NO_ROOM
            continue
        refused = verdict(clip)
        if refused is not None:
            selection.reasons[clip.id] = refused
            continue
        if per_creator.get(clip.broadcaster_id, 0) >= max_per_creator:
            selection.reasons[clip.id] = PER_CREATOR_LIMIT
            continue
        span = clip_range(clip)
        if span is not None and clip.video_id is not None:
            if any(overlaps(span, other, pad_s) for other in ranges.get(clip.video_id, ())):
                selection.reasons[clip.id] = SAME_MOMENT
                continue
            ranges.setdefault(clip.video_id, []).append(span)
        per_creator[clip.broadcaster_id] = per_creator.get(clip.broadcaster_id, 0) + 1
        selection.picks.append(item)
    return selection


def top_rows(
    ranked: Sequence[Ranked],
    picked: Collection[str],
    reasons: Mapping[str, str],
    known: Collection[str],
    limit: int,
) -> list[dict[str, Any]]:
    """The best clips as plain dicts for the discovery summary."""
    rows: list[dict[str, Any]] = []
    for item in ranked[:limit]:
        clip = item.clip
        skipped = ALREADY_SEEN if clip.id in known else reasons.get(clip.id)
        rows.append(
            {
                "rank": item.rank,
                "title": clip.title,
                "channel": clip.broadcaster_name,
                "views": clip.view_count,
                "views_per_hour": item.parts.get("views_per_hour"),
                "age_h": item.parts.get("age_h"),
                "duration_s": clip.duration_s,
                "language": clip.language or None,
                "picked": clip.id in picked,
                "skipped": skipped,
                "url": clip.url,
                "parts": item.parts,
            }
        )
    return rows

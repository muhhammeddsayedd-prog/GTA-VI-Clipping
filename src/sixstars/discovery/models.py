"""Data shapes shared by the discovery modules: a normalised `Candidate` (one live stream or finished video
seen by a source), the `Creator` row it belongs to, and the scored form that is stored and ranked.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

Platform = Literal["twitch", "youtube"]
SourceType = Literal["live", "vod"]
Tier = Literal["preferred", "auto", "restricted", "blocked"]

# Tiers that are never clipped (decision D10): no candidate row, no live slot, no job.
FORBIDDEN_TIERS: Final[frozenset[str]] = frozenset({"restricted", "blocked"})


@dataclass(frozen=True)
class Candidate:
    """One live stream or finished video as a source reported it, before scoring."""

    platform: Platform
    source_type: SourceType
    source_id: str  # Twitch stream id / VOD id, YouTube video id
    url: str
    title: str
    channel_id: str  # Twitch user id / YouTube channel id
    channel_handle: str  # Twitch login / YouTube channel name
    channel_name: str
    origin: str  # which fetcher found it: twitch, ytdlp, ytapi, rss, apify, recheck
    language: str | None = None  # ISO 639-1 when known
    viewers: int | None = None  # live audience now
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    published_at: datetime | None = None
    started_at: datetime | None = None
    duration_s: int | None = None
    has_chat: bool | None = None  # a chat replay exists (Twitch archive, YouTube past live stream)
    followers: int | None = None  # only when the source reports it (Apify)
    language_signal: str | None = None  # why `language` was set to "und" (script:title, term:hindi, ...)
    thumbnail_url: str | None = None  # public picture of the source (console media), size filled in

    @property
    def key(self) -> tuple[str, str]:
        return (self.platform, self.source_id)

    @property
    def channel_key(self) -> tuple[str, str]:
        return (self.platform, self.channel_id)


@dataclass(frozen=True)
class Creator:
    """The columns of a `creators` row that discovery reads."""

    id: str
    platform: Platform
    channel_id: str
    handle: str
    display_name: str | None
    tier: Tier
    language: str | None = None
    follower_count: int | None = None
    audience_spread: float | None = None
    avg_clip_views: float | None = None
    claims_count: int = 0
    description_checked_at: datetime | None = None
    is_new: bool = False

    @property
    def forbidden(self) -> bool:
        return self.tier in FORBIDDEN_TIERS

    @property
    def key(self) -> tuple[str, str]:
        return (self.platform, self.channel_id)


@dataclass(frozen=True)
class Scored:
    """A candidate with its creator, trend score (0 to 100) and the parts the score was built from."""

    candidate: Candidate
    creator: Creator
    score: float
    parts: dict[str, Any] = field(default_factory=dict)
    views_per_hour: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.candidate.key

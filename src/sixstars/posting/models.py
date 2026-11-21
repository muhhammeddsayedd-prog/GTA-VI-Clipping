"""Frozen input and output types of the pure posting rules. Nothing here touches the database, the network
or the clock: the caller loads rows, turns them into these dataclasses and passes the current UTC time in.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class Lane(StrEnum):
    SPIKE = "spike"
    HOURLY = "hourly"


class Platform(StrEnum):
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"


class Action(StrEnum):
    """What the caller must do with a decision.

    `ALLOW` is the only action that may lead to an Upload-Post call. `DRY_RUN` means the post passed every
    rule but the system is in dry mode: write the `posts` row (`dry_run = true, status = skipped`) and send
    nothing. `SKIP` means do nothing now. `DEFER` means try again at `defer_until`.
    """

    ALLOW = "allow"
    DRY_RUN = "dry_run"
    SKIP = "skip"
    DEFER = "defer"


class Reason(StrEnum):
    """Machine-readable reason codes. Every decision carries exactly one."""

    OK = "ok"
    DRY_RUN_SETTING = "dry_run_setting"
    BEFORE_LAUNCH = "before_launch"
    EMERGENCY_STOP = "emergency_stop"
    ACCOUNT_DISABLED = "account_disabled"
    LANE_NOT_ALLOWED = "lane_not_allowed"
    ACCOUNT_PAUSED = "account_paused"
    PRIVACY_INVALID = "privacy_invalid"
    STAGING_PRIVACY_NOT_PRIVATE = "staging_privacy_not_private"
    STAGING_INSTAGRAM_NOT_ALLOWED = "staging_instagram_not_allowed"
    DUPLICATE_CHANNEL = "duplicate_channel"
    CLIP_NOT_APPROVED = "clip_not_approved"
    CLIP_WRONG_LANE = "clip_wrong_lane"
    ALREADY_POSTED = "already_posted"
    POSTED_ON_SAME_PLATFORM = "posted_on_same_platform"
    CROSS_PLATFORM_TOO_SOON = "cross_platform_too_soon"
    SPIKE_TIME_MISSING = "spike_time_missing"
    SPIKE_TOO_OLD = "spike_too_old"
    AT_CAP = "at_cap"
    SPIKE_HOURLY_LIMIT = "spike_hourly_limit"
    MIN_GAP_WAIT = "min_gap_wait"
    SPIKE_WAIT_EXCEEDED = "spike_wait_exceeded"
    SPIKE_HOLD = "spike_hold"
    SLOT_NOT_DUE = "slot_not_due"
    SLOT_EXPIRED = "slot_expired"
    OUTSIDE_HOURLY_HOURS = "outside_hourly_hours"
    SLOT_ALREADY_FILLED = "slot_already_filled"
    SPIKE_PENDING = "spike_pending"
    SPIKE_RECENT = "spike_recent"
    RESERVED_FOR_SPIKE = "reserved_for_spike"
    MIN_GAP = "min_gap"
    QUEUE_EMPTY = "queue_empty"
    NO_ELIGIBLE_CLIP = "no_eligible_clip"


POST_STATUSES = frozenset({"pending", "published", "failed", "skipped", "removed", "remove_failed"})
CLIP_APPROVED = "approved"
CLIP_POSTED = "posted"


def ensure_utc(value: datetime, name: str = "time") -> datetime:
    """Returns `value` converted to UTC. A naive datetime is refused: guessing its zone could move a post."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Account:
    """One row of `platform_accounts`."""

    id: str
    platform: Platform
    upload_post_user: str
    handle: str
    lanes: frozenset[Lane]
    cap_24h: int
    spike_reserve: int
    min_gap_s: int
    privacy: str | None = None
    hourly_hours_utc: frozenset[int] | None = None  # None = all 24 hours; empty = no hour at all
    enabled: bool = True
    paused_until: datetime | None = None
    # Seconds between two hourly posts of this account, counted from its last hourly post only (spike posts
    # do not count). None = no extra hourly gap. Migration 0034.
    hourly_min_gap_s: int | None = None

    def __post_init__(self) -> None:
        if self.cap_24h < 0 or self.spike_reserve < 0 or self.min_gap_s < 0:
            raise ValueError(f"account {self.id}: cap_24h, spike_reserve and min_gap_s must not be negative")
        if self.hourly_min_gap_s is not None and self.hourly_min_gap_s < 0:
            raise ValueError(f"account {self.id}: hourly_min_gap_s must not be negative")
        if self.hourly_hours_utc is not None and any(not 0 <= h <= 23 for h in self.hourly_hours_utc):
            raise ValueError(f"account {self.id}: hourly_hours_utc must be within 0..23")
        if self.paused_until is not None:
            ensure_utc(self.paused_until, "paused_until")

    @property
    def channel(self) -> tuple[Platform, str]:
        """The real social account behind this row: two rows with the same channel post to the same place."""
        return (self.platform, self.upload_post_user)


@dataclass(frozen=True, slots=True)
class PostRecord:
    """One row of `posts`, joined with the creator of its clip (`clips.creator_id`)."""

    account_id: str
    platform: Platform
    clip_id: str
    lane: Lane
    status: str
    created_at: datetime
    published_at: datetime | None = None
    dry_run: bool = False
    creator_id: str | None = None
    slot_at: datetime | None = None
    # M18 Phase C: the short clip of the posted clip's family (itself, or the clip it is the story cut of).
    family_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in POST_STATUSES:
            raise ValueError(f"unknown post status {self.status!r}")
        ensure_utc(self.created_at, "created_at")
        if self.published_at is not None:
            ensure_utc(self.published_at, "published_at")
        if self.slot_at is not None:
            ensure_utc(self.slot_at, "slot_at")

    @property
    def at(self) -> datetime:
        """When the post counts from: the publish time, or the creation time while it is still unpublished."""
        return self.published_at if self.published_at is not None else self.created_at


@dataclass(frozen=True, slots=True)
class Clip:
    """The columns of `clips` (plus the creator's handle) that the rules and the field builder read."""

    id: str
    creator_id: str
    lane: Lane
    rank_score: float
    status: str = CLIP_APPROVED
    approved_at: datetime | None = None
    spike_at: datetime | None = None  # `spikes.spike_wall` for live spike clips
    creator_handle: str = ""
    source_platform: str = "youtube"  # 'twitch' | 'youtube'
    title: str | None = None
    caption: str | None = None
    hook: str | None = None
    hashtags: tuple[str, ...] = ()
    # M18 Phase C: the clip's family (the short clip's id; None before migration 0029) and its length.
    family_id: str | None = None
    duration_s: float | None = None
    # The gate profile that approved the clip (`clips.gate_profile`): "hourly" for a clip cut for the hourly
    # lane, "express" for a spike clip (also after it was demoted to the hourly lane).
    gate_profile: str | None = None

    @property
    def is_story(self) -> bool:
        """M18 Phase C: a story cut belongs to the family of another (short) clip."""
        return self.family_id is not None and self.family_id != self.id

    def __post_init__(self) -> None:
        if self.approved_at is not None:
            ensure_utc(self.approved_at, "approved_at")
        if self.spike_at is not None:
            ensure_utc(self.spike_at, "spike_at")


@dataclass(frozen=True, slots=True)
class Decision:
    """The answer of a rule function for one account (and one clip, when a clip was involved)."""

    action: Action
    code: Reason
    reason: str
    account_id: str
    lane: Lane
    dry_run: bool = False
    clip_id: str | None = None
    defer_until: datetime | None = None
    slot_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.action is Action.ALLOW and self.dry_run:
            raise ValueError("an ALLOW decision cannot be a dry run")
        if self.action is Action.DRY_RUN and not self.dry_run:
            raise ValueError("a DRY_RUN decision must have dry_run = True")
        if (self.action is Action.DEFER) != (self.defer_until is not None):
            raise ValueError("defer_until is set on DEFER decisions and only there")

    @property
    def may_send(self) -> bool:
        """True only when a real Upload-Post call is permitted."""
        return self.action is Action.ALLOW

    @property
    def would_post(self) -> bool:
        """True when every rule passed: a real post (`ALLOW`) or a recorded dry run (`DRY_RUN`)."""
        return self.action in (Action.ALLOW, Action.DRY_RUN)

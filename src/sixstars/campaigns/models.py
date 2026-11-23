"""Data shapes of the clipping-campaign registry (D118): a `Campaign` row with its streamers and quotas, and
the facts `rules.validate` checks a campaign against. Pure: no database, no network.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

Status = Literal["draft", "active", "paused", "ended"]
STATUSES: Final = ("draft", "active", "paused", "ended")
PLATFORMS: Final = ("youtube", "instagram", "tiktok")
LANE_PREFIX: Final = "campaign:"

# The `clip_campaigns` columns a change may set (besides the slug), in table order.
COLUMN_FIELDS: Final = (
    "name",
    "status",
    "platforms",
    "rate_per_1k",
    "min_payout",
    "max_payout",
    "currency",
    "budget_total",
    "budget_left",
    "budget_checked_at",
    "starts_at",
    "ends_at",
    "render_profile",
    "caption_rules",
    "brief_rules",
    "quota",
    "priority",
    "required_game",
    "submit_url",
    "notes",
    "end_reason",
)


@dataclass(frozen=True)
class PlatformQuota:
    """Posts a day one campaign wants on one platform: at least `min`, at most `max`."""

    min: int
    max: int


@dataclass(frozen=True)
class Campaign:
    """One `clip_campaigns` row plus its streamers (`clip_campaign_creators`)."""

    slug: str
    name: str
    rate_per_1k: float
    status: Status = "draft"
    platforms: tuple[str, ...] = ()
    creator_ids: tuple[str, ...] = ()
    min_payout: float = 0.0
    max_payout: float | None = None
    currency: str = "USD"
    budget_total: float | None = None
    budget_left: float | None = None
    budget_checked_at: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    render_profile: str = "brand-disclose"
    caption_rules: Mapping[str, Any] = field(default_factory=dict)
    brief_rules: Mapping[str, Any] = field(default_factory=dict)
    quota: Mapping[str, PlatformQuota] = field(default_factory=dict)
    priority: int = 0
    required_game: str | None = "Grand Theft Auto VI"
    submit_url: str | None = None
    notes: str | None = None
    version: int = 1
    id: str | None = None
    end_reason: str | None = None

    @property
    def lane_key(self) -> str:
        return f"{LANE_PREFIX}{self.slug}"

    @property
    def is_open(self) -> bool:
        """Draft, active or paused: the campaign still holds quota rows."""
        return self.status != "ended"


@dataclass(frozen=True)
class CreatorInfo:
    """What validation needs to know about a streamer."""

    id: str
    platform: str
    handle: str
    tier: str


@dataclass(frozen=True)
class ProfileInfo:
    """A `render_profiles` row, as far as validation cares."""

    id: str
    logo_pos: str | None
    logo_path: str | None
    keep_overlay: bool = False


@dataclass(frozen=True)
class AccountInfo:
    """An enabled posting account."""

    id: str
    platform: str
    cap_24h: int


@dataclass(frozen=True)
class Context:
    """Everything outside the campaign that a save is checked against."""

    now: datetime
    creators: Mapping[str, CreatorInfo]
    profiles: Mapping[str, ProfileInfo]
    accounts: tuple[AccountInfo, ...]
    campaign_share: Mapping[str, int]  # account id -> posts a day all campaigns together may take
    max_per_day: Mapping[str, int]  # platform -> most posts one campaign may get a day
    others: tuple[Campaign, ...] = ()  # every other active or paused campaign

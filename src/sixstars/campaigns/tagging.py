"""Which campaign a new clip belongs to (D117, PR 2). A clip is tagged when its row is created: the
creator's active campaign with the highest priority (one clip goes to one campaign only), and for a spike
clip only inside campaign hours: a night spike nobody can submit within 30 minutes is posted as Normal
(`brand`). Hourly-lane clips are always tagged; posting holds them until campaign hours (PR 4).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sixstars import db, settings

HOURS_KEY: Final = "lanes.campaign_hours_utc"
# 10:00-24:00 Cairo (UTC+2) = 08-22 UTC; YouTube 10:00-20:00 Cairo = 08-18 UTC.
DEFAULT_HOURS: Final[dict[str, list[int]]] = {
    "default": list(range(8, 22)),
    "youtube": list(range(8, 18)),
}
BRAND_PROFILE: Final = "brand"


@dataclass(frozen=True)
class Tag:
    """The campaign a clip is made for and the render profile it gets."""

    campaign_id: str
    slug: str
    render_profile: str


def campaign_hours(platform: str | None = None) -> frozenset[int]:
    """UTC hours in which campaign posts may go out (the default, or a platform's own window)."""
    raw: Mapping[str, Any] = settings.get_dict(HOURS_KEY, DEFAULT_HOURS)
    hours = raw.get(platform or "default", raw.get("default", DEFAULT_HOURS["default"]))
    return frozenset(int(hour) for hour in hours)


def pick(candidates: Sequence[Mapping[str, Any]], lane: str, hour: int, hours: frozenset[int]) -> Tag | None:
    """Pure: the winning campaign among the creator's active ones (already ordered by priority), or None for
    a spike clip outside campaign hours."""
    if not candidates:
        return None
    if lane == "spike" and hour not in hours:
        return None
    best = candidates[0]
    return Tag(str(best["id"]), str(best["slug"]), str(best["render_profile"]))


def active_for_creator(creator_id: str, now: datetime) -> list[dict[str, Any]]:
    """The creator's active campaigns that have not ended, best priority first."""
    return db.fetch_all(
        """
        select k.id::text as id, k.slug, k.render_profile
        from clip_campaigns k join clip_campaign_creators l on l.campaign_id = k.id
        where l.creator_id = %s and k.status = 'active' and (k.ends_at is null or k.ends_at > %s)
          and (k.starts_at is null or k.starts_at <= %s)
          and (k.budget_left is null or k.budget_left > 0)
        order by k.priority desc, k.slug
        """,
        (creator_id, now, now),
    )


def tag_for(creator_id: str, lane: str, now: datetime) -> Tag | None:
    """The tag a new clip of `creator_id` in `lane` gets now, or None (Normal lane)."""
    return pick(active_for_creator(creator_id, now), lane, now.hour, campaign_hours())

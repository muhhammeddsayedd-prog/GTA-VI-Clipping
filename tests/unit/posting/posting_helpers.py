"""Builders for the posting rule tests: documented default settings, the seeded accounts, posts and clips."""

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sixstars.posting.models import Account, Clip, Lane, Platform, PostRecord
from sixstars.posting.params import PostingParams

# A Friday noon after the launch (2026-11-18 11:00 UTC).
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
LAUNCH_AT = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)

# Defaults from docs/16-settings-reference.md, plus `posting.platform_max_24h` (migration 0005).
DEFAULT_SETTINGS: dict[str, object] = {
    "control.emergency_stop": False,
    "launch.dry_run": True,
    "launch.launch_at": "2026-11-18T11:00:00Z",
    "posting.jitter_max_s": 300,
    "posting.min_gap_s": 900,
    "posting.spike_max_per_hour": 2,
    "posting.skip_hourly_after_spike_s": 1800,
    "posting.spike_max_wait_s": 900,
    "posting.spike_max_age_s": 1800,
    "posting.freshness_half_life_h": {"live": 6, "vod": 24},
    "posting.max_clip_age_h": 72,
    "posting.first_comment": "Follow @6starsclipping for the best GTA 6 moments every hour 🎮⭐",
    "posting.base_hashtags": ["#GTA6", "#GTAVI", "#GTA6Clips", "#Gaming"],
    "posting.youtube_extra_hashtags": ["#Shorts"],
    "posting.platform_max_24h": {"tiktok": 15, "instagram": 50, "youtube": 30},
}

TIKTOK_HOURS = frozenset({0, 1, 2, 11, 12, 14, 15, 17, 18, 19, 20, 21, 22, 23})
BOTH = frozenset({Lane.HOURLY, Lane.SPIKE})


def make_params(app_env: str = "production", **overrides: object) -> PostingParams:
    """Live production parameters with the documented defaults; `overrides` are `PostingParams` fields."""
    base = PostingParams.from_settings({**DEFAULT_SETTINGS, "launch.dry_run": False}, app_env)
    return replace(base, **overrides)  # type: ignore[arg-type]


def production_accounts() -> dict[str, Account]:
    """`supabase/seed/production.sql`."""
    rows = [
        Account(
            "tiktok_main",
            Platform.TIKTOK,
            "default",
            "@6starsclipping",
            frozenset({Lane.HOURLY}),
            14,
            0,
            900,
            "PUBLIC_TO_EVERYONE",
            TIKTOK_HOURS,
            True,
        ),
        Account(
            "tiktok_live",
            Platform.TIKTOK,
            "default",
            "@6starsclipping",
            frozenset({Lane.SPIKE}),
            14,
            14,
            900,
            "PUBLIC_TO_EVERYONE",
            None,
            False,
        ),
        Account("instagram_main", Platform.INSTAGRAM, "default", "@6starsclippy", BOTH, 45, 10, 900),
        Account("youtube_main", Platform.YOUTUBE, "default", "@6starsclips-m3x", BOTH, 28, 4, 900, "public"),
    ]
    return {row.id: row for row in rows}


def staging_accounts() -> dict[str, Account]:
    """`supabase/seed/staging.sql`."""
    prod = production_accounts()
    return {
        "tiktok_main": replace(
            prod["tiktok_main"], lanes=BOTH, spike_reserve=4, privacy="SELF_ONLY", hourly_hours_utc=None
        ),
        "tiktok_live": replace(prod["tiktok_live"], privacy="SELF_ONLY"),
        "instagram_main": prod["instagram_main"],
        "youtube_main": replace(prod["youtube_main"], privacy="private"),
    }


def acct(account_id: str = "instagram_main", **changes: object) -> Account:
    """A production account with some fields changed."""
    return replace(production_accounts()[account_id], **changes)  # type: ignore[arg-type]


def small_account(cap: int = 5, reserve: int = 2, gap: int = 900, **changes: object) -> Account:
    """An Instagram account in both lanes with small numbers, for boundary tables."""
    return acct("instagram_main", cap_24h=cap, spike_reserve=reserve, min_gap_s=gap, **changes)


_ids = itertools.count(1)


def post(
    account_id: str = "instagram_main",
    at: datetime = T0,
    lane: Lane = Lane.HOURLY,
    *,
    clip_id: str | None = None,
    status: str = "published",
    dry_run: bool = False,
    creator_id: str | None = None,
    slot_at: datetime | None = None,
) -> PostRecord:
    """A `posts` row. `at` is the publish time, or the creation time for rows that were never published."""
    published = status in ("published", "removed", "remove_failed")
    return PostRecord(
        account_id=account_id,
        platform=Platform(account_id.split("_")[0]),
        clip_id=clip_id or f"old-{next(_ids)}",
        lane=lane,
        status=status,
        created_at=at,
        published_at=at if published else None,
        dry_run=dry_run,
        creator_id=creator_id,
        slot_at=slot_at,
    )


def spread(account_id: str, count: int, newest: datetime, lane: Lane = Lane.HOURLY) -> list[PostRecord]:
    """`count` posts, 20 minutes apart, the newest at `newest`."""
    return [post(account_id, newest - timedelta(minutes=20 * i), lane) for i in range(count)]


def hourly_clip(
    clip_id: str = "h1",
    rank: float = 5.0,
    creator: str = "creator-a",
    *,
    at: datetime = T0,
    **changes: object,
) -> Clip:
    """An approved hourly clip, approved one hour before `at`."""
    base = Clip(
        id=clip_id,
        creator_id=creator,
        lane=Lane.HOURLY,
        rank_score=rank,
        approved_at=at - timedelta(hours=1),
        creator_handle="SomeStreamer",
        source_platform="youtube",
        title="He jumped the WHOLE bridge",
        caption="Nobody expected this jump 😱",
        hashtags=("#stunt", "#viceCity"),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def spike_clip(
    clip_id: str = "s1",
    rank: float = 9.0,
    creator: str = "creator-s",
    *,
    at: datetime = T0,
    **changes: object,
) -> Clip:
    """An approved spike clip: the spike happened 4 minutes before `at`, the clip was approved at `at`."""
    base = Clip(
        id=clip_id,
        creator_id=creator,
        lane=Lane.SPIKE,
        rank_score=rank,
        approved_at=at,
        spike_at=at - timedelta(minutes=4),
        creator_handle="LiveStreamer",
        source_platform="twitch",
        title="Chat went insane",
        caption="Chat exploded when this happened",
    )
    return replace(base, **changes)  # type: ignore[arg-type]

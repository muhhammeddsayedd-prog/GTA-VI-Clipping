"""YouTube quota counters over the Pacific quota day; the `youtube_channel_cache` table (migration 0024)."""

from datetime import UTC, datetime, timedelta

from sixstars import db, settings
from sixstars.discovery import quota, store


def _usage(operation: str, units: int, at: datetime, provider: str = "youtube") -> None:
    db.execute(
        "insert into api_usage (provider, operation, units, created_at) values (%s, %s, %s, %s)",
        (provider, operation, units, at),
    )


def test_migration_seeds_the_quota_settings() -> None:
    assert settings.get("discovery.youtube_quota_daily_units") == 9000
    assert settings.get("discovery.youtube_quota_timezone") == "America/Los_Angeles"
    assert settings.get("discovery.youtube_channel_cache_ttl_s") == 3600


def test_units_are_counted_from_midnight_pacific() -> None:
    now = datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
    start = quota.day_start(now)  # 2026-11-18 08:00 UTC (midnight PST)
    assert start == datetime(2026, 11, 18, 8, 0, tzinfo=UTC)
    _usage("videos.list", 5, start - timedelta(minutes=1))  # yesterday's quota day, though today in UTC
    _usage("videos.list", 7, start + timedelta(minutes=1))
    _usage("channels.list", 3, start + timedelta(hours=2))
    _usage("search.list", 1, start + timedelta(hours=1))  # its own bucket
    _usage("search.list", 1, start - timedelta(hours=1))
    _usage("channel_feed", 0, start + timedelta(hours=1))
    _usage("anthropic", 9, start + timedelta(hours=1), provider="anthropic")
    assert store.youtube_units_today(now) == 10.0
    assert store.youtube_search_calls_today(now) == 1


def test_the_quota_timezone_is_a_setting() -> None:
    now = datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
    _usage("videos.list", 5, datetime(2026, 11, 18, 1, 0, tzinfo=UTC))  # 17:00 PST the day before
    assert store.youtube_units_today(now) == 0.0
    settings.set("discovery.youtube_quota_timezone", "UTC")
    assert store.youtube_units_today(now) == 5.0


def test_channel_cache_round_trip() -> None:
    store.save_youtube_channels(
        [
            {"id": "UCa", "snippet": {"title": "A"}},
            {"id": "UCa", "snippet": {"title": "A again"}},  # one row per id in one statement
            {"id": "UCgone", "missing": True},
            {"snippet": {"title": "no id"}},
        ]
    )
    cached = store.cached_youtube_channels(["UCa", "UCgone", "UCnew"], 3600)
    assert cached == {
        "UCa": {"id": "UCa", "snippet": {"title": "A again"}},
        "UCgone": {"id": "UCgone", "missing": True},
    }
    store.save_youtube_channels([{"id": "UCa", "snippet": {"title": "B"}}])  # upsert
    assert store.cached_youtube_channels(["UCa"], 3600)["UCa"]["snippet"]["title"] == "B"
    db.execute(
        "update youtube_channel_cache set fetched_at = now() - interval '2 hours' where channel_id = 'UCa'"
    )
    assert store.cached_youtube_channels(["UCa"], 3600) == {}
    assert store.cached_youtube_channels(["UCgone"], 0) == {}
    assert store.cached_youtube_channels([], 3600) == {}

"""The YouTube Data API quota day (pure, no network).

"Daily quotas reset at midnight Pacific Time (PT)."
(https://developers.google.com/youtube/v3/determine_quota_cost, checked 2026-11-18), so the units we count
against `discovery.youtube_quota_daily_units` (and the `search.list` calls of its own bucket) are summed from
the last midnight in `discovery.youtube_quota_timezone`. The money budgets keep their UTC day.
"""

from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE: Final = "America/Los_Angeles"
TIMEZONE_KEY: Final = "discovery.youtube_quota_timezone"


def day_start(now: datetime, tz: str = DEFAULT_TIMEZONE) -> datetime:
    """The last local midnight in `tz` at or before `now`, in UTC. DST-correct: the offset is the one in
    force at that midnight, not at `now`."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    midnight = datetime(local.year, local.month, local.day, tzinfo=zone)
    return midnight.astimezone(UTC)


def next_reset(now: datetime, tz: str = DEFAULT_TIMEZONE) -> datetime:
    """The next local midnight in `tz` after `now`, in UTC (when the quota resets)."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    tomorrow = (datetime(local.year, local.month, local.day) + timedelta(days=1)).replace(tzinfo=zone)
    return tomorrow.astimezone(UTC)

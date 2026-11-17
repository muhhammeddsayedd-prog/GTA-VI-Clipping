"""Settings of the Twitch Clips source (migration 0019), read once per pass or per job.

`DEFAULTS` mirrors the migration, except `twitch_clips.enabled`: a database without the migration has no
`viewer_clips` table, so there the source must stay off.
"""

from dataclasses import dataclass
from typing import Any, Final

from sixstars import settings
from sixstars.gate import profiles

DEFAULTS: Final[dict[str, Any]] = {
    "twitch_clips.lookback_h": 24,
    "twitch_clips.read_per_pass": 100,
    "twitch_clips.max_queued_per_pass": 3,
    "twitch_clips.max_per_creator_per_pass": 1,
    "twitch_clips.min_views": 10,
    "twitch_clips.daily_cap": 48,
    "twitch_clips.max_backlog": 6,
    "twitch_clips.min_age_min": 15,
    "twitch_clips.rate_floor_h": 1,
}
RENDER_WITHOUT_MOMENT_KEY: Final = "twitch_clips.render_without_moment"


def enabled() -> bool:
    """`twitch_clips.enabled`; off for a database that does not have the setting (before migration 0019)."""
    return settings.get_bool("twitch_clips.enabled", False)


@dataclass(frozen=True)
class ClipsConfig:
    """Snapshot of the source's settings. `min_len_s` / `max_len_s` are the hourly gate profile's limits
    and `source_pad_s` is the gate's `gate.duplicate.source_pad_s`: the source reads them, it does not
    define them."""

    enabled: bool
    lookback_h: float
    read_per_pass: int
    max_queued_per_pass: int
    max_per_creator_per_pass: int
    min_views: int
    daily_cap: int
    max_backlog: int
    min_age_min: float
    rate_floor_h: float
    render_without_moment: bool
    min_len_s: float
    max_len_s: float
    source_pad_s: float

    @classmethod
    def load(cls) -> "ClipsConfig":
        """Read the settings table (through the 30-second cache). Raises `GateSettingsError` when the
        gate profile is malformed: without its length limits nothing can be chosen."""
        gate = profiles.load()

        def whole(key: str) -> int:
            return max(0, settings.get_int(f"twitch_clips.{key}", DEFAULTS[f"twitch_clips.{key}"]))

        def number(key: str) -> float:
            return max(0.0, settings.get_float(f"twitch_clips.{key}", DEFAULTS[f"twitch_clips.{key}"]))

        return cls(
            enabled=enabled(),
            lookback_h=number("lookback_h"),
            read_per_pass=whole("read_per_pass"),
            max_queued_per_pass=whole("max_queued_per_pass"),
            max_per_creator_per_pass=whole("max_per_creator_per_pass"),
            min_views=whole("min_views"),
            daily_cap=whole("daily_cap"),
            max_backlog=whole("max_backlog"),
            min_age_min=number("min_age_min"),
            rate_floor_h=number("rate_floor_h"),
            render_without_moment=settings.get_bool(RENDER_WITHOUT_MOMENT_KEY, False),
            min_len_s=gate.hourly.min_len_s,
            max_len_s=gate.hourly.max_len_s,
            source_pad_s=gate.source_pad_s,
        )

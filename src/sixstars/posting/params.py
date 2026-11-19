"""Typed posting parameters. Every threshold the rules use comes from here; nothing in `sixstars.posting`
reads the settings table. `PostingParams.from_settings` takes a plain mapping of settings keys to JSON values.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Self

from sixstars.posting.models import Platform, ensure_utc

APP_ENVS = frozenset({"staging", "production"})

# Keys that must exist. A missing or malformed value raises, so a broken settings table posts nothing.
REQUIRED_KEYS = (
    "control.emergency_stop",
    "launch.dry_run",
    "launch.launch_at",
    "posting.jitter_max_s",
    "posting.min_gap_s",
    "posting.spike_max_per_hour",
    "posting.skip_hourly_after_spike_s",
    "posting.spike_max_wait_s",
    "posting.spike_max_age_s",
    "posting.freshness_half_life_h",
    "posting.max_clip_age_h",
    "posting.first_comment",
    "posting.base_hashtags",
    "posting.youtube_extra_hashtags",
    "posting.platform_max_24h",
)
# Keys that may be absent. Absent always means the reading that posts less.
OPTIONAL_KEYS = (
    "posting.staging_allow_instagram",  # absent = false
    "posting.reserve_release_hour_utc",  # absent or null = unused spike reserve is never released
    "posting.reserve_release_keep",
    "posting.spike_hold_s",  # absent = 0: spike clips post as soon as they are ready
    # development-plan/M18 Phase C (migration 0029). Without story cuts neither matters.
    "posting.sibling_gap_h",  # absent = 12
    "posting.max_duration_s",  # absent = no limit per platform
)
DEFAULT_SIBLING_GAP_H = 12.0


@dataclass(frozen=True, slots=True)
class PostingParams:
    app_env: str
    emergency_stop: bool
    dry_run: bool
    launch_at: datetime
    jitter_max_s: int
    min_gap_s: int
    spike_max_per_hour: int
    skip_hourly_after_spike_s: int
    spike_max_wait_s: int
    spike_max_age_s: int
    freshness_half_life_live_h: float
    freshness_half_life_vod_h: float
    max_clip_age_h: float
    first_comment: str
    base_hashtags: tuple[str, ...]
    youtube_extra_hashtags: tuple[str, ...]
    platform_max_24h: Mapping[Platform, int]
    staging_allow_instagram: bool = False
    reserve_release_hour_utc: int | None = None
    reserve_release_keep: int | None = None
    # R12 (holding window): spike clips wait this long after the first of them is approved, so the best
    # clip of the batch takes the spike post. 0 posts each spike clip as soon as it is ready.
    spike_hold_s: int = 0
    # M18 Phase C: two clips of one family (a short clip and its story cut) go to the same account at least
    # this many hours apart; a clip longer than its platform's limit is not posted there.
    sibling_gap_h: float = DEFAULT_SIBLING_GAP_H
    max_duration_s: Mapping[Platform, float] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.app_env not in APP_ENVS:
            raise ValueError(f"app_env must be one of {sorted(APP_ENVS)}, got {self.app_env!r}")
        ensure_utc(self.launch_at, "launch.launch_at")
        for name in (
            "jitter_max_s",
            "min_gap_s",
            "spike_max_per_hour",
            "skip_hourly_after_spike_s",
            "spike_max_wait_s",
            "spike_max_age_s",
            "spike_hold_s",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"posting.{name} must not be negative")
        if self.freshness_half_life_live_h <= 0 or self.freshness_half_life_vod_h <= 0:
            raise ValueError("posting.freshness_half_life_h values must be positive")
        if self.sibling_gap_h < 0 or any(limit <= 0 for limit in self.max_duration_s.values()):
            raise ValueError("posting.sibling_gap_h must not be negative and max_duration_s must be positive")
        if self.max_clip_age_h <= 0:
            raise ValueError("posting.max_clip_age_h must be positive")
        missing = [p.value for p in Platform if p not in self.platform_max_24h]
        if missing:
            raise ValueError(f"posting.platform_max_24h has no limit for {missing}")
        if any(limit < 0 for limit in self.platform_max_24h.values()):
            raise ValueError("posting.platform_max_24h values must not be negative")
        if (self.reserve_release_hour_utc is None) != (self.reserve_release_keep is None):
            raise ValueError("posting.reserve_release_hour_utc and posting.reserve_release_keep go together")
        if self.reserve_release_hour_utc is not None and not 0 <= self.reserve_release_hour_utc <= 23:
            raise ValueError("posting.reserve_release_hour_utc must be within 0..23")
        if self.reserve_release_keep is not None and self.reserve_release_keep < 0:
            raise ValueError("posting.reserve_release_keep must not be negative")

    @classmethod
    def from_settings(cls, values: Mapping[str, object], app_env: str) -> Self:
        """Builds the parameters from settings keys (`posting.min_gap_s`, ...) and the `APP_ENV` value."""
        for key in REQUIRED_KEYS:
            if key not in values:
                raise KeyError(f"missing setting {key}")
        half_life = _mapping("posting.freshness_half_life_h", values["posting.freshness_half_life_h"])
        for source in ("live", "vod"):
            if source not in half_life:
                raise KeyError(f"posting.freshness_half_life_h has no value for {source!r}")
        release_hour = values.get("posting.reserve_release_hour_utc")
        release_keep = values.get("posting.reserve_release_keep")
        return cls(
            app_env=app_env,
            emergency_stop=_bool("control.emergency_stop", values["control.emergency_stop"]),
            dry_run=_bool("launch.dry_run", values["launch.dry_run"]),
            launch_at=_time("launch.launch_at", values["launch.launch_at"]),
            jitter_max_s=_int("posting.jitter_max_s", values["posting.jitter_max_s"]),
            min_gap_s=_int("posting.min_gap_s", values["posting.min_gap_s"]),
            spike_max_per_hour=_int("posting.spike_max_per_hour", values["posting.spike_max_per_hour"]),
            skip_hourly_after_spike_s=_int(
                "posting.skip_hourly_after_spike_s", values["posting.skip_hourly_after_spike_s"]
            ),
            spike_max_wait_s=_int("posting.spike_max_wait_s", values["posting.spike_max_wait_s"]),
            spike_max_age_s=_int("posting.spike_max_age_s", values["posting.spike_max_age_s"]),
            freshness_half_life_live_h=_number("posting.freshness_half_life_h.live", half_life["live"]),
            freshness_half_life_vod_h=_number("posting.freshness_half_life_h.vod", half_life["vod"]),
            max_clip_age_h=_number("posting.max_clip_age_h", values["posting.max_clip_age_h"]),
            first_comment=_str("posting.first_comment", values["posting.first_comment"]),
            base_hashtags=_str_list("posting.base_hashtags", values["posting.base_hashtags"]),
            youtube_extra_hashtags=_str_list(
                "posting.youtube_extra_hashtags", values["posting.youtube_extra_hashtags"]
            ),
            platform_max_24h=_platform_limits(values["posting.platform_max_24h"]),
            staging_allow_instagram=_bool(
                "posting.staging_allow_instagram", values.get("posting.staging_allow_instagram", False)
            ),
            reserve_release_hour_utc=None
            if release_hour is None
            else _int("posting.reserve_release_hour_utc", release_hour),
            reserve_release_keep=None
            if release_keep is None
            else _int("posting.reserve_release_keep", release_keep),
            spike_hold_s=_int("posting.spike_hold_s", values.get("posting.spike_hold_s", 0)),
            sibling_gap_h=_number(
                "posting.sibling_gap_h", values.get("posting.sibling_gap_h", DEFAULT_SIBLING_GAP_H)
            ),
            max_duration_s=_duration_limits(values.get("posting.max_duration_s", {})),
        )

    def too_long(self, platform: Platform, duration_s: float | None) -> bool:
        """Is a clip of `duration_s` seconds over `platform`'s limit (`posting.max_duration_s`)?"""
        limit = self.max_duration_s.get(platform)
        return limit is not None and duration_s is not None and duration_s > limit


def _duration_limits(value: object) -> Mapping[Platform, float]:
    raw = _mapping("posting.max_duration_s", value)
    known = {platform.value: platform for platform in Platform}
    return MappingProxyType(
        {known[name]: _number(f"posting.max_duration_s.{name}", raw[name]) for name in raw if name in known}
    )


def _bool(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"setting {key} must be true or false, got {value!r}")
    return value


def _int(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"setting {key} must be a whole number, got {value!r}")
    return value


def _number(key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"setting {key} must be a number, got {value!r}")
    return float(value)


def _str(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"setting {key} must be text, got {value!r}")
    return value


def _str_list(key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"setting {key} must be a list of text values, got {value!r}")
    return tuple(value)


def _mapping(key: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"setting {key} must be an object, got {value!r}")
    return value


def _time(key: str, value: object) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value, key)
    if not isinstance(value, str):
        raise TypeError(f"setting {key} must be an ISO 8601 time, got {value!r}")
    return ensure_utc(datetime.fromisoformat(value), key)


def _platform_limits(value: object) -> Mapping[Platform, int]:
    key = "posting.platform_max_24h"
    raw = _mapping(key, value)
    return MappingProxyType({Platform(name): _int(f"{key}.{name}", limit) for name, limit in raw.items()})

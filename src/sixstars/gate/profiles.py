"""Gate settings: the two profiles and every other limit, read from the `settings` table.

`settings_from_values` is pure (a mapping of setting key to JSON value in, `GateSettings` out) and refuses a
missing or malformed value: the gate does not guess a limit. `load()` reads the table without the 30-second
cache, so a change to a limit applies to the next clip.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sixstars import db, lengths
from sixstars.gate.models import EXPRESS, HOURLY, GateSettings, ProfileLimits

EXPRESS_TIER_TOKENS = ("preferred", "auto", "auto_no_music")

KEYS = (
    "gate.hourly",
    "gate.express",
    "gate.spoiler_block_until",
    "gate.spoiler_terms",
    "gate.reject_topics",
    "gate.auto_creator_daily_cap",
    "gate.strict_max_music_ratio",
    "gate.music_min_run_frames",
    "gate.checks",
    "gate.duplicate",
    "factory.end_card_s",
)


class GateSettingsError(ValueError):
    """A gate setting is missing or has the wrong shape."""


def _number(value: object, key: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GateSettingsError(f"setting {key} must be a number, got {value!r}")
    if value < minimum:
        raise GateSettingsError(f"setting {key} must be at least {minimum:g}, got {value!r}")
    return float(value)


def _ratio(value: object, key: str) -> float:
    number = _number(value, key)
    if number > 1.0:
        raise GateSettingsError(f"setting {key} must be between 0 and 1, got {value!r}")
    return number


def _object(values: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = values.get(key)
    if not isinstance(value, Mapping):
        raise GateSettingsError(f"setting {key} must be an object, got {value!r}")
    return value


def _strings(values: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = values.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GateSettingsError(f"setting {key} must be an array of strings, got {value!r}")
    return tuple(item.strip() for item in value if item.strip())


def _time(values: Mapping[str, Any], key: str) -> datetime | None:
    if key not in values:
        raise GateSettingsError(f"setting {key} is missing")
    value = values[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise GateSettingsError(f"setting {key} must be an ISO 8601 time or null, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GateSettingsError(f"setting {key} is not an ISO 8601 time: {value!r}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _profile(values: Mapping[str, Any], name: str) -> ProfileLimits:
    key = f"gate.{name}"
    raw = _object(values, key)
    min_len = _number(raw.get("min_len"), f"{key}.min_len")
    max_len = _number(raw.get("max_len"), f"{key}.max_len")
    if max_len < min_len:
        raise GateSettingsError(f"setting {key}: max_len is below min_len")
    tiers: tuple[str, ...] = ()
    if name == EXPRESS:
        listed = raw.get("tiers")
        if not isinstance(listed, list) or not all(item in EXPRESS_TIER_TOKENS for item in listed):
            raise GateSettingsError(f"setting {key}.tiers must list some of {EXPRESS_TIER_TOKENS}")
        tiers = tuple(listed)
    precheck_risky = raw.get("precheck_risky", True)
    if not isinstance(precheck_risky, bool):
        raise GateSettingsError(f"setting {key}.precheck_risky must be a boolean")
    return ProfileLimits(
        name=name,
        min_len_s=min_len,
        max_len_s=max_len,
        max_music_ratio=_ratio(raw.get("max_music_ratio"), f"{key}.max_music_ratio"),
        min_score=_number(raw.get("min_score"), f"{key}.min_score"),
        topic_dedupe_h=_number(raw.get("topic_dedupe_h"), f"{key}.topic_dedupe_h"),
        tiers=tiers,
        precheck_risky=precheck_risky,
    )


def with_platform_lengths(settings: GateSettings, raw: object) -> GateSettings:
    """`gate.hourly_len_by_platform` (migration 0036): the hourly profile allows the union of every
    platform's window, so a clip that fits any platform is approved (posting sends it only there). Only the
    gate's verdict uses it; sources that read `gate.hourly` for their own limits (Twitch clips) do not.
    Pure; unchanged without the setting."""
    hourly = settings.hourly
    widest = lengths.union(lengths.LengthWindow(hourly.min_len_s, hourly.max_len_s), raw)
    return replace(settings, hourly=replace(hourly, min_len_s=widest.min_s, max_len_s=widest.max_s))


def settings_from_values(values: Mapping[str, Any]) -> GateSettings:
    """Build `GateSettings` from setting values keyed by setting name. Raises `GateSettingsError`."""
    checks = _object(values, "gate.checks")
    duplicate = _object(values, "gate.duplicate")
    min_run = _number(values.get("gate.music_min_run_frames"), "gate.music_min_run_frames", minimum=1)
    cap = _number(values.get("gate.auto_creator_daily_cap"), "gate.auto_creator_daily_cap")
    return GateSettings(
        hourly=_profile(values, HOURLY),
        express=_profile(values, EXPRESS),
        spoiler_block_until=_time(values, "gate.spoiler_block_until"),
        spoiler_terms=_strings(values, "gate.spoiler_terms"),
        reject_topics=_strings(values, "gate.reject_topics"),
        auto_creator_daily_cap=int(cap),
        strict_max_music_ratio=_ratio(
            values.get("gate.strict_max_music_ratio"), "gate.strict_max_music_ratio"
        ),
        music_min_run_frames=int(min_run),
        max_black_s=_number(checks.get("max_black_s"), "gate.checks.max_black_s"),
        max_freeze_s=_number(checks.get("max_freeze_s"), "gate.checks.max_freeze_s"),
        freeze_noise=_number(checks.get("freeze_noise"), "gate.checks.freeze_noise"),
        band_freeze_noise=_number(checks.get("band_freeze_noise"), "gate.checks.band_freeze_noise"),
        band_black_pixel=_ratio(checks.get("band_black_pixel"), "gate.checks.band_black_pixel"),
        max_subtitle_drift_s=_number(checks.get("max_subtitle_drift_s"), "gate.checks.max_subtitle_drift_s"),
        source_pad_s=_number(duplicate.get("source_pad_s"), "gate.duplicate.source_pad_s"),
        max_file_mb=_number(checks.get("max_file_mb"), "gate.checks.max_file_mb"),
        width=int(_number(checks.get("width"), "gate.checks.width", minimum=1)),
        height=int(_number(checks.get("height"), "gate.checks.height", minimum=1)),
        duration_tolerance_s=_number(checks.get("duration_tolerance_s"), "gate.checks.duration_tolerance_s"),
        end_card_s=_number(values.get("factory.end_card_s"), "factory.end_card_s"),
        topic_overlap_same_creator=_ratio(
            duplicate.get("topic_overlap_same_creator"), "gate.duplicate.topic_overlap_same_creator"
        ),
        topic_overlap_any_creator=_ratio(
            duplicate.get("topic_overlap_any_creator"), "gate.duplicate.topic_overlap_any_creator"
        ),
        transcript_overlap=_ratio(duplicate.get("transcript_overlap"), "gate.duplicate.transcript_overlap"),
    )


def load() -> GateSettings:
    """Read the gate settings from the table (uncached)."""
    rows = db.fetch_all("select key, value from settings where key = any(%s)", (list(KEYS),))
    return settings_from_values({str(row["key"]): row["value"] for row in rows})

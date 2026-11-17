"""The VOD lane's parameters as one frozen object, read from the `settings` table once per job. Implemented
in M08.

Keys that migration 0008 adds have their default in `DEFAULTS`, so a database that has not received the
migration yet behaves the same. Everything else must exist in the table.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sixstars import settings

# Mirrors supabase/migrations/0008_vod.sql.
DEFAULTS: Final[dict[str, Any]] = {
    "vod.max_concurrent": 1,
    "vod.concurrency_retry_s": 120,
    "vod.processing_stale_s": 7200,
    "vod.render_priority": {"S": 55, "A": 55, "B": 40},
    "vod.clipped_range_margin_s": 30,
    "vod.chat_retry_delay_s": 20,
    "vod.confirm_with_captions": True,
    "vod.caption_langs": [".*-orig", "en"],
    "vod.caption_timeout_s": 180,
    "vod.download_timeout_s": 900,
    "vod.download_failures_max": 3,
    "vod.download_workers": 3,
    "vod.section_exact_cuts": False,
    "vod.blocked_backoff_s": 1800,
    "vod.blocked_notify_interval_s": 3600,
    "vod.stop_retry_s": 600,
    "vod.fallback_enabled": True,
    "vod.fallback_below_spikes": 3,  # 1 in 0008, raised by 0010
    "vod.fallback_max_duration_s": 43200,
    "vod.fallback_min_confidence": 0.5,
    "vod.fallback_chunk_s": 30,
    "vod.fallback_max_transcript_s": 21600,
    "vod.fallback_transcribe_mode": "local",
    "vod.fallback_transcribe_max_s": 0,
    "vod.fallback_transcribe_daily_s": 21600,
    "vod.fallback_transcribe_block_s": 600,
    "vod.fallback_whisper_model": None,
    "vod.section_retention_s": 93600,
    "vod.chat_retention_s": 604800,
    "vod.cleanup_interval_s": 900,
}
TIERS: Final = ("S", "A", "B")


class VodConfigError(ValueError):
    """A VOD setting has the wrong JSON type or an impossible value."""


def _number(values: Mapping[str, Any], key: str) -> float:
    value = values[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise VodConfigError(f"setting {key} must be a number, got {value!r}")
    return float(value)


def _flag(values: Mapping[str, Any], key: str) -> bool:
    value = values[key]
    if not isinstance(value, bool):
        raise VodConfigError(f"setting {key} must be true or false, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class VodConfig:
    """Everything the lane reads from settings."""

    max_spikes_per_video: int
    fallback_moments: tuple[int, int]
    whisper_model: str
    max_height: int
    end_guard_s: float
    min_len_s: float
    max_len_s: float
    context_pad_s: float
    max_concurrent: int
    concurrency_retry_s: float
    processing_stale_s: float
    render_priority: Mapping[str, int]
    clipped_range_margin_s: float
    chat_retry_delay_s: float
    confirm_with_captions: bool
    caption_langs: tuple[str, ...]
    caption_timeout_s: float
    download_timeout_s: float
    download_failures_max: int
    download_workers: int
    section_exact_cuts: bool
    blocked_backoff_s: float
    blocked_notify_interval_s: float
    stop_retry_s: float
    fallback_enabled: bool
    fallback_below_spikes: int
    fallback_max_duration_s: float
    fallback_min_confidence: float
    fallback_chunk_s: float
    fallback_max_transcript_s: float
    fallback_transcribe_mode: str
    fallback_transcribe_max_s: float
    fallback_transcribe_daily_s: float
    fallback_transcribe_block_s: float
    fallback_whisper_model: str | None
    section_retention_s: float
    chat_retention_s: float
    cleanup_interval_s: float

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "VodConfig":
        """Build the config from `{setting key: JSON value}`; keys of `DEFAULTS` may be missing."""
        merged: dict[str, Any] = {**DEFAULTS, **values}
        moments = merged["vod.fallback_moments"]
        if not isinstance(moments, list) or len(moments) != 2:
            raise VodConfigError(f"setting vod.fallback_moments must be [min, max], got {moments!r}")
        priority = merged["vod.render_priority"]
        if not isinstance(priority, dict):
            raise VodConfigError(f"setting vod.render_priority must be an object, got {priority!r}")
        langs = merged["vod.caption_langs"]
        if not isinstance(langs, list):
            raise VodConfigError(f"setting vod.caption_langs must be a list, got {langs!r}")
        mode = str(merged["vod.fallback_transcribe_mode"])
        if mode not in ("local", "hosted"):
            raise VodConfigError(
                f"setting vod.fallback_transcribe_mode must be local or hosted, got {mode!r}"
            )
        fallback_model = merged["vod.fallback_whisper_model"]
        return cls(
            max_spikes_per_video=int(_number(merged, "vod.max_spikes_per_video")),
            fallback_moments=(int(moments[0]), int(moments[1])),
            whisper_model=str(merged["vod.whisper_model"]),
            max_height=int(_number(merged, "downloads.max_height")),
            end_guard_s=_number(merged, "spikes.end_guard_s"),
            min_len_s=_number(merged, "spikes.min_len_s"),
            max_len_s=_number(merged, "spikes.max_len_s"),
            context_pad_s=_number(merged, "spikes.context_pad_s"),
            max_concurrent=max(1, int(_number(merged, "vod.max_concurrent"))),
            concurrency_retry_s=_number(merged, "vod.concurrency_retry_s"),
            processing_stale_s=_number(merged, "vod.processing_stale_s"),
            render_priority={tier: int(priority.get(tier, 0)) for tier in TIERS},
            clipped_range_margin_s=_number(merged, "vod.clipped_range_margin_s"),
            chat_retry_delay_s=_number(merged, "vod.chat_retry_delay_s"),
            confirm_with_captions=_flag(merged, "vod.confirm_with_captions"),
            caption_langs=tuple(str(lang) for lang in langs),
            caption_timeout_s=_number(merged, "vod.caption_timeout_s"),
            download_timeout_s=_number(merged, "vod.download_timeout_s"),
            download_failures_max=max(1, int(_number(merged, "vod.download_failures_max"))),
            download_workers=max(1, int(_number(merged, "vod.download_workers"))),
            section_exact_cuts=_flag(merged, "vod.section_exact_cuts"),
            blocked_backoff_s=_number(merged, "vod.blocked_backoff_s"),
            blocked_notify_interval_s=_number(merged, "vod.blocked_notify_interval_s"),
            stop_retry_s=_number(merged, "vod.stop_retry_s"),
            fallback_enabled=_flag(merged, "vod.fallback_enabled"),
            fallback_below_spikes=int(_number(merged, "vod.fallback_below_spikes")),
            fallback_max_duration_s=_number(merged, "vod.fallback_max_duration_s"),
            fallback_min_confidence=_number(merged, "vod.fallback_min_confidence"),
            fallback_chunk_s=_number(merged, "vod.fallback_chunk_s"),
            fallback_max_transcript_s=_number(merged, "vod.fallback_max_transcript_s"),
            fallback_transcribe_mode=mode,
            fallback_transcribe_max_s=_number(merged, "vod.fallback_transcribe_max_s"),
            fallback_transcribe_daily_s=_number(merged, "vod.fallback_transcribe_daily_s"),
            fallback_transcribe_block_s=_number(merged, "vod.fallback_transcribe_block_s"),
            fallback_whisper_model=str(fallback_model) if fallback_model else None,
            section_retention_s=_number(merged, "vod.section_retention_s"),
            chat_retention_s=_number(merged, "vod.chat_retention_s"),
            cleanup_interval_s=_number(merged, "vod.cleanup_interval_s"),
        )

    @classmethod
    def load(cls) -> "VodConfig":
        """Read the lane's settings from the table (30-second cache)."""
        required = (
            "vod.max_spikes_per_video",
            "vod.fallback_moments",
            "vod.whisper_model",
            "downloads.max_height",
            "spikes.end_guard_s",
            "spikes.min_len_s",
            "spikes.max_len_s",
            "spikes.context_pad_s",
        )
        values: dict[str, Any] = {key: settings.get(key) for key in required}
        for key, default in DEFAULTS.items():
            values[key] = settings.get(key, default)
        return cls.from_mapping(values)

    def priority_for(self, tier: str) -> int:
        """Priority of the `clip.render` job of a spike of this tier."""
        return int(self.render_priority.get(tier, 0))

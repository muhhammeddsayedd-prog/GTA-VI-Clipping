"""Settings the factory reads besides `factory.*`: clip length limits, the context pad, the local Whisper
model and the music limit of the lane's gate profile. Loaded once per render from the `settings` table.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from sixstars import settings
from sixstars.factory.params import FactoryParams

Lane = Literal["spike", "hourly"]
GateProfile = Literal["hourly", "express"]

# The documented defaults (docs/16-settings-reference.md). Used only by `sixstars clip render-file` when no
# database is configured, so the look of a clip can be worked on offline; every service reads the table.
OFFLINE_DEFAULTS: dict[str, object] = {
    "factory.width": 1080,
    "factory.height": 1920,
    "factory.fps": 30,
    "factory.crf": 21,
    "factory.preset_normal": "veryfast",
    "factory.preset_express": "superfast",
    "factory.use_nvenc": False,
    "factory.loudness_lufs": -14,
    "factory.safe_top_px": 220,
    "factory.safe_bottom_px": 420,
    "factory.accent_color": "#FFD400",
    "factory.font": "Montserrat Black",
    "factory.hook_seconds": 3.0,
    "factory.end_card_s": 1.5,
    "factory.watermark": "@6starsclipping",
    "factory.series_label": "6 Stars Moments",
    "factory.bleep": True,
    "factory.music_duck_db": -18,
    "spikes.min_len_s": 15,
    "spikes.max_len_s": 60,
    "spikes.context_pad_s": 15,
    "vod.whisper_model": "medium",
    "gate.hourly": {"min_len": 20, "max_len": 60, "max_music_ratio": 0.1},
    "gate.express": {"min_len": 15, "max_len": 45, "max_music_ratio": 0.0},
}


def gate_profile_for(lane: str) -> GateProfile:
    """Spike-lane clips are judged by the express profile, everything else by the hourly one."""
    return "express" if lane == "spike" else "hourly"


def _number(value: object, fallback: float) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return fallback


@dataclass(frozen=True, slots=True)
class FactoryConfig:
    """Everything one render reads from the settings.

    `min_len_s` / `max_len_s` are the limits given to the clip writer: `spikes.min_len_s` /
    `spikes.max_len_s` narrowed by the `min_len` / `max_len` of the lane's gate profile, so the writer
    does not produce a clip the gate would reject for its length.
    """

    params: FactoryParams
    min_len_s: float
    max_len_s: float
    context_pad_s: float
    whisper_model: str
    gate_profile: GateProfile
    max_music_ratio: float

    @classmethod
    def from_mapping(cls, values: Mapping[str, object], lane: str) -> "FactoryConfig":
        """Build the configuration from a mapping of settings keys to JSON values."""
        profile = gate_profile_for(lane)
        raw_gate = values.get(f"gate.{profile}")
        gate: Mapping[str, object] = raw_gate if isinstance(raw_gate, Mapping) else {}
        min_len = _number(values["spikes.min_len_s"], 0.0)
        max_len = _number(values["spikes.max_len_s"], 0.0)
        if min_len <= 0 or max_len < min_len:
            raise ValueError("spikes.min_len_s and spikes.max_len_s must satisfy 0 < min <= max")
        low = max(min_len, _number(gate.get("min_len"), min_len))
        high = min(max_len, _number(gate.get("max_len"), max_len))
        if high < low:
            low, high = min_len, max_len
        whisper = values["vod.whisper_model"]
        if not isinstance(whisper, str) or not whisper.strip():
            raise TypeError("setting vod.whisper_model must be a string")
        return cls(
            params=FactoryParams.from_settings(values),
            min_len_s=low,
            max_len_s=high,
            context_pad_s=max(0.0, _number(values.get("spikes.context_pad_s"), 0.0)),
            whisper_model=whisper,
            gate_profile=profile,
            max_music_ratio=max(0.0, _number(gate.get("max_music_ratio"), 0.0)),
        )

    @classmethod
    def load(cls, lane: str) -> "FactoryConfig":
        """Read the configuration from the `settings` table."""
        keys = (
            *FactoryParams.setting_keys(),
            "spikes.min_len_s",
            "spikes.max_len_s",
            "spikes.context_pad_s",
            "vod.whisper_model",
        )
        values: dict[str, object] = {key: settings.get(key) for key in keys}
        profile = gate_profile_for(lane)
        values[f"gate.{profile}"] = settings.get(f"gate.{profile}", {})
        return cls.from_mapping(values, lane)

    @classmethod
    def offline(cls, lane: str, overrides: Mapping[str, object] | None = None) -> "FactoryConfig":
        """The documented defaults plus `overrides`, without a database (design iteration only)."""
        return cls.from_mapping({**OFFLINE_DEFAULTS, **(overrides or {})}, lane)

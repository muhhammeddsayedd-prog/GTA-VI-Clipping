"""The cover engine's `cover.*` keys in `scoring_config` (migration 0044), as one versioned snapshot.

Read with `CoverConfig.load()` (cached 30 s like every scoring_config read); `CoverConfig.from_values` is
the pure parser the tests use. Every malformed value falls back to its documented default, and the safety
limits of docs/29a section 4 are applied here (exploration never below 0.2, QA pass score 0-10), so no key
can loosen them.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from sixstars.scoring import config as scoring_config

LaneMode = Literal["full", "frame_only", "off"]
Platform = Literal["youtube", "instagram", "tiktok"]

PLATFORMS: tuple[Platform, ...] = ("instagram", "tiktok", "youtube")
DEFAULT_WEIGHTS: dict[str, float] = {
    "expression": 0.35,
    "face_area": 0.20,
    "sharpness": 0.15,
    "exposure": 0.10,
    "gameplay": 0.20,
}
DEFAULT_LANE_MODES: dict[str, LaneMode] = {"normal": "full", "spike": "full", "*": "frame_only"}
DEFAULT_BRAND_MARK: dict[str, bool] = {"normal": True, "spike": True, "*": False}
DEFAULT_DEADLINES: dict[str, float] = {"live": 60.0, "normal": 300.0}
DEFAULT_EXPOSURE: dict[str, float] = {
    "luma_min": 60.0,
    "luma_max": 200.0,
    "clipped_max": 0.10,
    "lift_below": 70.0,
    "lift_target": 75.0,
    "gamma_max": 1.25,
}
MIN_EXPLORATION = 0.2
WILDCARD = "*"


def lane_key(lane: str, campaign_slug: str | None = None) -> str:
    """The lane key the per-lane settings use: `campaign:<slug>` for a campaign clip, else `spike` for the
    live lane and `normal` for everything else. Pure."""
    if campaign_slug:
        return f"campaign:{campaign_slug}"
    return "spike" if lane == "spike" else "normal"


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _number(value: Any, default: float) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else default


def _per_lane(value: Any, default: Mapping[str, Any], allowed: tuple[Any, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return dict(default)
    out = {str(k): v for k, v in value.items() if v in allowed}
    out.setdefault(WILDCARD, default[WILDCARD])
    return out


@dataclass(frozen=True, slots=True)
class CoverConfig:
    """All `cover.*` keys and the scoring config version they were read at."""

    version: int = 0
    enabled: bool = False
    platforms: tuple[Platform, ...] = PLATFORMS
    youtube_custom_enabled: bool = False
    tiktok_image_enabled: bool = False
    lane_modes: Mapping[str, LaneMode] = field(default_factory=lambda: dict(DEFAULT_LANE_MODES))
    brand_mark: Mapping[str, bool] = field(default_factory=lambda: dict(DEFAULT_BRAND_MARK))
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    qa_pass_score: float = 7.0
    deadline_s: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_DEADLINES))
    upscale_enabled: bool = False
    exploration: float = MIN_EXPLORATION
    exposure: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_EXPOSURE))

    @classmethod
    def from_values(cls, raw: Mapping[str, Any], version: int = 0) -> "CoverConfig":
        """Parse the `cover.*` values (`raw` maps the full key to its JSON value). Pure."""

        def get(name: str) -> Any:
            return raw.get(f"cover.{name}")

        platforms_raw = get("platforms")
        platforms = (
            tuple(p for p in PLATFORMS if p in platforms_raw)
            if isinstance(platforms_raw, list)
            else PLATFORMS
        )
        weights_raw = get("weights")
        weights = dict(DEFAULT_WEIGHTS)
        if isinstance(weights_raw, Mapping):
            for name in DEFAULT_WEIGHTS:
                value = _number(weights_raw.get(name), -1.0)
                if value >= 0:
                    weights[name] = value
        deadlines_raw = get("deadline_s")
        deadlines = dict(DEFAULT_DEADLINES)
        if isinstance(deadlines_raw, Mapping):
            for name in DEFAULT_DEADLINES:
                value = _number(deadlines_raw.get(name), -1.0)
                if value > 0:
                    deadlines[name] = value
        exposure = dict(DEFAULT_EXPOSURE)
        exposure_raw = get("exposure")
        if isinstance(exposure_raw, Mapping):
            for name in DEFAULT_EXPOSURE:
                value = _number(exposure_raw.get(name), -1.0)
                if value >= 0:
                    exposure[name] = value
        exposure["gamma_max"] = min(max(exposure["gamma_max"], 1.0), 1.5)  # "mild": never more than 1.5
        return cls(
            version=version,
            enabled=_bool(get("enabled"), False),
            platforms=platforms,
            youtube_custom_enabled=_bool(get("youtube_custom_enabled"), False),
            tiktok_image_enabled=_bool(get("tiktok_image_enabled"), False),
            lane_modes=_per_lane(get("lane_modes"), DEFAULT_LANE_MODES, ("full", "frame_only", "off")),
            brand_mark=_per_lane(get("brand_mark"), DEFAULT_BRAND_MARK, (True, False)),
            weights=weights,
            qa_pass_score=min(max(_number(get("qa_pass_score"), 7.0), 0.0), 10.0),
            deadline_s=deadlines,
            upscale_enabled=_bool(get("upscale_enabled"), False),
            exploration=min(max(_number(get("exploration"), MIN_EXPLORATION), MIN_EXPLORATION), 1.0),
            exposure=exposure,
        )

    @classmethod
    def load(cls) -> "CoverConfig":
        """The current snapshot from `scoring_config`."""
        keys = (
            "enabled",
            "platforms",
            "youtube_custom_enabled",
            "tiktok_image_enabled",
            "lane_modes",
            "brand_mark",
            "weights",
            "qa_pass_score",
            "deadline_s",
            "upscale_enabled",
            "exploration",
            "exposure",
        )
        raw = {f"cover.{name}": scoring_config.raw(f"cover.{name}") for name in keys}
        return cls.from_values(raw, scoring_config.version())

    def mode_for(self, lane: str) -> LaneMode:
        """The lane's mode (`*` for lanes without their own entry); `off` everywhere while disabled."""
        if not self.enabled:
            return "off"
        return self.lane_modes.get(lane, self.lane_modes.get(WILDCARD, "frame_only"))

    def brand_mark_for(self, lane: str) -> bool:
        """Draw the VI mark on this lane's covers? Never in frame_only mode."""
        if self.mode_for(lane) != "full":
            return False
        return self.brand_mark.get(lane, self.brand_mark.get(WILDCARD, False))

    def runs_for(self, platform: str, lane: str) -> bool:
        """Does the engine make a cover for this platform and lane?"""
        return self.mode_for(lane) != "off" and platform in self.platforms

    def deadline_for(self, lane: str) -> float:
        """Seconds the cover may take: the live deadline on the spike lane, the normal one elsewhere."""
        return self.deadline_s["live"] if lane == "spike" else self.deadline_s["normal"]

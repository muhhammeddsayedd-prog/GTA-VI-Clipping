"""Typed bundle of the `longform.*` settings (migration 0058, docs/16 "Long-form episodes")."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sixstars import settings
from sixstars.longform.align import CutRules
from sixstars.longform.subs16x9 import OverlayStyle


class PublishMode(StrEnum):
    """How far a ready episode goes: off = build only, private = private YouTube video, public = published."""

    OFF = "off"
    PRIVATE = "private"
    PUBLIC = "public"


@dataclass(frozen=True)
class RenderSpec:
    """Encode of every segment: full frame, scale + pad, no crop. All segments share it so they concat."""

    width: int = 1920
    height: int = 1080
    fps: int = 30
    crf: int = 20
    maxrate: str = "8M"
    bufsize: str = "16M"
    preset: str = "veryfast"

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "RenderSpec":
        return cls(
            width=int(value.get("width", cls.width)),
            height=int(value.get("height", cls.height)),
            fps=int(value.get("fps", cls.fps)),
            crf=int(value.get("crf", cls.crf)),
            maxrate=str(value.get("maxrate", cls.maxrate)),
            bufsize=str(value.get("bufsize", cls.bufsize)),
            preset=str(value.get("preset", cls.preset)),
        )


@dataclass(frozen=True)
class RankWeights:
    """Weights of the candidate ranking (setting `longform.rank_weights`)."""

    score: float = 1.0
    spike_z: float = 1.0
    spike_z_cap: float = 5.0
    views: float = 1.0
    fresh: float = 0.5

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "RankWeights":
        return cls(
            score=float(value.get("score", cls.score)),
            spike_z=float(value.get("spike_z", cls.spike_z)),
            spike_z_cap=float(value.get("spike_z_cap", cls.spike_z_cap)),
            views=float(value.get("views", cls.views)),
            fresh=float(value.get("fresh", cls.fresh)),
        )


@dataclass(frozen=True)
class SoloRules:
    """Creator-day rules (setting `longform.solo`)."""

    pool_hours: int = 168
    min_s: float = 360.0
    target_s: float = 480.0
    max_s: float = 600.0
    cooldown_days: int = 14
    min_moments: int = 8

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "SoloRules":
        return cls(
            pool_hours=int(value.get("pool_hours", cls.pool_hours)),
            min_s=float(value.get("min_s", cls.min_s)),
            target_s=float(value.get("target_s", cls.target_s)),
            max_s=float(value.get("max_s", cls.max_s)),
            cooldown_days=int(value.get("cooldown_days", cls.cooldown_days)),
            min_moments=int(value.get("min_moments", cls.min_moments)),
        )


@dataclass(frozen=True)
class ThumbSpec:
    """Thumbnail generation (setting `longform.thumb`)."""

    ai: bool = True
    model: str = "gpt-image-2.6-sunburst"
    quality: str = "high"
    size: str = "1536x864"
    timeout_s: float = 240.0
    usd_max: float = 0.5
    frames: int = 2
    max_bytes: int = 2_000_000

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "ThumbSpec":
        return cls(
            ai=bool(value.get("ai", cls.ai)),
            model=str(value.get("model", cls.model)),
            quality=str(value.get("quality", cls.quality)),
            size=str(value.get("size", cls.size)),
            timeout_s=float(value.get("timeout_s", cls.timeout_s)),
            usd_max=float(value.get("usd_max", cls.usd_max)),
            frames=int(value.get("frames", cls.frames)),
            max_bytes=int(value.get("max_bytes", cls.max_bytes)),
        )


@dataclass(frozen=True)
class PublishSpec:
    """Upload of a ready episode (setting `longform.publish`)."""

    video_url_ttl_s: int = 21600
    wait_timeout_s: float = 5400.0
    poll_s: float = 20.0
    category_id: str = "20"

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "PublishSpec":
        return cls(
            video_url_ttl_s=int(value.get("video_url_ttl_s", cls.video_url_ttl_s)),
            wait_timeout_s=float(value.get("wait_timeout_s", cls.wait_timeout_s)),
            poll_s=float(value.get("poll_s", cls.poll_s)),
            category_id=str(value.get("category_id", cls.category_id)),
        )


@dataclass(frozen=True)
class LongformConfig:
    """Every number the long-form layer uses. Nothing here is hard-coded elsewhere."""

    enabled: bool
    publish_mode: PublishMode
    build_hour_utc: int
    publish_hour_utc: int
    pool_hours: int
    pool_statuses: tuple[str, ...]
    pool_reject_reasons: tuple[str, ...]
    min_score: float
    target_s: float
    min_s: float
    max_s: float
    segment_min_s: float
    segment_max_s: float
    lead_s: float
    tail_s: float
    max_per_creator: int
    max_creator_s: float
    reuse_after_days: int
    fetch_pad_s: float
    whisper_model: str
    render: RenderSpec
    priority: int
    keep_days: int
    cold_open_s: float
    end_card_s: float
    account_id: str
    rank_weights: RankWeights
    auto_build: bool
    cut: CutRules
    style: OverlayStyle
    format_cycle: tuple[str, ...] = ("mix", "creator")
    solo: SoloRules = SoloRules()
    thumb: ThumbSpec = ThumbSpec()
    publish: PublishSpec = PublishSpec()

    @classmethod
    def load(cls) -> "LongformConfig":
        """Read the settings table. A missing key raises KeyError: migration 0058 seeds them all."""
        return cls(
            enabled=settings.get_bool("longform.enabled"),
            publish_mode=PublishMode(settings.get_str("longform.publish_mode")),
            build_hour_utc=settings.get_int("longform.build_hour_utc"),
            publish_hour_utc=settings.get_int("longform.publish_hour_utc"),
            pool_hours=settings.get_int("longform.pool_hours"),
            pool_statuses=tuple(str(s) for s in settings.get_list("longform.pool_statuses")),
            pool_reject_reasons=tuple(str(s) for s in settings.get_list("longform.pool_reject_reasons")),
            min_score=settings.get_float("longform.min_score"),
            target_s=settings.get_float("longform.target_s"),
            min_s=settings.get_float("longform.min_s"),
            max_s=settings.get_float("longform.max_s"),
            segment_min_s=settings.get_float("longform.segment_min_s"),
            segment_max_s=settings.get_float("longform.segment_max_s"),
            lead_s=settings.get_float("longform.lead_s"),
            tail_s=settings.get_float("longform.tail_s"),
            max_per_creator=settings.get_int("longform.max_per_creator"),
            max_creator_s=settings.get_float("longform.max_creator_s"),
            reuse_after_days=settings.get_int("longform.reuse_after_days"),
            fetch_pad_s=settings.get_float("longform.fetch_pad_s"),
            whisper_model=settings.get_str("longform.whisper_model"),
            render=RenderSpec.from_value(settings.get_dict("longform.render")),
            priority=settings.get_int("longform.priority"),
            keep_days=settings.get_int("longform.keep_days"),
            cold_open_s=settings.get_float("longform.cold_open_s"),
            end_card_s=settings.get_float("longform.end_card_s"),
            account_id=settings.get_str("longform.account_id"),
            rank_weights=RankWeights.from_value(settings.get_dict("longform.rank_weights")),
            auto_build=settings.get_bool("longform.auto_build"),
            cut=cut_rules(settings.get_dict("longform.cut")),
            style=OverlayStyle.from_value(settings.get_dict("longform.style")),
            format_cycle=tuple(str(f) for f in settings.get_list("longform.format_cycle")),
            solo=SoloRules.from_value(settings.get_dict("longform.solo")),
            thumb=ThumbSpec.from_value(settings.get_dict("longform.thumb")),
            publish=PublishSpec.from_value(settings.get_dict("longform.publish")),
        )


def cut_rules(value: dict[str, Any]) -> CutRules:
    """Setting `longform.cut` (unknown keys ignored, missing keys keep their defaults)."""
    known = {k: v for k, v in value.items() if k in CutRules.__dataclass_fields__}
    return CutRules(**{**CutRules().__dict__, **known})

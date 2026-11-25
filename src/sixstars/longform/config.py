"""Typed bundle of the `longform.*` settings (migration 0058, docs/16 "Long-form episodes")."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sixstars import settings


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

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "RenderSpec":
        return cls(
            width=int(value.get("width", cls.width)),
            height=int(value.get("height", cls.height)),
            fps=int(value.get("fps", cls.fps)),
            crf=int(value.get("crf", cls.crf)),
            maxrate=str(value.get("maxrate", cls.maxrate)),
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
        )

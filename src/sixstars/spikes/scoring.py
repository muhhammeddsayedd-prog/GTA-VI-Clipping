"""Pure logic that turns bucket counts and baselines into a weighted z-score spike score and tier, with
weights read from settings. Implemented in M04.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from sixstars.spikes.baseline import Baseline
from sixstars.spikes.buckets import Bucket
from sixstars.spikes.config import FEATURES, SpikeConfig

Tier = Literal["S", "A", "B"]


@dataclass(frozen=True, slots=True)
class Scored:
    """A bucket with its baseline, its clamped z-score per feature and the combined score."""

    bucket: Bucket
    baseline: Baseline
    z: Mapping[str, float]
    score_z: float


def z_score(value: float, mean: float, std: float, z_min: float, z_max: float) -> float:
    """`(value - mean) / std`, clamped to `[z_min, z_max]`. A zero deviation gives 0 or the clamp limit."""
    diff = value - mean
    if std <= 0:
        return 0.0 if diff == 0 else (z_max if diff > 0 else z_min)
    return min(max(diff / std, z_min), z_max)


def z_scores(bucket: Bucket, baseline: Baseline, cfg: SpikeConfig) -> dict[str, float]:
    """The clamped z-score of every feature of `bucket` against `baseline`."""
    return {
        feature: z_score(
            bucket.value(feature), baseline.mean[feature], baseline.std[feature], cfg.z_min, cfg.z_max
        )
        for feature in FEATURES
    }


def combine(z: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """The spike score: the weighted sum of the feature z-scores."""
    return sum(float(weights.get(feature, 0.0)) * value for feature, value in z.items())


def score_bucket(bucket: Bucket, baseline: Baseline, cfg: SpikeConfig) -> Scored:
    """Score one bucket against its baseline."""
    z = z_scores(bucket, baseline, cfg)
    return Scored(bucket, baseline, z, combine(z, cfg.weights))


def is_candidate(scored: Scored, cfg: SpikeConfig, min_msgs: float) -> bool:
    """A spike candidate: score at least `z_b` and at least `min_msgs` messages (`spikes.min_messages`,
    raised in replay mode to twice the whole-file median)."""
    return scored.score_z >= cfg.z_b and scored.bucket.msgs >= min_msgs


def tier_for(score_z: float, uniq: int, mean_uniq: float, cfg: SpikeConfig) -> Tier:
    """Provisional tier from the numbers alone (Claude confirmation comes later, in M05)."""
    if score_z >= cfg.z_s and uniq >= cfg.s_unique_multiple * mean_uniq:
        return "S"
    return "A" if score_z >= cfg.z_a else "B"

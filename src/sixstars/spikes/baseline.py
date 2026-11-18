"""Pure logic for the trailing baseline (mean and standard deviation) of each chat signal over a stream's own
recent history. Implemented in M04.

The baseline of a bucket starting at `s` is made of the back-to-back buckets that lie inside
`[s - baseline_s, s - baseline_exclude_s)`: the newest 30 seconds are left out so that a rising spike does
not inflate its own baseline. Buckets that start before `coverage_start` (the warm-up period, or the time
before the reader connected) are never used.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean, pstdev
from typing import Final

from sixstars.spikes.buckets import Bucket
from sixstars.spikes.config import FEATURES, SpikeConfig

_EPS: Final = 1e-6


@dataclass(frozen=True, slots=True)
class Baseline:
    """Mean and floored standard deviation of every feature over `n` baseline buckets."""

    n: int
    mean: Mapping[str, float]
    std: Mapping[str, float]


def trailing_buckets(
    buckets: Sequence[Bucket],
    index: int,
    *,
    bucket_s: float,
    baseline_s: float,
    exclude_s: float,
    coverage_start: float,
) -> Sequence[Bucket]:
    """The baseline buckets of `buckets[index]`. `buckets` must be back-to-back buckets of `bucket_s`."""
    farthest = int(math.floor(baseline_s / bucket_s + _EPS))
    nearest = int(math.ceil((exclude_s + bucket_s) / bucket_s - _EPS))
    lo, hi = max(index - farthest, 0), index - nearest + 1
    if hi <= lo:
        return []
    return [bucket for bucket in buckets[lo:hi] if bucket.t >= coverage_start - _EPS]


def compute_baseline(buckets: Sequence[Bucket], min_std: Mapping[str, float]) -> Baseline:
    """Mean and population standard deviation per feature; each deviation is raised to its `min_std`."""
    mean: dict[str, float] = {}
    std: dict[str, float] = {}
    for feature in FEATURES:
        values = [bucket.value(feature) for bucket in buckets]
        mean[feature] = fmean(values) if values else 0.0
        std[feature] = max(pstdev(values) if values else 0.0, float(min_std.get(feature, 0.0)))
    return Baseline(len(buckets), mean, std)


def baseline_for(
    buckets: Sequence[Bucket], index: int, cfg: SpikeConfig, coverage_start: float
) -> Baseline | None:
    """The baseline of `buckets[index]`, or None when fewer than `min_baseline_buckets` buckets exist
    (start of a stream): no detection is possible then."""
    trailing = trailing_buckets(
        buckets,
        index,
        bucket_s=cfg.bucket_s,
        baseline_s=cfg.baseline_s,
        exclude_s=cfg.baseline_exclude_s,
        coverage_start=coverage_start,
    )
    if len(trailing) < cfg.min_baseline_buckets:
        return None
    return compute_baseline(trailing, cfg.min_std)


def to_json(baseline: Baseline, at: float) -> dict[str, object]:
    """A baseline as stored in `live_sessions.chat_baseline`."""
    return {
        "n": baseline.n,
        "at": round(at, 1),
        "mean": {key: round(value, 4) for key, value in baseline.mean.items()},
        "std": {key: round(value, 4) for key, value in baseline.std.items()},
    }


def median_baseline(stored: Sequence[object], min_std: Mapping[str, float]) -> Baseline | None:
    """The per-feature median of stored baselines (`to_json` objects; malformed ones are skipped), each
    deviation raised to its `min_std`. None when none is usable. Pure."""
    from statistics import median

    usable = [
        item
        for item in stored
        if isinstance(item, Mapping)
        and isinstance(item.get("mean"), Mapping)
        and isinstance(item.get("std"), Mapping)
    ]
    if not usable:
        return None
    mean: dict[str, float] = {}
    std: dict[str, float] = {}
    for feature in FEATURES:
        means = [float(m) for item in usable if isinstance(m := item["mean"].get(feature), int | float)]
        stds = [float(v) for item in usable if isinstance(v := item["std"].get(feature), int | float)]
        mean[feature] = float(median(means)) if means else 0.0
        std[feature] = max(float(median(stds)) if stds else 0.0, float(min_std.get(feature, 0.0)))
    return Baseline(len(usable), mean, std)

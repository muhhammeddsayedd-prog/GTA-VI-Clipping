"""Baselines, z-scores, the combined score and tiers."""

from dataclasses import replace

import pytest

from sixstars.spikes.baseline import Baseline, baseline_for, compute_baseline, trailing_buckets
from sixstars.spikes.buckets import Bucket
from sixstars.spikes.config import SpikeConfig
from sixstars.spikes.scoring import combine, is_candidate, score_bucket, tier_for, z_score, z_scores


def flat(count: int, msgs: int = 5, start: float = 0.0, step: float = 10.0) -> list[Bucket]:
    return [Bucket(t=start + i * step, msgs=msgs, uniq=msgs, reaction_count=0) for i in range(count)]


def starts(buckets: list[Bucket], index: int, coverage_start: float = 0.0) -> list[float]:
    chosen = trailing_buckets(
        buckets, index, bucket_s=10.0, baseline_s=600.0, exclude_s=30.0, coverage_start=coverage_start
    )
    return [bucket.t for bucket in chosen]


def test_baseline_is_the_trailing_600_seconds_without_the_newest_30() -> None:
    buckets = flat(200)
    chosen = starts(buckets, 100)  # the bucket starting at 1000
    assert chosen[0] == 400.0 and chosen[-1] == 960.0  # [400, 970): the 30 s before 1000 are left out
    assert len(chosen) == 57


@pytest.mark.parametrize(
    ("index", "coverage_start", "expected_count"),
    [
        (0, 0.0, 0),
        (3, 0.0, 0),  # only the excluded 30 s exist
        (4, 0.0, 1),
        (15, 0.0, 12),
        (100, 0.0, 57),
        (100, 300.0, 57),
        (45, 300.0, 12),  # warm-up buckets are never baseline: 300..410
        (44, 300.0, 11),
        (100, 900.0, 7),
    ],
)
def test_trailing_bucket_counts(index: int, coverage_start: float, expected_count: int) -> None:
    assert len(starts(flat(200), index, coverage_start)) == expected_count


def test_fewer_than_12_baseline_buckets_gives_no_baseline(cfg: SpikeConfig) -> None:
    buckets = flat(200)
    assert baseline_for(buckets, 44, cfg, 300.0) is None
    baseline = baseline_for(buckets, 45, cfg, 300.0)
    assert baseline is not None and baseline.n == 12


def test_baseline_mean_and_floored_std(cfg: SpikeConfig) -> None:
    buckets = [Bucket(t=i * 10.0, msgs=m, uniq=m, reaction_count=m // 2) for i, m in enumerate([2, 4, 6, 8])]
    baseline = compute_baseline(buckets, cfg.min_std)
    assert baseline.mean["msgs"] == 5.0 and baseline.mean["react"] == pytest.approx(0.5)
    assert baseline.std["msgs"] == pytest.approx(5**0.5)  # population std, above the 2.0 floor
    assert baseline.std["clipreq"] == 0.5 and baseline.std["audio"] == 1.5  # floors
    quiet = compute_baseline(flat(20), cfg.min_std)
    assert (quiet.std["msgs"], quiet.std["uniq"], quiet.std["react"]) == (2.0, 1.5, 0.05)


@pytest.mark.parametrize(
    ("value", "mean", "std", "expected"),
    [
        (9.0, 5.0, 2.0, 2.0),
        (5.0, 5.0, 2.0, 0.0),
        (1.0, 5.0, 2.0, -2.0),
        (500.0, 5.0, 2.0, 10.0),  # clamped high
        (0.0, 50.0, 2.0, -3.0),  # clamped low
        (5.0, 5.0, 0.0, 0.0),
        (6.0, 5.0, 0.0, 10.0),
        (4.0, 5.0, 0.0, -3.0),
    ],
)
def test_z_score_is_clamped(value: float, mean: float, std: float, expected: float) -> None:
    assert z_score(value, mean, std, -3.0, 10.0) == pytest.approx(expected)


def test_score_is_the_weighted_sum_of_z_scores(cfg: SpikeConfig) -> None:
    baseline = Baseline(
        20,
        mean={"msgs": 5.0, "uniq": 4.0, "react": 0.1, "clipreq": 0.0, "audio": 0.0},
        std={"msgs": 2.0, "uniq": 2.0, "react": 0.1, "clipreq": 0.5, "audio": 1.5},
    )
    bucket = Bucket(t=1000.0, msgs=13, uniq=10, reaction_count=13 * 3 // 10, clipreq=1, audio=3.0)
    z = z_scores(bucket, baseline, cfg)
    assert z == pytest.approx(
        {"msgs": 4.0, "uniq": 3.0, "react": (3 / 13 - 0.1) / 0.1, "clipreq": 2.0, "audio": 2.0}
    )
    scored = score_bucket(bucket, baseline, cfg)
    expected = 0.4 * 4 + 0.25 * 3 + 0.2 * z["react"] + 0.1 * 2 + 0.05 * 2
    assert scored.score_z == pytest.approx(expected)
    assert combine({"msgs": 1.0, "unknown": 9.0}, cfg.weights) == pytest.approx(0.4)


def test_weights_come_from_the_config(cfg: SpikeConfig) -> None:
    only_msgs = replace(cfg, weights={"msgs": 1.0})
    baseline = compute_baseline(flat(20), cfg.min_std)
    assert score_bucket(Bucket(t=0.0, msgs=9, uniq=9), baseline, only_msgs).score_z == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("score", "uniq", "mean_uniq", "expected"),
    [
        (4.0, 30, 10.0, "S"),
        (9.5, 30, 10.0, "S"),
        (9.5, 29, 10.0, "A"),  # unique chatters below 3x the baseline: not S
        (3.99, 300, 10.0, "A"),
        (3.0, 5, 10.0, "A"),
        (2.99, 300, 10.0, "B"),
        (2.5, 5, 10.0, "B"),
    ],
)
def test_tier_table(cfg: SpikeConfig, score: float, uniq: int, mean_uniq: float, expected: str) -> None:
    assert tier_for(score, uniq, mean_uniq, cfg) == expected


def test_candidate_needs_the_score_and_the_message_floor(cfg: SpikeConfig) -> None:
    baseline = compute_baseline(flat(20, msgs=0), cfg.min_std)
    strong = score_bucket(Bucket(t=0.0, msgs=40, uniq=30, reaction_count=40), baseline, cfg)
    small = score_bucket(Bucket(t=0.0, msgs=14, uniq=14, reaction_count=14), baseline, cfg)
    assert strong.score_z >= cfg.z_b and small.score_z >= cfg.z_b
    assert is_candidate(strong, cfg, cfg.min_messages)
    assert not is_candidate(small, cfg, cfg.min_messages)  # 14 < spikes.min_messages
    assert not is_candidate(strong, cfg, 41.0)  # replay: below twice the whole-file median
    calm = score_bucket(Bucket(t=0.0, msgs=20, uniq=1), compute_baseline(flat(20, msgs=20), cfg.min_std), cfg)
    assert not is_candidate(calm, cfg, cfg.min_messages)

"""M20 L2: pure moment selection (rank, variety, fill, countdown order)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sixstars.longform.config import RankWeights
from sixstars.longform.selector import Candidate, Limits, estimate_seconds, rank_value, select

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
LIMITS = Limits(
    target_s=120,
    min_s=90,
    max_s=150,
    segment_min_s=15,
    segment_max_s=45,
    lead_s=3,
    tail_s=2,
    max_per_creator=2,
    max_creator_s=60,
    pool_hours=48,
)
W = RankWeights()


def cand(
    i: int,
    *,
    creator: str | None = None,
    score: float = 7.0,
    clip_s: float = 20.0,
    window_s: float | None = 25.0,
    z: float | None = 2.0,
    views: int | None = None,
    topic: str | None = None,
    age_h: float = 2.0,
) -> Candidate:
    return Candidate(
        clip_id=f"c{i:02d}",
        creator_id=creator or f"cr{i:02d}",
        creator_name=creator or f"creator{i}",
        title=f"moment {i}",
        score=score,
        clip_s=clip_s,
        window_s=window_s,
        score_z=z,
        views=views,
        topic=topic,
        created_at=NOW - timedelta(hours=age_h),
    )


def test_estimate_uses_the_longer_length_plus_lead_and_tail_clamped() -> None:
    assert estimate_seconds(cand(1, clip_s=18, window_s=30), LIMITS) == 35
    assert estimate_seconds(cand(2, clip_s=5, window_s=None), LIMITS) == 15
    assert estimate_seconds(cand(3, clip_s=50, window_s=60), LIMITS) == 45


def test_rank_prefers_score_then_spike_views_and_freshness() -> None:
    hi = rank_value(cand(1, score=9), NOW, 0, LIMITS, W)
    lo = rank_value(cand(2, score=7), NOW, 0, LIMITS, W)
    assert hi > lo
    viewed = rank_value(cand(3, views=10_000), NOW, 10_000, LIMITS, W)
    plain = rank_value(cand(4), NOW, 10_000, LIMITS, W)
    assert viewed > plain
    fresh = rank_value(cand(5, age_h=1), NOW, 0, LIMITS, W)
    stale = rank_value(cand(6, age_h=47), NOW, 0, LIMITS, W)
    assert fresh > stale


def test_fills_to_target_and_respects_max() -> None:
    pool = [cand(i) for i in range(10)]  # 30 s each
    sel = select(pool, LIMITS, W, NOW)
    assert not sel.skipped
    assert sel.total_s == 120
    assert len(sel.picks) == 4


def test_skips_the_day_when_the_pool_is_too_thin() -> None:
    sel = select([cand(1), cand(2)], LIMITS, W, NOW)
    assert sel.skipped and sel.picks == ()
    assert sel.skipped_reason is not None and "too thin" in sel.skipped_reason


def test_per_creator_count_and_seconds_caps() -> None:
    pool = [cand(i, creator="same", score=9) for i in range(5)] + [cand(10 + i) for i in range(5)]
    sel = select(pool, LIMITS, W, NOW)
    same = [p for p in sel.picks if p.candidate.creator_id == "same"]
    assert len(same) == 2  # max_per_creator, and 2 x 30 s = max_creator_s
    assert sum(p.est_s for p in same) <= LIMITS.max_creator_s


def test_topic_is_used_once() -> None:
    pool = [cand(i, topic="heist-fail", score=9) for i in range(3)] + [cand(10 + i) for i in range(5)]
    sel = select(pool, LIMITS, W, NOW)
    assert sum(1 for p in sel.picks if p.candidate.topic == "heist-fail") == 1


def test_countdown_best_plays_last_and_positions_run_n_to_1() -> None:
    pool = [cand(i, score=5 + i * 0.4) for i in range(6)]
    sel = select(pool, LIMITS, W, NOW)
    positions = [p.position for p in sel.picks]
    assert positions == list(range(len(positions), 0, -1))
    best = max(sel.picks, key=lambda p: p.rank)
    assert sel.picks[-1] is best and best.position == 1


def test_no_creator_plays_twice_in_a_row_when_avoidable() -> None:
    limits = replace(LIMITS, max_per_creator=3, max_creator_s=200, target_s=240, max_s=300)
    pool = [cand(i, creator="a", score=9.5 - i * 0.01) for i in range(3)]
    pool += [cand(10 + i, score=7) for i in range(6)]
    sel = select(pool, limits, W, NOW)
    order = [p.candidate.creator_id for p in sel.picks]
    assert all(x != y for x, y in zip(order, order[1:], strict=False)), order

"""AI cost plan A5b: the queue throttle's pure rules."""

import pytest

from sixstars.factory.throttle import QueuedClip, decide, postable_count, pre_rank


def queue(count: int, *, remaining_h: float = 72.0, rank: float = 50.0) -> list[QueuedClip]:
    return [
        QueuedClip(rank_score=float(count - i), remaining_h=remaining_h, pre_rank=rank) for i in range(count)
    ]


def test_postable_count_stops_where_the_pace_cannot_reach_a_clip_in_time() -> None:
    # one post an hour; the clips have 2.5, 2.5, 2.5, 2.5 hours left: positions 0, 1, 2 post in time
    assert postable_count([2.5, 2.5, 2.5, 2.5], posts_per_h=1.0) == 3
    assert postable_count([72.0] * 100, posts_per_h=1.0) == 72
    assert postable_count([72.0] * 10, posts_per_h=0.0) == 0


def test_pre_rank_uses_the_posting_bonuses_and_freshness() -> None:
    assert pre_rank(None, "A", 0.0, 6.0) is None
    assert pre_rank(80.0, "B", 0.0, 6.0) == pytest.approx(80.0)
    assert pre_rank(80.0, "A", 0.0, 6.0) == pytest.approx(80.0 * 1.15)
    assert pre_rank(80.0, "B", 6.0, 6.0) == pytest.approx(40.0)  # one half-life old


def test_a_weak_candidate_against_a_long_queue_would_be_skipped() -> None:
    decision = decide(30.0, queue(60), posts_per_h=[1.0], queue_min=40)
    assert decision.would_skip and decision.effective_queue == 60 and decision.queue_median == 50.0


def test_a_strong_candidate_is_kept() -> None:
    assert not decide(70.0, queue(60), posts_per_h=[1.0], queue_min=40).would_skip


def test_clips_that_will_expire_before_their_turn_do_not_count() -> None:
    # 60 queued, but each has 10 h left and the busiest account posts 2 an hour: only 20 can post
    decision = decide(30.0, queue(60, remaining_h=10.0), posts_per_h=[1.0, 2.0], queue_min=40)
    assert not decision.would_skip and decision.effective_queue == 20 and decision.why == "queue short"


def test_without_a_percentile_or_enough_ranked_clips_nothing_is_skipped() -> None:
    assert decide(None, queue(60), [1.0], 40).why == "no percentile"
    unranked = queue(60, rank=50.0)[:3] + [QueuedClip(1.0, 72.0, None)] * 57
    assert decide(30.0, unranked, [1.0], 40).why == "too few ranked clips in the queue"


def test_no_posting_account_means_no_effective_queue() -> None:
    decision = decide(30.0, queue(60), posts_per_h=[], queue_min=40)
    assert decision.effective_queue == 0 and not decision.would_skip

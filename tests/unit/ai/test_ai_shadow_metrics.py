"""AI cost plan B1: the shadow comparison metrics."""

import pytest

from sixstars.ai.shadow_metrics import (
    VisualPair,
    ranks,
    shadow_visual_score,
    spearman,
    visual_report,
    writer_report,
)


def test_ranks_share_ties() -> None:
    assert ranks([3, 1, 3, 2]) == [3.5, 1.0, 3.5, 2.0]


def test_spearman() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2], [1, 2]) is None
    assert spearman([1, 1, 1], [1, 2, 3]) is None


def test_the_shadow_visual_score_is_bounded_like_production() -> None:
    reply = {"seen": "x", "score_parts": {"hook": 9, "clarity": 9, "payoff": 5, "rewatch": 9}, "score": 9}
    assert shadow_visual_score(reply) == 6  # never more than one above payoff
    assert shadow_visual_score({"score": "high"}) is None
    assert shadow_visual_score(None) is None


def test_visual_report_counts_agreement_at_each_pass_line() -> None:
    pairs = [
        VisualPair(7, 7, 7),
        VisualPair(5, 5, 6),
        VisualPair(8, 6, 7),  # production passes, shadow fails
        VisualPair(5, 7, 6),  # shadow passes, production fails
        VisualPair(6, None, 6),  # no usable answer
    ]
    report = visual_report(pairs)
    assert (report.pairs, report.usable) == (5, 4)
    assert report.agreement == 0.5
    assert (report.shadow_passes_primary_fails, report.shadow_fails_primary_passes) == (1, 1)
    assert not report.meets_criteria


def test_writer_report() -> None:
    primary = {"start_s": 10.0, "end_s": 30.0, "safety": {"sexual": True, "hate": False}, "spoiler": True}
    close = {"start_s": 10.6, "end_s": 29.2, "safety": {"sexual": True}, "spoiler": True}
    far = {"start_s": 14.0, "end_s": 30.0, "safety": {"sexual": False}, "spoiler": False, "reject": True}
    report = writer_report([(primary, close), (primary, far), (primary, None)])
    assert report.pairs == 3 and report.valid_rate == pytest.approx(2 / 3)
    assert report.trim_within_1s == 0.5
    assert report.flag_recall == 0.5 and report.missed_flags == 2
    assert report.reject_agreement == 0.5
    assert not report.meets_criteria

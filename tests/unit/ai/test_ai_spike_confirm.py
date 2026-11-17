"""Tier rules applied to a P1 verdict (M05 build step 5)."""

import pytest
from ai_samples import verdict_reply

from sixstars.ai.schemas import SpikeVerdict
from sixstars.ai.spike_confirm import Thresholds, Tier, decide

THRESHOLDS = Thresholds(s_min_confidence=0.8, ab_min_confidence=0.6)


def verdict(confidence: float, is_moment: bool = True) -> SpikeVerdict:
    return SpikeVerdict.model_validate(
        verdict_reply(confidence=confidence, is_moment=is_moment, noise_reason=None if is_moment else "raid")
    )


@pytest.mark.parametrize(
    ("provisional", "confidence", "tier", "status"),
    [
        ("S", 0.95, "S", "confirmed"),
        ("S", 0.8, "S", "confirmed"),
        ("S", 0.79, "A", "confirmed"),
        ("S", 0.6, "A", "confirmed"),
        ("S", 0.59, "rejected", "rejected"),
        ("A", 0.9, "A", "confirmed"),
        ("A", 0.6, "A", "confirmed"),
        ("A", 0.59, "rejected", "rejected"),
        ("B", 0.7, "B", "confirmed"),
        ("B", 0.2, "rejected", "rejected"),
    ],
)
def test_tier_rules(provisional: Tier, confidence: float, tier: str, status: str) -> None:
    decision = decide(provisional, verdict(confidence), THRESHOLDS)
    assert (decision.tier, decision.status, decision.confidence) == (tier, status, confidence)


@pytest.mark.parametrize("provisional", ["S", "A", "B"])
def test_not_a_moment_is_rejected_whatever_the_confidence(provisional: Tier) -> None:
    decision = decide(provisional, verdict(0.99, is_moment=False), THRESHOLDS)
    assert (decision.tier, decision.status, decision.reason) == ("rejected", "rejected", "raid")


def test_thresholds_come_from_settings(ai_settings: dict[str, object]) -> None:
    ai_settings["spikes.s_min_confidence"] = 0.9
    ai_settings["spikes.ab_min_confidence"] = 0.5
    assert Thresholds.from_settings() == Thresholds(0.9, 0.5)
    assert decide("S", verdict(0.85), Thresholds.from_settings()).tier == "A"


def test_reason_describes_the_moment() -> None:
    assert decide("A", verdict(0.9), THRESHOLDS).reason == "funny: Streamer jumps a bike onto a police car"
    assert decide("A", verdict(0.3), THRESHOLDS).reason.startswith("low confidence (0.30)")

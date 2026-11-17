"""Applies a P1 verdict to a detected spike: the tier rules of M05 build step 5 and the update of the
`spikes` row. Implemented in M05.

Rules: `is_moment` false rejects the spike. A provisional S needs confidence of at least
`spikes.s_min_confidence`, otherwise it becomes A (and must then pass the A/B rule). A and B need at
least `spikes.ab_min_confidence`, otherwise the spike is rejected.

`decide` is pure; `apply` writes the result (tier, confidence, reason, status) and an event.
"""

from dataclasses import dataclass
from typing import Literal

from sixstars import db, events, settings
from sixstars.ai.schemas import SpikeVerdict

Tier = Literal["S", "A", "B"]
FinalTier = Literal["S", "A", "B", "rejected"]
Status = Literal["confirmed", "rejected"]


@dataclass(frozen=True, slots=True)
class Thresholds:
    s_min_confidence: float
    ab_min_confidence: float

    @classmethod
    def from_settings(cls) -> "Thresholds":
        return cls(
            s_min_confidence=settings.get_float("spikes.s_min_confidence"),
            ab_min_confidence=settings.get_float("spikes.ab_min_confidence"),
        )


@dataclass(frozen=True, slots=True)
class TierDecision:
    tier: FinalTier
    status: Status
    confidence: float
    reason: str


def decide(provisional: Tier, verdict: SpikeVerdict, thresholds: Thresholds) -> TierDecision:
    """Final tier of a spike from its provisional tier and the P1 verdict."""
    confidence = verdict.confidence
    if not verdict.is_moment:
        reason = verdict.noise_reason or verdict.one_line or "not a moment"
        return TierDecision("rejected", "rejected", confidence, reason)
    tier: Tier = provisional
    if tier == "S" and confidence < thresholds.s_min_confidence:
        tier = "A"
    if tier != "S" and confidence < thresholds.ab_min_confidence:
        reason = f"low confidence ({confidence:.2f}): {verdict.one_line}"
        return TierDecision("rejected", "rejected", confidence, reason)
    return TierDecision(tier, "confirmed", confidence, f"{verdict.moment_type}: {verdict.one_line}")


def apply(spike_id: str, provisional: Tier, verdict: SpikeVerdict) -> TierDecision:
    """Decide with the thresholds from settings, update the `spikes` row and record an event."""
    decision = decide(provisional, verdict, Thresholds.from_settings())
    db.execute(
        """
        update spikes
        set tier = %s, claude_confidence = %s, claude_reason = %s, status = %s
        where id = %s
        """,
        (decision.tier, decision.confidence, decision.reason, decision.status, spike_id),
    )
    events.emit(
        f"spike.{decision.status}",
        entity_type="spike",
        entity_id=spike_id,
        message=decision.reason,
        data={
            "provisional_tier": provisional,
            "tier": decision.tier,
            "confidence": decision.confidence,
            "moment_type": verdict.moment_type,
        },
    )
    return decision

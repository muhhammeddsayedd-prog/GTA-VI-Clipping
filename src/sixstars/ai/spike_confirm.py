"""Applies a P1 verdict to a detected spike: the tier rules of M05 build step 5 and the update of the
`spikes` row. Implemented in M05.

Rules: `is_moment` false rejects the spike. A provisional S needs confidence of at least
`spikes.s_min_confidence`, otherwise it becomes A (and must then pass the A/B rule). A and B need at
least `spikes.ab_min_confidence`, otherwise the spike is rejected.

`decide` is pure; `apply` writes the result (tier, confidence, reason, status) and an event.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sixstars import db, events, settings
from sixstars.ai.schemas import SpikeVerdict
from sixstars.scoring import log as scoring_log

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


def non_content(verdict: SpikeVerdict, keep_reject_terms: Sequence[str]) -> str | None:
    """The term that marks a P1 reject as a non-content spike (giveaway, raid, ...), or None. Pure."""
    text = f"{verdict.noise_reason or ''} {verdict.one_line}".lower()
    return next((term for term in keep_reject_terms if term and term.lower() in text), None)


def decide_fast(
    verdict: SpikeVerdict, normal: TierDecision, keep_reject_terms: Sequence[str], why: str
) -> TierDecision:
    """`fastpath.live`: a strong live spike is clipped as tier S whatever the chat-only P1 check says,
    unless P1 rejected it as a non-content spike. Pure."""
    if normal.status == "rejected" and non_content(verdict, keep_reject_terms) is not None:
        return normal
    reason = f"fast path ({why}): {verdict.moment_type}: {verdict.one_line}"
    if normal.status == "rejected":
        reason = f"fast path ({why}) over P1 reject [{normal.reason}]"
    return TierDecision("S", "confirmed", verdict.confidence, reason[:500])


def apply(
    spike_id: str,
    provisional: Tier,
    verdict: SpikeVerdict,
    *,
    fast: tuple[Sequence[str], str] | None = None,
    sub_scores: Mapping[str, Any] | None = None,
    creator_id: str | None = None,
) -> TierDecision:
    """Decide with the thresholds from settings, update the `spikes` row and record an event.

    `fast` is `(keep_reject_terms, why)` when the spike takes the live fast path (`decide_fast`).
    The decision and `sub_scores` go to `scoring_log` (stage `spike.confirm`)."""
    normal = decide(provisional, verdict, Thresholds.from_settings())
    decision = normal if fast is None else decide_fast(verdict, normal, fast[0], fast[1])
    scoring_log.record(
        "spike.confirm",
        decision=decision.status,
        reason=decision.reason,
        spike_id=spike_id,
        creator_id=creator_id,
        sub_scores={
            **dict(sub_scores or {}),
            "provisional_tier": provisional,
            "tier": decision.tier,
            "p1_is_moment": verdict.is_moment,
            "p1_confidence": verdict.confidence,
            "p1_moment_type": verdict.moment_type,
            "p1_status": normal.status,
            "fast_path": fast is not None,
            "fast_path_overrode_reject": fast is not None
            and normal.status == "rejected"
            and decision.status == "confirmed",
        },
    )
    scoring_log.stamp("spikes", spike_id)
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

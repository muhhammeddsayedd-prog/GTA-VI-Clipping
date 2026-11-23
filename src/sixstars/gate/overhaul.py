"""The gate's part of the scoring overhaul (docs/CHANGELOG-scoring.md). Each rule reads its flag from
`scoring_config` and does nothing while the flag is off.

- `cut.adaptive`: the minimum clip length of both profiles is lowered to the flag's `min_s` (short clips
  win, docs/taste-profile.md section 1). The maximum is not touched, so clips cut before the flag still pass.
- `pass.strong_spike`: a clip whose spike is in the channel's top (100 - percentile)% passes the hourly
  profile at the flag's `min_score`.
- `boost.first_time`: score and rank points for "first time" moments.

Hard safety rules (safety flags, spoilers, music, black or frozen video, duplicates) are never touched.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from sixstars import db
from sixstars.gate import store
from sixstars.gate.models import GateDecision, GateSettings
from sixstars.logging import get_logger
from sixstars.scoring import config as scoring_config
from sixstars.scoring import firsttime
from sixstars.scoring import log as scoring_log
from sixstars.scoring.percentile import Strength, strength_of_spike

_log = get_logger("gate")


@dataclass(slots=True)
class Adjustments:
    """What the overhaul changed for one clip, for `scoring_log` and `gate_result.scoring`."""

    min_len_s: float | None = None
    strength: Strength | None = None
    hourly_min_score: float | None = None
    boost: firsttime.Boost | None = None
    ai_score: float | None = None
    rank_bonus: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_version": scoring_config.version(),
            "min_len_s": self.min_len_s,
            "percentile": None if self.strength is None else self.strength.percentile,
            "percentile_basis": None if self.strength is None else self.strength.basis,
            "hourly_min_score": self.hourly_min_score,
            "ai_score": self.ai_score,
            "first_time": None if self.boost is None else self.boost.as_dict(),
            "rank_bonus": self.rank_bonus,
            "notes": list(self.notes),
        }


def lowered_lengths(settings: GateSettings, min_s: float) -> GateSettings:
    """Both profiles with their minimum length lowered to `min_s` (never raised). Pure."""
    return replace(
        settings,
        hourly=replace(settings.hourly, min_len_s=min(settings.hourly.min_len_s, min_s)),
        express=replace(settings.express, min_len_s=min(settings.express.min_len_s, min_s)),
    )


def lowered_hourly_score(settings: GateSettings, min_score: float) -> GateSettings:
    """The hourly profile's minimum score lowered to `min_score` (never raised). Pure."""
    return replace(
        settings, hourly=replace(settings.hourly, min_score=min(settings.hourly.min_score, min_score))
    )


def adjust_lengths(settings: GateSettings, notes: Adjustments | None = None) -> GateSettings:
    """The length limits under `cut.adaptive` (both minimums lowered to its `min_s`). Also used by the
    checks before the clip writer (`factory.precheck`), so both read the same limits."""
    cut = scoring_config.flag("cut.adaptive")
    if cut is None:
        return settings
    min_s = scoring_config.number(cut, "min_s", settings.hourly.min_len_s)
    if notes is not None:
        notes.min_len_s = min_s
    return lowered_lengths(settings, min_s)


def adjust_settings(row: store.ClipRow, settings: GateSettings, notes: Adjustments) -> GateSettings:
    """The gate settings for this clip under the enabled flags."""
    settings = adjust_lengths(settings, notes)
    strong = scoring_config.flag("pass.strong_spike")
    if strong is not None:
        notes.strength = strength_of_spike(row.spike_id)
        if notes.strength.at_least(scoring_config.number(strong, "percentile", 90.0)):
            min_score = scoring_config.number(strong, "min_score", settings.hourly.min_score)
            settings = lowered_hourly_score(settings, min_score)
            notes.hourly_min_score = settings.hourly.min_score
            notes.notes.append("strong spike: lower hourly min score")
    return settings


def _chat_texts(spike_id: str | None) -> list[str]:
    if not spike_id:
        return []
    try:
        row = db.fetch_one("select chat_sample from spikes where id = %s", (spike_id,))
    except Exception as exc:  # noqa: BLE001
        _log.warning("gate_chat_sample_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return []
    sample = row["chat_sample"] if row is not None else None
    if not isinstance(sample, list):
        return []
    return [str(entry.get("text", "")) for entry in sample if isinstance(entry, Mapping)]


def boost_row(row: store.ClipRow, notes: Adjustments) -> store.ClipRow:
    """The row with the first-time score points added to its score (the stored `clips.score` keeps the AI
    score). Unchanged when the flag is off, the clip has no score, or nothing was found."""
    score = row.raw.get("score")
    notes.ai_score = None if score is None else float(score)
    flag = scoring_config.flag("boost.first_time")
    if flag is None or score is None:
        return row
    boost = firsttime.for_clip(
        flag,
        creator_id=row.creator_id,
        clip_id=row.id,
        transcript_text=store.transcript_text(row.raw.get("transcript")),
        chat_texts=_chat_texts(row.spike_id),
        topic=str(row.raw.get("topic") or ""),
    )
    notes.boost = boost
    if not boost.applies:
        return row
    notes.rank_bonus = boost.rank_points
    notes.notes.append("first-time boost")
    return replace(row, raw={**row.raw, "score": float(score) + boost.score_points})


def record(row: store.ClipRow, decision: GateDecision, notes: Adjustments) -> None:
    """Log the gate decision with every sub-score and stamp the clip with the config version."""
    sub = notes.as_dict()
    sub.update(
        {
            "verdict": decision.verdict.value,
            "profile": decision.profile,
            "lane": decision.lane,
            "codes": [code.value for code in decision.codes],
            "demoted": decision.demoted,
            "score_used": row.raw.get("score"),
            "duration_s": row.duration_s,
            "score_parts": row.raw.get("score_parts"),
        }
    )
    scoring_log.record(
        "gate",
        decision=decision.verdict.value,
        reason=decision.reject_reason,
        sub_scores=sub,
        spike_id=row.spike_id,
        clip_id=row.id,
        creator_id=row.creator_id,
    )
    scoring_log.stamp("clips", row.id)

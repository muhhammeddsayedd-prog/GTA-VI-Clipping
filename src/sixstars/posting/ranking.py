"""Pure ranking of approved clips (docs/11 §5): freshness decay, spike and creator bonuses, expiry."""

from datetime import datetime, timedelta

from sixstars.posting.models import ensure_utc
from sixstars.posting.params import PostingParams

# docs/11 §5 multipliers; they have no settings key in docs/16.
SPIKE_BONUS = {"S": 1.3, "A": 1.15}
PREFERRED_CREATOR_BONUS = 1.1


def half_life_h(source_type: str, params: PostingParams) -> float:
    """`posting.freshness_half_life_h` for a 'live' or 'vod' clip."""
    if source_type == "live":
        return params.freshness_half_life_live_h
    if source_type == "vod":
        return params.freshness_half_life_vod_h
    raise ValueError(f"unknown source type {source_type!r}")


def freshness(age_h: float, half_life: float) -> float:
    """`0.5 ** (age_h / half_life)`; a negative age (clock skew) counts as zero."""
    return float(0.5 ** (max(0.0, age_h) / half_life))


RANK_USES_VISUAL_SETTING = "posting.rank_uses_visual_score"


def rank_input_score(score: float, score_parts: object, use_visual: bool) -> float:
    """The score the posting rank starts from. When the stored score came from the visual judge (P9V) and
    `use_visual` is off, the clip writer's score takes its place, keeping any points added on top of the
    stored score (the first-time boost). The P9V score did not predict views (AI cost plan A2, D117). Pure."""
    if use_visual or not isinstance(score_parts, dict) or score_parts.get("source") != "visual":
        return score
    writer = score_parts.get("writer")
    visual = score_parts.get("visual")
    if not isinstance(writer, dict) or not isinstance(visual, dict):
        return score
    writer_score, visual_score = writer.get("score"), visual.get("score")
    if not isinstance(writer_score, int | float) or not isinstance(visual_score, int | float):
        return score
    return score - float(visual_score) + float(writer_score)


def rank_score(
    score: float,
    source_type: str,
    tier: str | None,
    preferred_creator: bool,
    reference_at: datetime,
    now: datetime,
    params: PostingParams,
) -> float:
    """`score x freshness x spike bonus x preferred-creator bonus`.

    `reference_at` is the approval time, or the spike time (`spike_wall`) for live clips.
    """
    age_h = (ensure_utc(now, "now") - ensure_utc(reference_at, "reference_at")).total_seconds() / 3600
    bonus = SPIKE_BONUS.get(tier or "", 1.0)
    preferred = PREFERRED_CREATOR_BONUS if preferred_creator else 1.0
    return score * freshness(age_h, half_life_h(source_type, params)) * bonus * preferred


def is_expired(approved_at: datetime, now: datetime, params: PostingParams) -> bool:
    """True when an approved clip is older than `posting.max_clip_age_h` (exactly that old is still fine)."""
    age = ensure_utc(now, "now") - ensure_utc(approved_at, "approved_at")
    return age > timedelta(hours=params.max_clip_age_h)

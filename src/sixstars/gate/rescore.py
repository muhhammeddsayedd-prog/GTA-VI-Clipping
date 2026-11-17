"""The visual score: before the gate decides, the rendered clip is scored again by a model that sees it.

The clip writer (P2) scores from the transcript and the chat sample, before anything is rendered. In game
clips the payoff is usually on screen (a fall, a crash, a "mission failed" screen) and not in the words, so
the writer's score reads low for exactly the clips worth posting. Here the finished file is scored with
prompt P9V (`ai.calls.score_clip_visual`): frames of the clip, the hook, the subtitle lines and the chat
reaction, on the same four scales and bands.

    wanted(row)            is the visual score switched on, and does this clip still need one?
    run(row, ...)          extract the frames, score, store; returns the clip row to judge and the frames

`clips.score` becomes the visual score; `clips.score_parts` keeps both (`score_document`). The frames
include the four P5 positions, so the gate's visual check reuses them (`visual.p5_frames`): one extraction
for both calls.

The score can only be replaced, never invented: when the call or the frame extraction fails, the clip
keeps the writer's score and the gate judges it on that (event `clip.visual_score_failed`). The one
exception is a used-up daily AI budget on the VOD lane (`BudgetExceededError`), which is raised so that
the gate defers the clip to the next budget day, as it does for P5. Switches: `ai.visual_score`,
`ai.visual_score_model`, `ai.visual_score_frames`, `ai.visual_score_samples`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sixstars import events, settings
from sixstars.ai import calls
from sixstars.ai.schemas import ScoreParts, VisualClipScore
from sixstars.clients.anthropic_client import AnthropicClient, BudgetKind
from sixstars.factory import lines as subtitle_lines
from sixstars.gate import store
from sixstars.gate import visual as visuals
from sixstars.gate.models import EXPRESS
from sixstars.logging import get_logger

# The top of the score scale (`ai.schemas.ScoreParts`): "would this clip pass if its score were perfect?"
TOP_SCORE = 10
VISUAL = "visual"
WRITER = "writer"
# A chat peak this far outside the clip is not a position in it (live clips have no offset at all).
PEAK_BEFORE_S = 5.0
PEAK_AFTER_S = 30.0

Shots = list[tuple[float, bytes]]


def enabled() -> bool:
    """`ai.visual_score`; off for a database that does not have the setting yet (before migration 0016)."""
    return settings.get_bool("ai.visual_score", False)


def already_scored(row: store.ClipRow) -> bool:
    parts = row.raw.get("score_parts")
    return isinstance(parts, Mapping) and isinstance(parts.get(VISUAL), Mapping)


def wanted(row: store.ClipRow) -> bool:
    """True when the clip has a file to look at and no visual score yet (a gate that runs again after
    a failed P5 does not pay for the score twice)."""
    return bool(row.local_path) and bool(row.duration_s) and not already_scored(row) and enabled()


def with_top_score(row: store.ClipRow) -> store.ClipRow:
    """The row as it would be with a perfect score, for the question "can the score still matter?"."""
    return replace(row, raw={**row.raw, "score": TOP_SCORE})


def _count(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _score(row: store.ClipRow) -> float | None:
    value = row.raw.get("score")
    return None if value is None else float(value)


def peak_in_clip(spike: Mapping[str, Any], start_s: float, duration_s: float) -> float | None:
    """Where the chat peak falls in clip time, when the spike has a position on the source's clock
    (replay spikes) and it lies in or near the clip."""
    offset = spike.get("spike_offset_s")
    if offset is None:
        return None
    peak = float(offset) - start_s
    return peak if -PEAK_BEFORE_S <= peak <= duration_s + PEAK_AFTER_S else None


def chat_reaction(
    spike: Mapping[str, Any] | None, start_s: float, duration_s: float
) -> calls.ChatReaction | None:
    """The spike row as the scorer sees it: the counts of the peak window and the stored chat sample."""
    if spike is None:
        return None
    sample = spike.get("chat_sample")
    lines = [
        calls.ChatLine(float(item["t_rel"]), str(item.get("text") or ""))
        for item in (sample if isinstance(sample, list) else [])
        if isinstance(item, Mapping) and isinstance(item.get("t_rel"), int | float)
    ]
    tier = spike.get("tier")
    return calls.ChatReaction(
        msgs=_count(spike.get("msgs_in_bucket")),
        uniq=_count(spike.get("unique_chatters")),
        reactions=_count(spike.get("reaction_count")),
        tier=str(tier) if tier else None,
        peak_s=peak_in_clip(spike, start_s, duration_s),
        lines=lines,
    )


def audience_evidence(viewer_clip: Mapping[str, Any] | None) -> calls.AudienceEvidence | None:
    """The `viewer_clips` row as the scorer sees it: the Twitch clip's views, its age when discovery read
    them, and the title its maker gave it. None when the clip did not come from a viewer's clip."""
    if viewer_clip is None:
        return None
    made, read = viewer_clip.get("clip_created_at"), viewer_clip.get("created_at")
    age_h: float | None = None
    if isinstance(made, datetime) and isinstance(read, datetime):
        age_h = max(0.0, (read - made).total_seconds() / 3600.0)
    return calls.AudienceEvidence(
        views=_count(viewer_clip.get("view_count")),
        age_h=age_h,
        title=str(viewer_clip.get("title") or ""),
    )


def viewer_audience(row: store.ClipRow) -> calls.AudienceEvidence | None:
    """The audience evidence of a clip rendered from a viewer-made clip, None for every other clip. It is
    optional context: when it cannot be read the clip is scored without it."""
    try:
        return audience_evidence(store.audience_row(row.id, row.spike_id))
    except Exception as exc:  # noqa: BLE001 - never lose the score over optional context
        get_logger("gate").warning("gate_audience_evidence_failed", error=type(exc).__name__)
        return None


def rendered_clip(
    row: store.ClipRow, chat: calls.ChatReaction | None, audience: calls.AudienceEvidence | None = None
) -> calls.RenderedClip:
    """What P9V is told about the clip: the hook, the subtitle lines (grouped as the factory groups them,
    in clip time) and the chat reaction, or for a viewer-made clip the audience evidence."""
    transcript = row.raw.get("transcript")
    words = subtitle_lines.words_from_transcript(transcript) if isinstance(transcript, Mapping) else []
    commentary = row.raw.get("commentary")
    return calls.RenderedClip(
        hook=str(row.raw.get("hook") or ""),
        length_s=float(row.duration_s or 0.0),
        lines=subtitle_lines.group_lines(words),
        commentary=str(commentary) if commentary else None,
        chat=chat,
        clip_id=row.id,
        audience=audience,
    )


def _writer_record(row: store.ClipRow) -> dict[str, object]:
    record: dict[str, object] = {"score": _score(row)}
    parts = row.raw.get("score_parts")
    if isinstance(parts, Mapping):
        record.update({name: parts[name] for name in ScoreParts.model_fields if name in parts})
    return record


def score_document(
    row: store.ClipRow, scored: VisualClipScore, model: str, frames: int, samples: int
) -> dict[str, object]:
    """`clips.score_parts` after the visual score. The four parts at the top are those of the score now in
    `clips.score` (the shape every reader already knows); `writer` and `visual` hold each judge's own
    score and parts, so the daily report and later tuning can compare them."""
    parts = scored.score_parts.model_dump()
    return {
        **parts,
        "source": VISUAL,
        WRITER: _writer_record(row),
        VISUAL: {
            "score": scored.score,
            **parts,
            "seen": scored.seen,
            "model": model,
            "frames": frames,
            "samples": samples,
        },
    }


def _failed(row: store.ClipRow, error: Exception) -> None:
    message = f"{type(error).__name__}: {error}"[:300]
    get_logger("gate").warning("gate_visual_score_failed", error=message)
    try:
        events.emit(
            "clip.visual_score_failed",
            level="warn",
            entity_type="clip",
            entity_id=row.id,
            message=f"kept the writer's score: {message}",
            service="gate",
        )
    except Exception as exc:  # noqa: BLE001 - losing the event must not cost the clip its gate run
        get_logger("gate").warning("gate_event_failed", error=type(exc).__name__)


def run(
    row: store.ClipRow,
    *,
    client: AnthropicClient | None = None,
    ffmpeg: str = "ffmpeg",
) -> tuple[store.ClipRow, Shots | None]:
    """Score the rendered clip and store the score. Returns the row the gate should judge (reloaded when
    the score was stored) and the extracted frames for P5 (None when they could not be extracted).

    Never raises for a failed score: the row comes back unchanged and the writer's score stands. Only
    `BudgetExceededError` passes through (see the module docstring).
    """
    assert row.local_path is not None and row.duration_s is not None
    lane: BudgetKind = "express" if row.profile == EXPRESS else "vod"
    shots: Shots | None = None
    try:
        count = settings.get_int("ai.visual_score_frames")
        # One fast call on the express path; the median of several only where a minute does not matter.
        samples = 1 if lane == "express" else settings.get_int("ai.visual_score_samples", 1)
        chat = chat_reaction(store.spike_row(row.spike_id), float(row.raw["start_s"]), row.duration_s)
        peak = chat.peak_s if chat is not None else None
        audience = None if chat is not None else viewer_audience(row)
        shots = visuals.sample_score_frames(Path(row.local_path), row.duration_s, count, peak, ffmpeg)
        scored = calls.score_clip_visual(
            rendered_clip(row, chat, audience),
            [jpeg for _, jpeg in shots],
            [time_s for time_s, _ in shots],
            samples=samples,
            lane=lane,
            client=client,
        )
        document = score_document(row, scored, calls.visual_score_model(), len(shots), samples)
        stored = store.store_visual_score(row.id, scored.score, document)
    except calls.BudgetExceededError:
        raise
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the writer's score stands
        _failed(row, exc)
        return row, shots
    get_logger("gate").info(
        "gate_visual_score",
        writer_score=_score(row),
        score=scored.score,
        payoff=scored.score_parts.payoff,
        clarity=scored.score_parts.clarity,
        frames=len(shots),
        stored=stored,
    )
    fresh = store.get_clip(row.id) if stored else None
    return (fresh or row), shots


def p5_frames(shots: Sequence[tuple[float, bytes]] | None, duration_s: float) -> list[bytes] | None:
    """The P5 frames out of the score's extraction, or None when P5 must extract its own."""
    if not shots:
        return None
    try:
        return visuals.p5_frames(shots, duration_s)
    except KeyError:
        return None

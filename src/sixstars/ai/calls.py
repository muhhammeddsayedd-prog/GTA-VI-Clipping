"""Typed functions for each Claude call: load the prompt, call the client, validate the response against its
schema. Implemented in M05.

One function per prompt of docs/15:

    confirm_spikes      P1  fast model    is a chat spike a real moment?
    write_clip          P2  writer model  trim, hook, title, caption, hashtags, safety, score
    pick_moments        P3  writer model  moments from a transcript when a video has no chat
    detect_layout       P4  writer model  facecam and gameplay boxes in one frame (vision)
    visual_check        P5  fast model    last look at frames of the rendered clip (vision)
    read_permission     P6  fast model    does the creator allow or forbid clipping?
    classify_takedown   P7  fast model    is this email a removal request?
    translate_segments  P8  writer model  transcript segments into English
    score_clip          P9  fast model    the score of a finished cut, median of several asks
    score_clip_visual   P9V see below     the score of the rendered clip, judged from its frames (vision)
    sentence_ends       P10 fast model    where sentences end in a transcript without punctuation

Models come from `ai.model_writer` and `ai.model_fast`; P9V uses the one `ai.visual_score_model` names
(`"fast"` or `"writer"`). Tokens and cost are recorded by the client in
`api_usage` with `operation` = the prompt id. Calls made for the VOD lane (`lane="vod"`, the default)
stop with `BudgetExceededError` once today's spend reaches `ai.daily_budget_usd`; express calls
(`lane="express"`) and takedown classification always run.

Errors a caller should expect: `BudgetExceededError`, `InvalidModelOutput` (the reply was still invalid after
the repair retries), `AnthropicRefusal` (the model declined) and `AnthropicError` (API failure). All are
`AnthropicError` subclasses, so the job retry rules can treat them alike.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel

from sixstars import settings
from sixstars.ai import prompt_files
from sixstars.ai.images import prepare_jpeg
from sixstars.ai.schemas import (
    ClipScore,
    ClipWriting,
    Layout,
    Moment,
    PermissionVerdict,
    ScoreParts,
    SpikeVerdict,
    TakedownVerdict,
    TranslatedSegment,
    VisualClipScore,
    VisualVerdict,
    clip_writing_schema,
    layout_schema,
    moment_list_schema,
    sentence_ends_schema,
    spike_batch_schema,
    translated_segments_schema,
)
from sixstars.clients import anthropic_client
from sixstars.clients.anthropic_client import (
    AnthropicClient,
    AnthropicError,
    AnthropicRefusal,
    BudgetKind,
    Effort,
    InvalidModelOutput,
)
from sixstars.logging import get_logger

P1 = "p1_spike_confirm"
P2 = "p2_clip_writer"
P3 = "p3_vod_moment_picker"
P4 = "p4_layout_detect"
P5 = "p5_visual_check"
P6 = "p6_permission_read"
P7 = "p7_takedown_classify"
P8 = "p8_translate_lines"
P9 = "p9_clip_score"
P9V = "p9_visual_score"
P10 = "p10_sentence_ends"

# Shapes fixed by docs/15 (prompt contract, not runtime settings).
SPIKE_BATCH_SIZE = 10
CHAT_SAMPLE_MAX = 40
MOMENT_SLICE_S = 1800.0
TAKEDOWN_BODY_MAX_CHARS = 4000
PERMISSION_TITLES_MAX = 15
TRANSLATE_BATCH_SIZE = 60
VISUAL_FRAMES_MAX = 8
# P9V: frames are shrunk to this long side (a 1080x1920 frame becomes 432x768; the event and the game's
# own text stay readable), and chat is shown from this window around the peak.
VISUAL_SCORE_MAX_SIDE_PX = 768
VISUAL_SCORE_CHAT_MAX = 30
# A viewer-given clip title is shown to P9V cut to this many characters.
AUDIENCE_TITLE_MAX = 140
VISUAL_SCORE_CHAT_BEFORE_S = 15.0
VISUAL_SCORE_CHAT_AFTER_S = 25.0

# Output caps. Both models think before answering and thinking counts toward the cap, so these leave
# room for it; the client doubles the cap once if a reply is cut off.
MAX_TOKENS: dict[str, int] = {
    P1: 2000,
    P2: 5000,
    P3: 5000,
    P4: 2500,
    P5: 2000,
    P6: 1500,
    P7: 2500,
    P8: 8000,
    P9: 2000,
    P9V: 3000,
    P10: 3000,
}
# Reasoning depth per prompt. The clip writer and the layout box are judgement calls worth more thought;
# the classifiers are not.
EFFORT: dict[str, Effort] = {
    P1: "low",
    P2: "medium",
    P3: "medium",
    P4: "medium",
    P5: "low",
    P6: "low",
    P7: "low",
    P8: "low",
    P9: "low",
    P9V: "low",
    P10: "low",
}

_log = get_logger("ai")
_default_client: AnthropicClient | None = None


class BudgetExceededError(AnthropicError):
    """Today's Anthropic spend has reached `ai.daily_budget_usd`; VOD-lane calls wait until 00:00 UTC."""


# ── Inputs ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChatLine:
    """One chat message. `t_s` is relative to the peak (P1) or to the window file start (P2)."""

    t_s: float
    text: str
    user: str | None = None


@dataclass(frozen=True, slots=True)
class SpikeContext:
    """What P1 sees about one spike."""

    spike_id: str
    creator: str
    platform: str
    stream_title: str
    score_z: float
    msgs: int
    uniq: int
    top_tokens: Sequence[str] = ()
    chat: Sequence[ChatLine] = ()
    transcript: str | None = None


class NumberedLine(Protocol):
    """A numbered subtitle line; `sixstars.factory.lines.Line` satisfies it."""

    @property
    def index(self) -> int: ...
    @property
    def start_s(self) -> float: ...
    @property
    def end_s(self) -> float: ...
    @property
    def text(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ClipContext:
    """What P2 sees about one candidate clip. All times are seconds from the start of the window file.

    `now`, `spoiler_block_until` and `reject_topics` default to the clock and to the settings
    `gate.spoiler_block_until` and `gate.reject_topics`; pass them only to override (tests).
    """

    creator: str
    platform: str
    stream_title: str
    lines: Sequence[NumberedLine]
    window_s: float
    proposed_start_s: float
    proposed_end_s: float
    spike_floor_s: float
    min_len_s: float
    max_len_s: float
    chat: Sequence[ChatLine] = ()
    verdict: SpikeVerdict | None = None
    profanity_hint: Sequence[str] = ()
    now: datetime | None = None
    spoiler_block_until: datetime | None = None
    reject_topics: Sequence[str] | None = None
    clip_id: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptChunk:
    """About 30 seconds of transcript; times on the video's own clock."""

    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True, slots=True)
class Segment:
    """One source-language transcript segment."""

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class ChatReaction:
    """The chat spike a clip was cut around, as P9V sees it. The counts are those of the peak window
    (`spikes.msgs_in_bucket`, `unique_chatters`, `reaction_count`); `reactions` are the messages the spike
    engine counted as laughter or shock. `peak_s` is the peak in clip time when that is known; the
    `lines` are timed relative to the peak."""

    msgs: int | None = None
    uniq: int | None = None
    reactions: int | None = None
    tier: str | None = None
    peak_s: float | None = None
    lines: Sequence[ChatLine] = ()


@dataclass(frozen=True, slots=True)
class AudienceEvidence:
    """What is known about a clip that a viewer made on Twitch (decision D110), given to P9V in place
    of a chat spike: there is none. `views` and `age_h` are the Twitch clip's view count and its age when
    discovery read it; `title` is the name the viewer gave it."""

    views: int | None = None
    age_h: float | None = None
    title: str = ""


@dataclass(frozen=True, slots=True)
class RenderedClip:
    """What P9V sees about one rendered clip besides its frames. Line times are clip time. `audience` is
    read only for a clip without a chat spike."""

    hook: str
    length_s: float
    lines: Sequence[NumberedLine]
    commentary: str | None = None
    chat: ChatReaction | None = None
    clip_id: str | None = None
    audience: AudienceEvidence | None = None


# ── Shared plumbing ──────────────────────────────────────────


def _client(client: AnthropicClient | None) -> AnthropicClient:
    global _default_client
    if client is not None:
        return client
    if _default_client is None:
        _default_client = AnthropicClient()
    return _default_client


def _guard(prompt_id: str, lane: BudgetKind) -> None:
    if not anthropic_client.budget_ok(lane):
        _log.warning("ai_budget_exceeded", prompt_id=prompt_id, lane=lane)
        raise BudgetExceededError(f"{prompt_id}: ai.daily_budget_usd is used up for today")


def _writer_model() -> str:
    return settings.get_str("ai.model_writer")


def _fast_model() -> str:
    return settings.get_str("ai.model_fast")


def _system_values() -> dict[str, object]:
    """Values every system prompt may use. They change only when a setting changes, so the cached
    prompt prefix stays stable."""
    return {
        "game": settings.get_str("env.target_game"),
        "watermark": settings.get_str("factory.watermark"),
    }


def _ask[T: BaseModel](
    prompt_id: str,
    schema: type[T],
    model: str,
    user_values: dict[str, object],
    client: AnthropicClient | None,
    images: Sequence[bytes] | None = None,
    ref_id: str | None = None,
) -> T:
    prompt = prompt_files.load(prompt_id)
    return _client(client).complete_json(
        prompt_id,
        prompt.render_system(_system_values()),
        prompt.render_user(user_values),
        schema,
        model,
        MAX_TOKENS[prompt_id],
        images=images,
        cache_system=True,
        effort=EFFORT[prompt_id],
        ref_id=ref_id,
    )


def _seconds(value: float) -> str:
    return f"{value:.2f}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _chat_block(chat: Sequence[ChatLine]) -> str:
    if not chat:
        return "(no chat messages)"
    return "\n".join(f"{line.t_s:+.1f}s  {_one_line(line.text)}" for line in chat[:CHAT_SAMPLE_MAX])


# ── P1 spike_confirm ─────────────────────────────────────────


def _spike_block(spike: SpikeContext) -> str:
    transcript = _one_line(spike.transcript) if spike.transcript else "(not available)"
    tokens = ", ".join(spike.top_tokens) if spike.top_tokens else "(none)"
    return (
        f"### spike_id: {spike.spike_id}\n"
        f"Creator: {spike.creator} ({spike.platform})\n"
        f"Stream title: {_one_line(spike.stream_title)}\n"
        f"Spike stats: score_z {spike.score_z:.1f}, {spike.msgs} messages, {spike.uniq} unique chatters; "
        f"top tokens: {tokens}\n"
        f"Chat around the peak (seconds relative to the peak):\n{_chat_block(spike.chat)}\n"
        f"Transcript of the window: {transcript}"
    )


def confirm_spikes(
    spikes: Sequence[SpikeContext],
    *,
    lane: BudgetKind = "vod",
    client: AnthropicClient | None = None,
) -> list[SpikeVerdict]:
    """P1: one verdict per spike, in the order given.

    A single spike (live mode) is one request; several (replay mode) are sent in batches of up to 10.
    """
    if not spikes:
        return []
    ids = [spike.spike_id for spike in spikes]
    if len(set(ids)) != len(ids):
        raise ValueError("spike ids must be unique")
    _guard(P1, lane)
    model = _fast_model()
    verdicts: list[SpikeVerdict] = []
    for start in range(0, len(spikes), SPIKE_BATCH_SIZE):
        batch = spikes[start : start + SPIKE_BATCH_SIZE]
        values: dict[str, object] = {
            "count": len(batch),
            "spikes": "\n\n".join(_spike_block(spike) for spike in batch),
        }
        if len(batch) == 1:
            verdicts.append(_ask(P1, SpikeVerdict, model, values, client, ref_id=batch[0].spike_id))
            continue
        batch_ids = [spike.spike_id for spike in batch]
        reply = _ask(P1, spike_batch_schema(batch_ids), model, values, client, ref_id=batch_ids[0])
        by_id = {item.spike_id: item for item in reply.results}
        verdicts.extend(by_id[spike_id] for spike_id in batch_ids)
    return verdicts


# ── P2 clip_writer ───────────────────────────────────────────


def _setting_time(key: str) -> datetime | None:
    raw = settings.get(key, None)
    if not isinstance(raw, str) or not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _setting_strings(key: str) -> list[str]:
    raw = settings.get(key, None)
    return [str(item) for item in raw] if isinstance(raw, list) else []


def spoiler_status(now: datetime, spoiler_block_until: datetime | None) -> str:
    """The spoiler rule in words. The date is compared here, not by the model."""
    if spoiler_block_until is not None and now < spoiler_block_until:
        return f"BLOCKED until {spoiler_block_until:%Y-%m-%d %H:%M} UTC (reject a spoiler clip)"
    return "allowed (do not reject a clip only for being a spoiler)"


def enforce_policy(writing: ClipWriting, now: datetime, spoiler_block_until: datetime | None) -> ClipWriting:
    """Code-side backstop for D20 and D21: a clip with a safety flag, or a story spoiler before the
    block date, is rejected even if the model left `reject` false."""
    if writing.reject:
        return writing
    flags = writing.safety.flagged()
    if flags:
        return writing.model_copy(update={"reject": True, "reject_reason": "safety: " + ", ".join(flags)})
    if writing.spoiler and spoiler_block_until is not None and now < spoiler_block_until:
        return writing.model_copy(
            update={"reject": True, "reject_reason": "story spoiler before the block date"}
        )
    return writing


def write_clip(
    ctx: ClipContext,
    *,
    lane: BudgetKind = "vod",
    client: AnthropicClient | None = None,
) -> ClipWriting:
    """P2: trim points and everything written on the post, plus the spoiler, safety and score verdicts.

    The trim is validated against the window: `end_s` inside the file, `start_s` not after
    `spike_floor_s`, length between `min_len_s` and `max_len_s`. `reject` is forced to true when a safety
    flag is set or the clip is a story spoiler before `gate.spoiler_block_until`.
    """
    if not ctx.lines:
        raise ValueError("write_clip needs at least one subtitle line")
    _guard(P2, lane)
    now = ctx.now or datetime.now(UTC)
    block_until = ctx.spoiler_block_until or _setting_time("gate.spoiler_block_until")
    topics = (
        list(ctx.reject_topics) if ctx.reject_topics is not None else _setting_strings("gate.reject_topics")
    )
    if ctx.verdict is not None:
        moment = f"{ctx.verdict.moment_type}: {ctx.verdict.one_line}"
    else:
        moment = "(no spike verdict)"
    values: dict[str, object] = {
        "creator": ctx.creator,
        "platform": ctx.platform,
        "stream_title": _one_line(ctx.stream_title),
        "moment": moment,
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "spoiler_status": spoiler_status(now, block_until),
        "reject_topics": ", ".join(topics) if topics else "(none)",
        "profanity_hint": ", ".join(ctx.profanity_hint) if ctx.profanity_hint else "(none given)",
        "window_s": _seconds(ctx.window_s),
        "proposed_start_s": _seconds(ctx.proposed_start_s),
        "proposed_end_s": _seconds(ctx.proposed_end_s),
        "spike_floor_s": _seconds(ctx.spike_floor_s),
        "min_len_s": f"{ctx.min_len_s:g}",
        "max_len_s": f"{ctx.max_len_s:g}",
        "chat": _chat_block(ctx.chat),
        "lines": "\n".join(
            f"[{line.index}] {_seconds(line.start_s)}-{_seconds(line.end_s)} | {line.text}"
            for line in ctx.lines
        ),
    }
    schema = clip_writing_schema(
        ctx.window_s,
        ctx.spike_floor_s,
        ctx.min_len_s,
        ctx.max_len_s,
        {line.index: line.text for line in ctx.lines},
    )
    reply = _ask(P2, schema, _writer_model(), values, client, ref_id=ctx.clip_id)
    writing = enforce_policy(reply, now, block_until)
    samples = settings.get_int("ai.score_samples", 1)
    if samples > 1 and not writing.reject:
        writing = _rescored(ctx, writing, samples, lane, client)
    _log.info(
        "clip_written",
        clip_id=ctx.clip_id,
        score=writing.score,
        reject=writing.reject,
        reject_reason=writing.reject_reason,
        spoiler=writing.spoiler,
        topic=writing.topic,
    )
    return writing


# ── P9 clip_score ────────────────────────────────────────────


def median_score(scores: Sequence[ClipScore]) -> ClipScore:
    """The middle score of several: each part and the overall score separately (the lower middle one
    for an even number)."""
    if not scores:
        raise ValueError("median_score needs at least one score")
    parts = {
        name: statistics.median_low([getattr(item.score_parts, name) for item in scores])
        for name in ScoreParts.model_fields
    }
    overall = statistics.median_low([item.score for item in scores])
    return ClipScore(score_parts=ScoreParts(**parts), score=overall)


def score_clip(
    ctx: ClipContext,
    writing: ClipWriting,
    samples: int,
    *,
    lane: BudgetKind = "vod",
    client: AnthropicClient | None = None,
) -> ClipScore:
    """P9: score the cut `writing` made of `ctx`, `samples` times with the fast model, and return the
    median. The model sees the hook and the subtitle lines of the cut in clip time, and the same scales
    the clip writer scores with."""
    if samples < 1:
        raise ValueError("samples must be at least 1")
    kept = [line for line in ctx.lines if writing.start_s <= line.start_s < writing.end_s]
    if not kept:
        raise ValueError("the cut holds no subtitle line")
    _guard(P9, lane)
    cut_s = writing.start_s
    values: dict[str, object] = {
        "creator": ctx.creator,
        "platform": ctx.platform,
        "stream_title": _one_line(ctx.stream_title),
        "length_s": f"{writing.length_s:.0f}",
        "hook": writing.hook,
        "lines": "\n".join(
            f"{_seconds(line.start_s - cut_s)}-{_seconds(line.end_s - cut_s)} | {line.text}" for line in kept
        ),
    }
    model = _fast_model()
    scores = [_ask(P9, ClipScore, model, values, client, ref_id=ctx.clip_id) for _ in range(samples)]
    return median_score(scores)


def _rescored(
    ctx: ClipContext, writing: ClipWriting, samples: int, lane: BudgetKind, client: AnthropicClient | None
) -> ClipWriting:
    """`writing` with its score replaced by the median of `samples` P9 scores. The stabiliser never
    costs a clip: if it cannot run, the clip writer's own score stays."""
    try:
        scored = score_clip(ctx, writing, samples, lane=lane, client=client)
    except (AnthropicError, ValueError) as exc:
        _log.warning("clip_score_failed", clip_id=ctx.clip_id, error=f"{type(exc).__name__}: {exc}"[:300])
        return writing
    _log.info(
        "clip_scored",
        clip_id=ctx.clip_id,
        samples=samples,
        writer_score=writing.score,
        score=scored.score,
    )
    return writing.model_copy(update={"score": scored.score, "score_parts": scored.score_parts})


# ── P9V visual_score ─────────────────────────────────────────


# The rubric's bands (`_score_rubric.md`): a score in the band starting at the key needs `payoff` and
# `clarity` both at least the value.
BAND_NEEDS: dict[int, int] = {9: 8, 7: 7}


def bounded_score(score: ClipScore | VisualClipScore) -> int:
    """The overall score held to the rubric's own rules: never more than one above `payoff`, never more
    than two above `clarity`, and in the 7-8 or 9-10 band only when both reach what that band asks for.
    The model is told these rules; here they are enforced. The result is never above the model's score."""
    parts = score.score_parts
    bounded = min(score.score, parts.payoff + 1, parts.clarity + 2)
    for band_start, needed in sorted(BAND_NEEDS.items(), reverse=True):
        if bounded >= band_start and min(parts.payoff, parts.clarity) < needed:
            bounded = band_start - 1
    return max(1, bounded)


def chat_summary(chat: ChatReaction | None) -> str:
    """One line of numbers about the chat spike, in words the model can use."""
    if chat is None:
        return "(no chat data for this clip)"
    facts: list[str] = []
    if chat.msgs is not None:
        who = f" from {chat.uniq} chatters" if chat.uniq is not None else ""
        facts.append(f"{chat.msgs} messages{who} in the peak window")
        if chat.reactions is not None and chat.msgs > 0:
            share = min(100, round(100 * chat.reactions / chat.msgs))
            facts.append(f"{chat.reactions} of them ({share}%) laughter or shock reactions")
    if chat.tier:
        facts.append(f"spike tier {chat.tier} (S strongest, then A, then B)")
    if chat.peak_s is not None:
        facts.append(f"the peak is about {chat.peak_s:.0f} s into the clip (chat reacts a few seconds late)")
    return "; ".join(facts) + "." if facts else "(no chat counts for this clip)"


def audience_summary(audience: AudienceEvidence) -> str:
    """The audience evidence of a viewer-made clip, in words the model can use. It says plainly that
    there is no chat spike, and that views are evidence about the moment, not a score."""
    facts = ["No chat spike: this clip was not cut around a chat peak, and there are no chat messages."]
    made = "A viewer of the stream clipped this moment on Twitch by hand"
    title = _one_line(audience.title)[:AUDIENCE_TITLE_MAX]
    facts.append(f'{made} and named the clip "{title}".' if title else f"{made}.")
    if audience.views is not None:
        age = ""
        if audience.age_h is not None:
            hours = max(1, round(audience.age_h))
            age = f" about {hours} hour{'s' if hours != 1 else ''} after it was made"
        facts.append(f"That Twitch clip had {audience.views} views{age}.")
    facts.append(
        "This is evidence that people who watched thought the moment worth keeping, not a score: "
        "judge what the frames and the lines show."
    )
    return " ".join(facts)


def _reaction_block(chat: ChatReaction | None) -> str:
    """The messages near the peak, in clip time when the peak position is known."""
    if chat is None:
        return "(no chat messages)"
    near = [
        line
        for line in chat.lines
        if -VISUAL_SCORE_CHAT_BEFORE_S <= line.t_s <= VISUAL_SCORE_CHAT_AFTER_S and line.text.strip()
    ][:VISUAL_SCORE_CHAT_MAX]
    if not near:
        return "(no chat messages near the peak)"
    if chat.peak_s is None:
        head = "Messages around the peak (seconds relative to the peak):"
        rows = [f"{line.t_s:+.0f}s  {_one_line(line.text)}" for line in near]
    else:
        head = "Messages around the peak (seconds from the start of the clip; some fall outside it):"
        rows = [f"{chat.peak_s + line.t_s:.0f}s  {_one_line(line.text)}" for line in near]
    return "\n".join([head, *rows])


def visual_score_model() -> str:
    """The model id P9V runs on: the fast or the writer model, as `ai.visual_score_model` says."""
    role = settings.get_str("ai.visual_score_model", "writer")
    if role == "writer":
        return _writer_model()
    if role == "fast":
        return _fast_model()
    raise ValueError(f"ai.visual_score_model must be 'fast' or 'writer', not {role!r}")


def score_clip_visual(
    clip: RenderedClip,
    frames: Sequence[bytes],
    frame_times_s: Sequence[float],
    *,
    samples: int = 1,
    lane: BudgetKind = "vod",
    client: AnthropicClient | None = None,
) -> VisualClipScore:
    """P9V: score the rendered clip from `frames` (JPEG, in time order, taken at `frame_times_s` of clip
    time), its hook, its subtitle lines and the chat reaction, on the same scales as the clip writer.

    `samples` requests go to the model `ai.visual_score_model` names, each recorded as operation
    `p9_visual_score`; the median is returned (`median_visual_score`). The returned `score` obeys the
    rubric's limits (`bounded_score`). Raises `ValueError` for unusable input and the `AnthropicError`
    family when a call fails (`BudgetExceededError` for a VOD clip over the daily budget); the caller
    keeps the clip writer's score then."""
    if samples < 1:
        raise ValueError("samples must be at least 1")
    if not frames:
        raise ValueError("score_clip_visual needs at least one frame")
    if len(frames) > VISUAL_FRAMES_MAX:
        raise ValueError(f"score_clip_visual takes at most {VISUAL_FRAMES_MAX} frames")
    if len(frame_times_s) != len(frames):
        raise ValueError("one time is needed for each frame")
    model = visual_score_model()
    _guard(P9V, lane)
    images = [prepare_jpeg(frame, max_side=VISUAL_SCORE_MAX_SIDE_PX).data for frame in frames]
    lines = "\n".join(f"{_seconds(line.start_s)}-{_seconds(line.end_s)} | {line.text}" for line in clip.lines)
    values: dict[str, object] = {
        "length_s": f"{clip.length_s:.0f}",
        "hook": _one_line(clip.hook) or "(none)",
        "commentary": _one_line(clip.commentary) if clip.commentary else "(none)",
        "count": len(images),
        "frame_times": ", ".join(f"{time_s:.1f}" for time_s in frame_times_s),
        "lines": lines or "(nobody speaks)",
        "chat_summary": (
            audience_summary(clip.audience)
            if clip.chat is None and clip.audience is not None
            else chat_summary(clip.chat)
        ),
        "chat": _reaction_block(clip.chat),
    }
    asked: list[VisualClipScore] = []
    for _ in range(samples):
        reply = _ask(P9V, VisualClipScore, model, values, client, images=images, ref_id=clip.clip_id)
        asked.append(reply.model_copy(update={"score": bounded_score(reply)}))
    scored = median_visual_score(asked)
    _log.info(
        "clip_scored_visual",
        clip_id=clip.clip_id,
        model=model,
        frames=len(images),
        samples=samples,
        score=scored.score,
        asked_scores=[item.score for item in asked],
        payoff=scored.score_parts.payoff,
        clarity=scored.score_parts.clarity,
    )
    return scored


def median_visual_score(scores: Sequence[VisualClipScore]) -> VisualClipScore:
    """The middle of several visual scores: each part and the overall score separately (the lower middle
    one for an even number), held to the rubric's limits again, with the description of an ask that
    gave that overall score."""
    middle = median_score([ClipScore(score_parts=item.score_parts, score=item.score) for item in scores])
    seen = next((item.seen for item in scores if item.score == middle.score), scores[0].seen)
    result = VisualClipScore(seen=seen, score_parts=middle.score_parts, score=middle.score)
    return result.model_copy(update={"score": bounded_score(result)})


# ── P10 sentence_ends ────────────────────────────────────────


def sentence_ends(
    words: Sequence[str],
    *,
    lane: BudgetKind = "vod",
    ref_id: str | None = None,
    client: AnthropicClient | None = None,
) -> list[int]:
    """P10: the indexes of the words that end a sentence, in order, for a transcript that came back
    without punctuation. The words themselves are never changed by the model: it answers with numbers."""
    if not words:
        return []
    _guard(P10, lane)
    values: dict[str, object] = {
        "count": len(words),
        "last": len(words) - 1,
        "words": " ".join(f"{index}:{_one_line(word)}" for index, word in enumerate(words)),
    }
    reply = _ask(P10, sentence_ends_schema(len(words)), _fast_model(), values, client, ref_id=ref_id)
    return list(reply.ends)


# ── P3 vod_moment_picker ─────────────────────────────────────


def slice_chunks(
    chunks: Sequence[TranscriptChunk], slice_s: float = MOMENT_SLICE_S
) -> list[list[TranscriptChunk]]:
    """Group transcript chunks into consecutive slices of about `slice_s` seconds."""
    slices: list[list[TranscriptChunk]] = []
    for chunk in sorted(chunks, key=lambda item: item.start_s):
        if slices and chunk.end_s - slices[-1][0].start_s <= slice_s:
            slices[-1].append(chunk)
        else:
            slices.append([chunk])
    return slices


def merge_moments(moments: Sequence[Moment], n_max: int) -> list[Moment]:
    """Strongest first, overlapping moments dropped (the stronger one stays), at most `n_max`."""
    kept: list[Moment] = []
    for moment in sorted(moments, key=lambda item: (-item.confidence, item.start_s)):
        if len(kept) >= n_max:
            break
        if not any(moment.overlaps(other) for other in kept):
            kept.append(moment)
    return kept


def pick_moments(
    transcript_chunks: Sequence[TranscriptChunk],
    n_min: int,
    n_max: int,
    len_min: float,
    len_max: float,
    *,
    title: str = "",
    creator: str = "",
    ref_id: str | None = None,
    client: AnthropicClient | None = None,
) -> list[Moment]:
    """P3 (VOD lane only): the strongest standalone moments of a video with no chat, strongest first.

    Long transcripts are processed in 30-minute slices and merged. Fewer than `n_min` moments come back
    when the video does not have that many good ones. A slice whose reply is unusable or declined is
    logged and skipped; the error is raised only when every slice failed.
    """
    chunks = [chunk for chunk in transcript_chunks if chunk.text.strip()]
    if not chunks:
        return []
    _guard(P3, "vod")
    model = _writer_model()
    slices = slice_chunks(chunks)
    found: list[Moment] = []
    failed: list[AnthropicError] = []
    for part in slices:
        start_s, end_s = part[0].start_s, max(chunk.end_s for chunk in part)
        values: dict[str, object] = {
            "title": _one_line(title) or "(unknown)",
            "creator": creator or "(unknown)",
            "slice_start_s": f"{start_s:.0f}",
            "slice_end_s": f"{end_s:.0f}",
            "n_min": n_min if len(slices) == 1 else 0,
            "n_max": n_max,
            "len_min_s": f"{len_min:g}",
            "len_max_s": f"{len_max:g}",
            "transcript": "\n".join(
                f"[{chunk.start_s:.0f}-{chunk.end_s:.0f}] {_one_line(chunk.text)}" for chunk in part
            ),
        }
        schema = moment_list_schema(n_max, len_min, len_max, start_s, end_s)
        try:
            found.extend(_ask(P3, schema, model, values, client, ref_id=ref_id).moments)
        except (InvalidModelOutput, AnthropicRefusal) as exc:
            # One unusable part must not cost the moments of the other parts.
            failed.append(exc)
            _log.warning(
                "moment_slice_failed",
                prompt_id=P3,
                ref_id=ref_id,
                slice_start_s=round(start_s),
                slice_end_s=round(end_s),
                slices=len(slices),
                error=str(exc)[:300],
            )
    if failed and len(failed) == len(slices):
        raise failed[-1]
    return merge_moments(found, n_max)


# ── P4 layout_detect ─────────────────────────────────────────


def detect_layout(
    frame_jpeg: bytes,
    w: int,
    h: int,
    *,
    lane: BudgetKind = "vod",
    ref_id: str | None = None,
    client: AnthropicClient | None = None,
) -> Layout:
    """P4: facecam and gameplay boxes of one frame, in the pixels of the original `w` x `h` video.

    The frame is shrunk to at most 1280 px before it is sent; the model answers in the pixels of the
    image it sees and the boxes are scaled back here. `facecam` is None when there is no webcam.
    """
    if w <= 0 or h <= 0:
        raise ValueError("frame width and height must be positive")
    _guard(P4, lane)
    image = prepare_jpeg(frame_jpeg)
    values: dict[str, object] = {"sent_w": image.width, "sent_h": image.height}
    seen = _ask(
        P4,
        layout_schema(image.width, image.height),
        _writer_model(),
        values,
        client,
        images=[image.data],
        ref_id=ref_id,
    )
    factor_x, factor_y = w / image.width, h / image.height
    return Layout(
        facecam=seen.facecam.scaled(factor_x, factor_y, w, h) if seen.facecam is not None else None,
        gameplay=seen.gameplay.scaled(factor_x, factor_y, w, h),
        confidence=seen.confidence,
    )


# ── P5 visual_check ──────────────────────────────────────────


def visual_check(
    frames: Sequence[bytes],
    *,
    lane: BudgetKind = "vod",
    ref_id: str | None = None,
    client: AnthropicClient | None = None,
) -> VisualVerdict:
    """P5: what the vision model sees in frames of the rendered clip (the gate sends four, taken at
    10%, 35%, 60% and 85%)."""
    if not frames:
        raise ValueError("visual_check needs at least one frame")
    if len(frames) > VISUAL_FRAMES_MAX:
        raise ValueError(f"visual_check takes at most {VISUAL_FRAMES_MAX} frames")
    _guard(P5, lane)
    images = [prepare_jpeg(frame).data for frame in frames]
    values: dict[str, object] = {"count": len(images)}
    return _ask(P5, VisualVerdict, _fast_model(), values, client, images=images, ref_id=ref_id)


# ── P6 permission_read ───────────────────────────────────────


def read_permission(
    description: str,
    name: str,
    titles: Sequence[str],
    *,
    ref_id: str | None = None,
    client: AnthropicClient | None = None,
) -> PermissionVerdict:
    """P6 (discovery, VOD budget): the creator's clipping permission tier from their public text."""
    _guard(P6, "vod")
    shown = [f"- {_one_line(title)}" for title in titles[:PERMISSION_TITLES_MAX] if title.strip()]
    values: dict[str, object] = {
        "name": _one_line(name) or "(unknown)",
        "description": description.strip() or "(empty)",
        "titles": "\n".join(shown) if shown else "(none)",
    }
    return _ask(P6, PermissionVerdict, _fast_model(), values, client, ref_id=ref_id)


# ── P7 takedown_classify ─────────────────────────────────────


def classify_takedown(
    sender: str,
    subject: str,
    body: str,
    *,
    ref_id: str | None = None,
    client: AnthropicClient | None = None,
) -> TakedownVerdict:
    """P7: is this email a removal request? Never blocked by the budget: a takedown must be handled."""
    values: dict[str, object] = {
        "sender": _one_line(sender) or "(unknown)",
        "subject": _one_line(subject) or "(no subject)",
        "body": body.strip()[:TAKEDOWN_BODY_MAX_CHARS] or "(empty)",
    }
    return _ask(P7, TakedownVerdict, _fast_model(), values, client, ref_id=ref_id)


# ── P8 translate_lines ───────────────────────────────────────


def translate_segments(
    segments: Sequence[Segment],
    source_lang: str,
    *,
    lane: BudgetKind = "vod",
    ref_id: str | None = None,
    client: AnthropicClient | None = None,
) -> list[TranslatedSegment]:
    """P8: English text for each source segment, one output per input, with the source times kept."""
    if not segments:
        return []
    _guard(P8, lane)
    model = _writer_model()
    translated: list[TranslatedSegment] = []
    for start in range(0, len(segments), TRANSLATE_BATCH_SIZE):
        batch = segments[start : start + TRANSLATE_BATCH_SIZE]
        values: dict[str, object] = {
            "source_lang": source_lang or "unknown",
            "segments": "\n".join(
                f"{_seconds(item.start)}-{_seconds(item.end)} | {_one_line(item.text)}" for item in batch
            ),
        }
        reply = _ask(P8, translated_segments_schema(len(batch)), model, values, client, ref_id=ref_id)
        # The source times drive the subtitle sync, so they are kept whatever the model echoed back.
        translated.extend(
            TranslatedSegment(start=source.start, end=source.end, en=item.en)
            for source, item in zip(batch, reply.segments, strict=True)
        )
    return translated

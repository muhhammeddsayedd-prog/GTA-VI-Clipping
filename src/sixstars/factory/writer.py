"""Claude-written clip metadata: hook, title and captions for each platform. Implemented in M06.

Builds the `ClipContext` for prompt P2 from the numbered subtitle lines, calls `calls.write_clip`, turns
the reply's in and out points into the final clip window (trim rules, then the snap to a pause so the
clip opens and closes on a phrase, then a small move so the cut does not land inside a word) and maps the
reply to `clips` columns. All times here are in window time: seconds
from the start of the transcribed (padded) window.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from sixstars.ai import calls
from sixstars.ai.calls import ChatLine, ClipContext
from sixstars.ai.schemas import ClipWriting, SpikeVerdict
from sixstars.clients.anthropic_client import AnthropicClient, BudgetKind
from sixstars.factory.bleep import ProfanityMatcher, Span, normalize_word
from sixstars.factory.lines import Line, Word
from sixstars.factory.pauses import SnapRules, snap_to_pauses
from sixstars.factory.subtitles import keyword_flags
from sixstars.factory.window import ClipWindow, resolve_trim

# Room left around the first and last word so a cut never clips a syllable (Whisper word ends run early).
CUT_LEAD_S = 0.15
CUT_TAIL_S = 0.3
# Two keyword pops closer than this sound like one stutter; the later one is dropped.
SFX_MIN_GAP_S = 0.35
PROFANITY_HINT_MAX = 12


@dataclass(frozen=True, slots=True)
class WriterInput:
    """What the clip writer needs about one candidate clip (window time)."""

    creator: str
    platform: str
    stream_title: str
    lines: Sequence[Line]
    window_s: float
    proposed: ClipWindow
    spike_floor_s: float
    min_len_s: float
    max_len_s: float
    chat: Sequence[ChatLine] = ()
    verdict: SpikeVerdict | None = None
    profanity_hint: Sequence[str] = ()
    clip_id: str | None = None
    snap: SnapRules = SnapRules()
    cut_guidance: str = ""

    @property
    def effective_min_len_s(self) -> float:
        """The minimum length, lowered to the window length when the window file is shorter."""
        return min(self.min_len_s, self.window_s)

    @property
    def effective_floor_s(self) -> float:
        """The latest allowed start: the spike floor, kept where a clip of minimum length still fits."""
        floor = min(max(self.spike_floor_s, 0.0), self.window_s)
        return max(0.0, min(floor, self.window_s - self.effective_min_len_s))


def build_context(item: WriterInput) -> ClipContext:
    """The P2 input. Values are clamped so the rules the reply is validated against can be met."""
    end = min(item.proposed.end_s, item.window_s)
    start = min(max(item.proposed.start_s, 0.0), item.effective_floor_s)
    return ClipContext(
        creator=item.creator,
        platform=item.platform,
        stream_title=item.stream_title,
        lines=item.lines,
        window_s=item.window_s,
        proposed_start_s=start,
        proposed_end_s=max(end, start),
        spike_floor_s=item.effective_floor_s,
        min_len_s=item.effective_min_len_s,
        max_len_s=max(item.max_len_s, item.effective_min_len_s),
        chat=item.chat,
        verdict=item.verdict,
        profanity_hint=tuple(item.profanity_hint),
        clip_id=item.clip_id,
        cut_guidance=item.cut_guidance,
    )


def write(
    item: WriterInput, *, lane: BudgetKind = "vod", client: AnthropicClient | None = None
) -> ClipWriting:
    """Run prompt P2 for the clip."""
    return calls.write_clip(build_context(item), lane=lane, client=client)


def profanity_hint(words: Sequence[Word], matcher: ProfanityMatcher) -> list[str]:
    """The distinct profane words of the transcript (normalised), for the P2 input."""
    texts = [word.text for word in words]
    found: list[str] = []
    for index in matcher.find(texts):
        word = normalize_word(texts[index])
        if word and word not in found:
            found.append(word)
    return found[:PROFANITY_HINT_MAX]


def snap_to_words(
    window: ClipWindow, words: Sequence[Word], *, limit_s: float, max_len_s: float
) -> ClipWindow:
    """Moves the cut points off the words.

    The start moves up to `CUT_LEAD_S` earlier, but not into the previous word. The end moves to
    `CUT_TAIL_S` after the last word that starts inside the window, but not into the next word, not past
    `limit_s` and never making the clip longer than `max_len_s`. A window with no words is unchanged.
    """
    inside = [word for word in words if window.start_s <= word.start_s < window.end_s]
    if not inside:
        return window
    before = [word.end_s for word in words if word.start_s < window.start_s]
    start = max(0.0, window.start_s - CUT_LEAD_S, min(max(before, default=0.0), window.start_s))
    after = [word.start_s for word in words if word.start_s >= window.end_s]
    last_end = max(word.end_s for word in inside)
    end = window.end_s
    if last_end + CUT_TAIL_S > end:
        end = min(last_end + CUT_TAIL_S, max(min(after, default=limit_s), last_end))
    end = min(end, limit_s, start + max_len_s)
    return ClipWindow(start, end) if end > start else window


def final_window(writing: ClipWriting, item: WriterInput) -> ClipWindow:
    """The clip window in window time: the writer's trim under the trim rules, snapped to the nearest
    pause or sentence boundary (`item.snap`, the setting `factory.cut_snap`), then moved off the words."""
    max_len_s = max(item.max_len_s, item.effective_min_len_s)
    trimmed = resolve_trim(
        item.proposed,
        writer_start_s=writing.start_s,
        writer_end_s=writing.end_s,
        spike_floor_s=item.effective_floor_s,
        min_len_s=item.effective_min_len_s,
        max_len_s=max_len_s,
        file_duration_s=item.window_s,
    )
    words = [word for line in item.lines for word in line.words]
    snapped = snap_to_pauses(
        trimmed,
        words,
        item.snap,
        floor_s=item.effective_floor_s,
        min_len_s=item.effective_min_len_s,
        max_len_s=max_len_s,
        limit_s=item.window_s,
    )
    return snap_to_words(snapped, words, limit_s=item.window_s, max_len_s=item.max_len_s)


def keyword_times(lines: Sequence[Line], keywords: Mapping[int, Sequence[str]]) -> tuple[float, ...]:
    """When each keyword starts being spoken (clip time), for the pop sound.

    A multi-word keyword pops once, on its first word. Pops closer together than `SFX_MIN_GAP_S` are
    thinned out.
    """
    times: list[float] = []
    for line in lines:
        flags = keyword_flags([word.text for word in line.words], keywords.get(line.index, ()))
        for i, word in enumerate(line.words):
            if flags[i] and (i == 0 or not flags[i - 1]):
                times.append(word.start_s)
    kept: list[float] = []
    for time_s in sorted(times):
        if not kept or time_s - kept[-1] >= SFX_MIN_GAP_S:
            kept.append(round(time_s, 3))
    return tuple(kept)


def outside_spans(times_s: Sequence[float], spans: Sequence[Span]) -> tuple[float, ...]:
    """The times that fall in none of the spans (a bleeped keyword gets no pop on top of the bleep)."""
    return tuple(t for t in times_s if not any(span.start_s <= t <= span.end_s for span in spans))


def clip_columns(writing: ClipWriting) -> dict[str, object]:
    """The P2 outputs as `clips` column values (JSON columns already wrapped for psycopg)."""
    return {
        "hook": writing.hook,
        "title": writing.title,
        "caption": writing.caption,
        "hashtags": list(writing.hashtags),
        "commentary": writing.commentary,
        "topic": writing.topic,
        "keywords": Jsonb({str(line): words for line, words in writing.keyword_map().items()}),
        "spoiler": writing.spoiler,
        "safety_flags": Jsonb(writing.safety.model_dump()),
        "score": writing.score,
        "score_parts": Jsonb(writing.score_parts.model_dump()),
    }

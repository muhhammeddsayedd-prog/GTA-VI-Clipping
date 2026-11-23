"""Pydantic schemas for the structured responses expected from each Claude prompt (docs/15, P1 to P8).
Implemented in M05.

The Anthropic client sends the JSON Schema of the model as the structured output format, so the API
returns JSON of that shape, and then validates the reply with the model; a reply that fails validation is
sent back with the error. The limits of docs/15 (hook length, title length, hashtag shape, score range)
cannot be expressed in the API's schema dialect, so they are validators here and a reply that breaks one
is repaired by the model instead of reaching the renderer. Field order matters: the model writes the
fields in the order they are declared.

Limits that depend on the call (the window length, the frame size, the spike ids) are added by the
`*_schema(...)` factories: they return a subclass with the same title and JSON Schema as the base model,
so the API compiles and caches one output grammar per prompt.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

MomentType = Literal["funny", "fail", "skill", "chaos", "shock", "reveal", "emotional", "other"]
PermissionTier = Literal["preferred", "auto", "restricted"]
TakedownKind = Literal["copyright", "privacy", "trademark", "other", "not_takedown"]

# Limits fixed by docs/15 (they are part of the prompt contract, not runtime settings).
ONE_LINE_MAX_WORDS = 15
HOOK_MAX_WORDS = 8
TITLE_MAX_CHARS = 70
CAPTION_MAX_CHARS = 120
HASHTAGS_MIN = 3
HASHTAGS_MAX = 5
COMMENTARY_MAX_WORDS = 12
KEYWORDS_PER_LINE = 2
# An emoji with skin-tone or ZWJ parts is several code points; two such emoji fit.
PAYOFF_EMOJI_MAX_CHARS = 8
# Design styles (development-plan/M18 Phase B): the kind of moment and the text each style shows.
MOMENT_TYPES = ("reaction", "discovery", "action", "story", "npc_funny", "fail")
HEADLINE_MAX_CHARS = 90
STYLE_LINE_MAX_CHARS = 48
EVIDENCE_MAX_WORDS = 20
TOPIC_MAX_CHARS = 80
PLATFORMS = ("tiktok", "instagram", "youtube", "twitch")
# Seconds of slack when a time is checked against a bound (float formatting, word boundaries).
TIME_TOLERANCE_S = 0.05

_HASHTAG_RE = re.compile(r"^[a-z0-9_]+$")
_SLUG_JUNK_RE = re.compile(r"[^a-z0-9]+")
_WORD_RE = re.compile(r"\w", re.UNICODE)
# Emoji and spaces a question may end with after its "?" ("Can you punch a bird? 🐦").
_EMOJI_TAIL = " \U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u200d"
_TOKEN_EDGE_RE = re.compile(r"^\W+|\W+$", re.UNICODE)


def count_words(text: str) -> int:
    """Number of words in `text`. A token with no letter or digit (an emoji, a dash) is not a word."""
    return sum(1 for token in text.split() if _WORD_RE.search(token))


def _tokens(text: str) -> list[str]:
    """Lowercase words of `text` with the punctuation around them removed."""
    cleaned = (_TOKEN_EDGE_RE.sub("", token).lower() for token in text.split())
    return [token for token in cleaned if token]


def _contains(line: Sequence[str], phrase: Sequence[str]) -> bool:
    """True when the words of `phrase` appear next to each other, in order, in `line`."""
    size = len(phrase)
    return size > 0 and any(list(line[i : i + size]) == list(phrase) for i in range(len(line) - size + 1))


def _max_words(value: str, limit: int, name: str) -> str:
    """The error lists the words as they were counted: sent back to the model, that is what lets it
    shorten the text instead of rewording it at the same length."""
    words = [token for token in value.split() if _WORD_RE.search(token)]
    if len(words) > limit:
        raise ValueError(
            f"{name} has {len(words)} words ({' | '.join(words)}), the maximum is {limit}: "
            f"rewrite it with at least {len(words) - limit} fewer"
        )
    return value


class Reply(BaseModel):
    """Base of every model reply: unknown keys are ignored and strings are trimmed."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# ── P1 spike_confirm ─────────────────────────────────────────


class SpikeVerdict(Reply):
    """Is the chat spike a real, clip-worthy moment?"""

    is_moment: bool
    confidence: float = Field(ge=0.0, le=1.0)
    moment_type: MomentType
    one_line: str = Field(description="What happened, max 15 words, no spoilers.")
    noise_reason: str | None = None

    @field_validator("one_line")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return _max_words(value, ONE_LINE_MAX_WORDS, "one_line")


class SpikeVerdictItem(SpikeVerdict):
    """One verdict of a batch, tied to the spike it judges."""

    spike_id: str


class SpikeVerdictBatch(Reply):
    """Replay mode: one verdict per spike."""

    results: list[SpikeVerdictItem]


def spike_batch_schema(spike_ids: Sequence[str]) -> type[SpikeVerdictBatch]:
    """`SpikeVerdictBatch` that must hold exactly one result for each of `spike_ids`."""
    expected = list(spike_ids)

    class Bound(SpikeVerdictBatch):
        model_config = ConfigDict(title="SpikeVerdictBatch")

        @model_validator(mode="after")
        def _ids(self) -> Self:
            got = [item.spike_id for item in self.results]
            if sorted(got) != sorted(expected):
                raise ValueError(
                    f"results must have exactly one entry for each of these spike_id values: {expected}; "
                    f"got {got}"
                )
            return self

    return Bound


# ── P2 clip_writer ───────────────────────────────────────────


class KeywordHighlight(Reply):
    """Words to highlight in one subtitle line."""

    line: int = Field(ge=0)
    words: list[str] = Field(max_length=KEYWORDS_PER_LINE)


class SafetyFlags(Reply):
    """Content the channel never posts (D21). Any true flag rejects the clip."""

    politics: bool = False
    gambling: bool = False
    sponsored: bool = False
    real_violence: bool = False
    sexual: bool = False
    personal_info: bool = False
    hate: bool = False
    copyrighted_media: bool = False

    def flagged(self) -> list[str]:
        """Names of the flags that are set, in schema order."""
        return [name for name in type(self).model_fields if getattr(self, name)]


class ScoreParts(Reply):
    hook: int = Field(ge=1, le=10)
    clarity: int = Field(ge=1, le=10)
    payoff: int = Field(ge=1, le=10)
    rewatch: int = Field(ge=1, le=10)


class ClipScore(Reply):
    """P9: the score of one finished clip, on the same scales as the clip writer's."""

    score_parts: ScoreParts
    score: int = Field(ge=1, le=10)


class VisualClipScore(Reply):
    """P9V: the score of one rendered clip, judged from its frames, its lines and the chat reaction.
    `seen` comes first so the model says what happens on screen before it scores it."""

    seen: str = Field(min_length=1, description="One or two sentences: what happens, and the payoff.")
    score_parts: ScoreParts
    score: int = Field(ge=1, le=10)


class ClipWriting(Reply):
    """Everything needed to trim, subtitle, title and post one clip."""

    start_s: float = Field(ge=0.0)
    end_s: float
    hook: str = Field(min_length=1, description="Max 8 words.")
    title: str = Field(min_length=1, description="Max 70 characters, no hashtags.")
    caption: str = Field(min_length=1, description="Max 120 characters.")
    hashtags: list[str] = Field(description="3 to 5 lowercase tags without '#'.")
    commentary: str | None = Field(default=None, description="Max 12 words, or null.")
    topic: str = Field(min_length=1, description="Lowercase slug, words joined by hyphens.")
    keywords: list[KeywordHighlight] = Field(default_factory=list)
    payoff_s: float | None = Field(
        default=None, description="When the payoff lands (seconds, window-file clock), or null."
    )
    payoff_emoji: str | None = Field(default=None, description="One emoji for the payoff, or null.")
    moment_type: str | None = Field(
        default=None, description="reaction, discovery, action, story, npc_funny or fail."
    )
    headline: str | None = Field(default=None, description="News-style sentence, max 90 characters.")
    title_line: str | None = Field(default=None, description="On-screen title, max 48 characters.")
    question: str | None = Field(default=None, description="Question title ending in '?', or null.")
    pov_line: str | None = Field(default=None, description="Line starting with 'POV', or null.")
    story_start_s: float | None = Field(
        default=None, description="Start of the longer story cut (window-file clock), or null."
    )
    story_end_s: float | None = Field(default=None, description="End of the story cut, or null.")
    spoiler: bool
    safety: SafetyFlags
    score_parts: ScoreParts
    score: int = Field(ge=1, le=10)
    reject: bool
    reject_reason: str | None = None

    @field_validator("hook")
    @classmethod
    def _hook(cls, value: str) -> str:
        return _max_words(value, HOOK_MAX_WORDS, "hook")

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        if len(value) > TITLE_MAX_CHARS:
            raise ValueError(f"title has {len(value)} characters, the maximum is {TITLE_MAX_CHARS}")
        if "#" in value:
            raise ValueError("title must not contain hashtags")
        return value

    @field_validator("caption")
    @classmethod
    def _caption(cls, value: str) -> str:
        if len(value) > CAPTION_MAX_CHARS:
            raise ValueError(f"caption has {len(value)} characters, the maximum is {CAPTION_MAX_CHARS}")
        return value

    @field_validator("hashtags", mode="before")
    @classmethod
    def _hashtags(cls, value: Any) -> Any:
        """A leading '#', capitals and repeats are tidied; anything else wrong is an error."""
        if not isinstance(value, list):
            return value
        tags: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("hashtags must be strings")
            tag = item.strip().lstrip("#").lower()
            if not _HASHTAG_RE.match(tag):
                raise ValueError(f"hashtag {item!r} must be one lowercase word: letters, digits or '_'")
            if tag not in tags:
                tags.append(tag)
        if not HASHTAGS_MIN <= len(tags) <= HASHTAGS_MAX:
            raise ValueError(f"{len(tags)} hashtags given, {HASHTAGS_MIN} to {HASHTAGS_MAX} are needed")
        return tags

    @field_validator("commentary")
    @classmethod
    def _commentary(cls, value: str | None) -> str | None:
        if not value:
            return None
        return _max_words(value, COMMENTARY_MAX_WORDS, "commentary")

    @field_validator("topic")
    @classmethod
    def _topic(cls, value: str) -> str:
        slug = _SLUG_JUNK_RE.sub("-", value.lower()).strip("-")
        if not slug:
            raise ValueError("topic must contain letters or digits")
        if len(slug) > TOPIC_MAX_CHARS:
            raise ValueError(f"topic has {len(slug)} characters, the maximum is {TOPIC_MAX_CHARS}")
        return slug

    @field_validator("moment_type")
    @classmethod
    def _moment_type(cls, value: str | None) -> str | None:
        """A decoration like the styles' texts: an unknown kind is dropped, not an error."""
        kind = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
        return kind if kind in MOMENT_TYPES else None

    @field_validator("headline", "title_line", "question", "pov_line")
    @classmethod
    def _style_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        """The texts of the design styles are decorations: one that breaks its rule is dropped (the style
        that needs it is then not used), never an error."""
        text = " ".join((value or "").split())
        limit = HEADLINE_MAX_CHARS if info.field_name == "headline" else STYLE_LINE_MAX_CHARS
        if not text or len(text) > limit or "#" in text or "@" in text:
            return None
        if info.field_name == "question" and not re.sub(f"[{_EMOJI_TAIL}]+$", "", text).endswith("?"):
            return None
        if info.field_name == "pov_line" and not text.lower().startswith("pov"):
            return None
        return text

    def style_texts(self) -> dict[str, str]:
        """The texts the design styles can show, by name (only those the writer gave)."""
        texts = {
            "headline": self.headline,
            "title_line": self.title_line,
            "question": self.question,
            "pov_line": self.pov_line,
        }
        return {name: text for name, text in texts.items() if text}

    @field_validator("payoff_emoji")
    @classmethod
    def _payoff_emoji(cls, value: str | None) -> str | None:
        """The payoff emoji is a decoration: anything that is not one or two emoji is dropped, not an
        error (letters, digits or more than a few characters)."""
        if value is None:
            return None
        clean = "".join(value.split())
        if not clean or len(clean) > PAYOFF_EMOJI_MAX_CHARS or _WORD_RE.search(clean):
            return None
        return clean

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.start_s >= self.end_s:
            raise ValueError("start_s must be less than end_s")
        if self.payoff_s is not None and not self.start_s <= self.payoff_s <= self.end_s:
            self.payoff_s = None
        story = (self.story_start_s, self.story_end_s)
        if None in story or story[0] >= story[1]:  # type: ignore[operator]
            self.story_start_s = self.story_end_s = None
        return self

    @property
    def has_story(self) -> bool:
        return self.story_start_s is not None and self.story_end_s is not None

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s

    def keyword_map(self) -> dict[int, list[str]]:
        """Highlights keyed by subtitle line number, as the subtitle renderer takes them."""
        merged: dict[int, list[str]] = {}
        for item in self.keywords:
            words = merged.setdefault(item.line, [])
            words.extend(word for word in item.words if word and word not in words)
        return {line: words[:KEYWORDS_PER_LINE] for line, words in merged.items() if words}


def clip_writing_schema(
    window_s: float,
    spike_floor_s: float,
    min_len_s: float,
    max_len_s: float,
    lines: Mapping[int, str],
    story_min_s: float = 0.0,
    story_max_s: float = 0.0,
) -> type[ClipWriting]:
    """`ClipWriting` with the trim rules of one call: inside the window file, not starting after
    `spike_floor_s`, length between the limits.

    `lines` maps each subtitle line number to its text. A highlight is only a decoration, so a keyword
    for an unknown line, or one that is not in the text of its line, is dropped instead of failing the
    reply. The story cut (development-plan/M18 Phase C) is kept only when `story_max_s` is positive and it
    lies inside the window, is `story_min_s`..`story_max_s` long and overlaps the short cut; otherwise it
    is dropped, never an error."""
    tokens_by_line = {number: _tokens(text) for number, text in lines.items()}

    class Bound(ClipWriting):
        model_config = ConfigDict(title="ClipWriting")

        @model_validator(mode="after")
        def _story(self) -> Self:
            if not self.has_story:
                return self
            start, end = float(self.story_start_s or 0.0), float(self.story_end_s or 0.0)
            length = end - start
            fits = (
                story_max_s > 0
                and start >= -TIME_TOLERANCE_S
                and end <= window_s + TIME_TOLERANCE_S
                and story_min_s - TIME_TOLERANCE_S <= length <= story_max_s + TIME_TOLERANCE_S
                and start < self.end_s
                and end > self.start_s
            )
            if not fits:
                self.story_start_s = self.story_end_s = None
            return self

        @model_validator(mode="after")
        def _trim(self) -> Self:
            if self.end_s > window_s + TIME_TOLERANCE_S:
                raise ValueError(f"end_s {self.end_s} is after the end of the window file ({window_s})")
            if self.start_s > spike_floor_s + TIME_TOLERANCE_S:
                raise ValueError(f"start_s {self.start_s} is later than spike_floor_s ({spike_floor_s})")
            length = self.end_s - self.start_s
            if not min_len_s - TIME_TOLERANCE_S <= length <= max_len_s + TIME_TOLERANCE_S:
                raise ValueError(
                    f"clip length {length:.2f} s must be between {min_len_s} and {max_len_s} seconds"
                )
            kept: list[KeywordHighlight] = []
            for item in self.keywords:
                line_tokens = tokens_by_line.get(item.line)
                if line_tokens is None:
                    continue
                words = [word for word in item.words if _contains(line_tokens, _tokens(word))]
                if words:
                    kept.append(KeywordHighlight(line=item.line, words=words))
            self.keywords = kept
            return self

    return Bound


# ── P3 vod_moment_picker ─────────────────────────────────────


class Moment(Reply):
    start_s: float = Field(ge=0.0)
    end_s: float
    moment_type: MomentType
    one_line: str
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("one_line")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return _max_words(value, ONE_LINE_MAX_WORDS, "one_line")

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.start_s >= self.end_s:
            raise ValueError("start_s must be less than end_s")
        return self

    def overlaps(self, other: "Moment") -> bool:
        return self.start_s < other.end_s and other.start_s < self.end_s


class MomentList(Reply):
    moments: list[Moment]


def moment_list_schema(
    n_max: int, len_min_s: float, len_max_s: float, slice_start_s: float, slice_end_s: float
) -> type[MomentList]:
    """`MomentList` with at most `n_max` moments, each inside the slice and the length limits."""

    class Bound(MomentList):
        model_config = ConfigDict(title="MomentList")

        @model_validator(mode="after")
        def _limits(self) -> Self:
            if len(self.moments) > n_max:
                raise ValueError(f"{len(self.moments)} moments given, the maximum is {n_max}")
            for index, moment in enumerate(self.moments):
                length = moment.end_s - moment.start_s
                if not len_min_s - TIME_TOLERANCE_S <= length <= len_max_s + TIME_TOLERANCE_S:
                    raise ValueError(
                        f"moment {index} is {length:.1f} s long, it must be between "
                        f"{len_min_s} and {len_max_s} seconds"
                    )
                if (
                    moment.start_s < slice_start_s - TIME_TOLERANCE_S
                    or moment.end_s > slice_end_s + TIME_TOLERANCE_S
                ):
                    raise ValueError(
                        f"moment {index} is outside the transcript ({slice_start_s} to {slice_end_s} s)"
                    )
            return self

    return Bound


# ── P4 layout_detect ─────────────────────────────────────────


class PixelBox(Reply):
    """A rectangle in whole pixels: top-left corner, width and height."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)

    def scaled(self, factor_x: float, factor_y: float, frame_w: int, frame_h: int) -> "PixelBox":
        """The same rectangle in a frame scaled by the factors, kept inside `frame_w` x `frame_h`."""
        x = min(max(0, round(self.x * factor_x)), frame_w - 1)
        y = min(max(0, round(self.y * factor_y)), frame_h - 1)
        w = min(max(1, round(self.w * factor_x)), frame_w - x)
        h = min(max(1, round(self.h * factor_y)), frame_h - y)
        return PixelBox(x=x, y=y, w=w, h=h)

    def inside(self, frame_w: int, frame_h: int) -> bool:
        return self.x + self.w <= frame_w and self.y + self.h <= frame_h


class Layout(Reply):
    """Where the facecam and the gameplay are. `facecam` is null when there is no webcam."""

    facecam: PixelBox | None = None
    gameplay: PixelBox
    confidence: float = Field(ge=0.0, le=1.0)


def layout_schema(frame_w: int, frame_h: int) -> type[Layout]:
    """`Layout` whose boxes must lie inside a `frame_w` x `frame_h` image."""

    class Bound(Layout):
        model_config = ConfigDict(title="Layout")

        @model_validator(mode="after")
        def _inside(self) -> Self:
            for name, box in (("facecam", self.facecam), ("gameplay", self.gameplay)):
                if box is not None and not box.inside(frame_w, frame_h):
                    raise ValueError(f"{name} box is outside the {frame_w} x {frame_h} image")
            return self

    return Bound


# ── P5 visual_check ──────────────────────────────────────────


class VisualVerdict(Reply):
    """What the vision check saw in the rendered clip."""

    other_watermark: bool
    platform_logos: bool
    menu_or_loading: bool
    pause_or_brb_screen: bool
    empty_facecam: bool
    readable_subtitles: bool
    notes: str = ""

    def problems(self) -> list[str]:
        """Names of the failed checks, in schema order (empty when the clip looks fine)."""
        failed = [
            name
            for name in ("other_watermark", "platform_logos", "menu_or_loading", "pause_or_brb_screen")
            if getattr(self, name)
        ]
        if self.empty_facecam:
            failed.append("empty_facecam")
        if not self.readable_subtitles:
            failed.append("unreadable_subtitles")
        return failed


# ── P10 sentence_ends ────────────────────────────────────────


class SentenceEnds(Reply):
    """The numbers of the transcript words that end a sentence."""

    ends: list[int]


def sentence_ends_schema(word_count: int) -> type[SentenceEnds]:
    """`SentenceEnds` whose numbers must be word numbers of a transcript of `word_count` words."""

    class Bound(SentenceEnds):
        model_config = ConfigDict(title="SentenceEnds")

        @model_validator(mode="after")
        def _in_range(self) -> Self:
            outside = [number for number in self.ends if not 0 <= number < word_count]
            if outside:
                raise ValueError(
                    f"word numbers go from 0 to {word_count - 1}; these are outside: {outside[:10]}"
                )
            self.ends = sorted(set(self.ends))
            return self

    return Bound


# ── P6 permission_read ───────────────────────────────────────


class PermissionVerdict(Reply):
    tier: PermissionTier
    evidence: str | None = Field(default=None, description="Quote of at most 20 words, or null.")

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: str | None) -> str | None:
        if not value:
            return None
        return _max_words(value, EVIDENCE_MAX_WORDS, "evidence")


# ── P7 takedown_classify ─────────────────────────────────────


class TakedownVerdict(Reply):
    is_takedown: bool
    kind: TakedownKind
    creator_handles: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    summary: str = ""

    @field_validator("creator_handles", mode="after")
    @classmethod
    def _handles(cls, value: list[str]) -> list[str]:
        handles = [handle.strip().lstrip("@") for handle in value]
        return [handle for handle in dict.fromkeys(handles) if handle]

    @field_validator("urls", mode="after")
    @classmethod
    def _urls(cls, value: list[str]) -> list[str]:
        return [url for url in dict.fromkeys(url.strip() for url in value) if url]

    @field_validator("platforms", mode="after")
    @classmethod
    def _platforms(cls, value: list[str]) -> list[str]:
        """Known platform names only, lowercase; anything else the model names is dropped."""
        names = [name.strip().lower() for name in value]
        return [name for name in dict.fromkeys(names) if name in PLATFORMS]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.is_takedown:
            self.kind = "not_takedown"
        elif self.kind == "not_takedown":
            self.kind = "other"
        return self


# ── P8 translate_lines ───────────────────────────────────────


class TranslatedSegment(Reply):
    start: float
    end: float
    en: str


class TranslatedSegments(Reply):
    segments: list[TranslatedSegment]


def translated_segments_schema(count: int) -> type[TranslatedSegments]:
    """`TranslatedSegments` that must hold exactly `count` segments (one per source segment)."""

    class Bound(TranslatedSegments):
        model_config = ConfigDict(title="TranslatedSegments")

        @model_validator(mode="after")
        def _count(self) -> Self:
            if len(self.segments) != count:
                raise ValueError(f"{len(self.segments)} segments returned, exactly {count} are needed")
            return self

    return Bound


# ── P11 title_writer ─────────────────────────────────────────


class TitleCandidate(Reply):
    """One candidate title. The title rules are checked by code (`titles.checks`), not here, so a weak
    candidate is dropped instead of costing a repair call."""

    pattern: str = Field(description="stakes, emotion, hot_take, streamer_on_streamer, discovery or fail.")
    title: str = Field(min_length=1)
    facts_used: list[str] = Field(default_factory=list)


class TitleSet(Reply):
    """P11: five candidate titles and the short caption for TikTok and Instagram."""

    candidates: list[TitleCandidate] = Field(min_length=1, max_length=8)
    caption_short: str = Field(default="", description="Under 80 characters, no hashtags.")


# ── P12 cover_hookline (cover engine, docs/29a stage 3) ──────


class CoverLineCandidate(Reply):
    """One cover line. The rules are checked by code (`covers.hookline.validate`), so a weak candidate is
    dropped instead of costing a repair call."""

    text: str = Field(min_length=1, description="1 to 5 words in capitals.")
    style: Literal["quote", "label"]
    key_word: str = Field(default="", description="The one word drawn in yellow.")
    source: str = Field(default="", description="The exact transcript words the line is based on.")


class CoverLines(Reply):
    """P12: candidate cover lines (two quotes and one label)."""

    candidates: list[CoverLineCandidate] = Field(min_length=1, max_length=6)

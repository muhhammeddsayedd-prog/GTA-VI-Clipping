"""Builds the ASS subtitle file (styled, word-timed English subtitles) from a transcript.
Implemented in M06.

The file is authored on the 1080x1920 reference canvas (docs/09 section 4): one Dialogue per word state,
the current word in the accent colour, keywords popping as they are spoken, profanity masked, everything
inside the safe zone. The hook box, LIVE badge and creator credit are events in the same file.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sixstars.factory.bleep import ProfanityMatcher, mask_word, normalize_word
from sixstars.factory.escape import ass_color, ass_field, ass_override_color, ass_text, ass_text_emoji
from sixstars.factory.layout import (
    A_GAMEPLAY_H,
    B_GAMEPLAY_H,
    B_GAMEPLAY_Y,
    BADGE_Y,
    CAPTION_Y_A,
    CAPTION_Y_B,
    HOOK_BOTTOM,
    HOOK_TOP,
    REF_HEIGHT,
    REF_WIDTH,
    LayoutKind,
)
from sixstars.factory.lines import Chunk, Line, chunk_line, collapse_repeats
from sixstars.factory.overlays import (
    BADGE_GAP,
    BADGE_HEIGHT,
    BADGE_TEXT_SIZE,
    CREDIT_OPACITY,
    CREDIT_SIZE,
    LAYER_CAPTION,
    badge_events,
    credit_events,
    dialogue,
    hook_events,
)
from sixstars.factory.params import FactoryParams
from sixstars.factory.style import HookMode, StyleRules
from sixstars.factory.textfit import TextMeasurer, estimate_text_width

WHITE = "#FFFFFF"
BLACK = "#000000"
# docs/09 section 4: keyword pop.
# The thick outline eats most of the font's narrow space, so the gap between words is widened.
WORD_GAP_EXTRA = 10
WORD_GAP = "{\\fsp" + str(WORD_GAP_EXTRA) + "} {\\fsp0}"
KEYWORD_POP = "\\t(0,80,\\fscx118\\fscy118)\\t(80,160,\\fscx100\\fscy100)"


# docs/27: every caption chunk pops in (80% -> 110% -> 100%); a keyword chunk pops harder.
CHUNK_POP = "\\fscx80\\fscy80\\t(0,60,\\fscx110\\fscy110)\\t(60,120,\\fscx100\\fscy100)"
CHUNK_KEYWORD_POP = "\\fscx80\\fscy80\\t(0,70,\\fscx125\\fscy125)\\t(70,150,\\fscx100\\fscy100)"
# docs/27: the payoff emoji, centred on the gameplay, pops in big for 1.5 s.
PAYOFF_EMOJI_SIZE = 230
PAYOFF_EMOJI_S = 1.5
PAYOFF_POP = "\\fscx30\\fscy30\\t(0,120,\\fscx115\\fscy115)\\t(120,200,\\fscx100\\fscy100)"
LAYER_PAYOFF = 8


@dataclass(frozen=True, slots=True)
class SubtitleStyle:
    """Caption look. The defaults are the classic look of docs/09 section 4 (size 78, outline 6, soft 50%
    shadow 3, bold, whole lines); `from_rules` gives the chunked look of docs/27.

    `chunk_words` 0 shows whole lines with the spoken word highlighted; otherwise captions are chunks of at
    most `chunk_words` words and `chunk_chars` characters. `font` empty means `factory.font`.
    """

    size: int = 78
    outline: int = 6
    shadow: int = 3
    shadow_alpha: int = 0x80
    margin_x: int = 40
    min_size: int = 48
    hold_s: float = 0.3
    credit_gap: int = 14
    chunk_words: int = 0
    chunk_chars: int = 22
    caps: bool = False
    font: str = ""

    @classmethod
    def from_rules(cls, rules: StyleRules) -> "SubtitleStyle":
        """The caption look for a `factory.style`: classic for whole lines, else heavy capitals with a thick
        outline and a hard drop shadow."""
        if not rules.chunked:
            return cls()
        size = rules.caption_size
        return cls(
            size=size,
            outline=max(4, round(size * 0.07)),
            shadow=max(2, round(size * 0.045)),
            shadow_alpha=0x00,
            min_size=max(40, size // 2),
            hold_s=0.25,
            chunk_words=rules.caption_words,
            chunk_chars=rules.caption_chars,
            caps=rules.caption_caps,
            font=rules.caption_font,
        )


@dataclass(frozen=True, slots=True)
class SafeZone:
    """Text limits on the reference canvas: nothing above `top`, nothing below `bottom`."""

    top: float
    bottom: float


def safe_zone(params: FactoryParams) -> SafeZone:
    """`factory.safe_top_px` / `factory.safe_bottom_px` converted to the 1080x1920 reference canvas."""
    scale = REF_HEIGHT / params.height
    return SafeZone(params.safe_top_px * scale, REF_HEIGHT - params.safe_bottom_px * scale)


def caption_bottom(layout: LayoutKind, params: FactoryParams, style: SubtitleStyle | None = None) -> float:
    """Y of the caption's bottom edge (captions are bottom-anchored, so a wrapped line grows upwards).

    A one-line caption is centred on y=1150 (layout A) or y=1450 (layout B), then pushed up if its outline
    and shadow would cross the bottom of the safe zone.
    """
    style = style or SubtitleStyle()
    zone = safe_zone(params)
    centre = CAPTION_Y_A if layout == "A" else CAPTION_Y_B
    bottom = min(centre + style.size / 2, zone.bottom - style.outline - style.shadow)
    return max(bottom, zone.top + style.size + style.outline)


def credit_bottom(layout: LayoutKind, params: FactoryParams, style: SubtitleStyle | None = None) -> float:
    """Y of the credit's bottom edge: just above a one-line caption, at the bottom of the gameplay area."""
    style = style or SubtitleStyle()
    return caption_bottom(layout, params, style) - style.size - style.outline - style.credit_gap


def keyword_flags(words: Sequence[str], keywords: Sequence[str]) -> list[bool]:
    """Marks the words of a line that belong to a keyword; a multi-word keyword marks consecutive words."""
    normal = [normalize_word(word) for word in words]
    flags = [False] * len(words)
    for keyword in keywords:
        tokens = [token for token in (normalize_word(part) for part in keyword.split()) if token]
        if not tokens:
            continue
        for i in range(len(normal) - len(tokens) + 1):
            if normal[i : i + len(tokens)] == tokens:
                for j in range(i, i + len(tokens)):
                    flags[j] = True
    return flags


def _caption_text(
    words: Sequence[str], current: int, keyword: Sequence[bool], accent: str, prefix: str
) -> str:
    white = ass_override_color(WHITE)
    parts: list[str] = []
    for i, word in enumerate(words):
        escaped = ass_text(word)
        if i == current and keyword[i]:
            parts.append(f"{{\\1c{accent}{KEYWORD_POP}}}{escaped}{{\\1c{white}\\fscx100\\fscy100}}")
        elif i == current or (keyword[i] and i < current):
            parts.append(f"{{\\1c{accent}}}{escaped}{{\\1c{white}}}")
        else:
            parts.append(escaped)
    return prefix + WORD_GAP.join(parts)


def caption_events(
    lines: Sequence[Line],
    *,
    layout: LayoutKind,
    params: FactoryParams,
    duration_s: float,
    keywords: Mapping[int, Sequence[str]] | None = None,
    profanity: ProfanityMatcher | None = None,
    style: SubtitleStyle | None = None,
    measure: TextMeasurer = estimate_text_width,
) -> list[str]:
    """Caption Dialogue lines: one per word state.

    `lines` are in clip time (see `lines.clip_lines`). The spoken word is in the accent colour; a keyword
    pops when spoken and stays in the accent colour until the line ends. Profanity is masked when
    `factory.bleep` is on. A line too wide for the frame gets a smaller font so it stays on one row.
    """
    style = style or SubtitleStyle()
    keywords = keywords or {}
    if style.chunk_words > 0:
        return chunk_events(
            lines,
            layout=layout,
            params=params,
            duration_s=duration_s,
            keywords=keywords,
            profanity=profanity,
            style=style,
            measure=measure,
        )
    accent = ass_override_color(params.accent_color)
    bottom = round(caption_bottom(layout, params, style))
    max_width = REF_WIDTH - 2 * style.margin_x
    events: list[str] = []
    ordered = sorted(lines, key=lambda line: line.start_s)
    for n, line in enumerate(ordered):
        texts = [word.text for word in line.words]
        if params.bleep and profanity is not None:
            for i in profanity.find(texts):
                texts[i] = mask_word(texts[i])
        flags = keyword_flags([word.text for word in line.words], keywords.get(line.index, ()))
        width = measure(" ".join(texts), style.size) + WORD_GAP_EXTRA * (len(texts) - 1)
        size = style.size if width <= max_width else max(style.min_size, int(style.size * max_width / width))
        prefix = (
            f"{{\\an2\\pos({REF_WIDTH // 2},{bottom})" + (f"\\fs{size}" if size != style.size else "") + "}"
        )
        line_end = min(line.end_s + style.hold_s, duration_s)
        if n + 1 < len(ordered):
            line_end = min(line_end, ordered[n + 1].start_s)
        for i, word in enumerate(line.words):
            start = max(0.0, word.start_s)
            end = line.words[i + 1].start_s if i + 1 < len(line.words) else line_end
            end = min(end, duration_s)
            if round(end * 100) <= round(start * 100):
                continue
            events.append(
                dialogue(LAYER_CAPTION, start, end, "Caption", _caption_text(texts, i, flags, accent, prefix))
            )
    return events


def _display_words(line: Line, params: FactoryParams, profanity: ProfanityMatcher | None) -> list[str]:
    texts = [word.text for word in line.words]
    if params.bleep and profanity is not None:
        for i in profanity.find(texts):
            texts[i] = mask_word(texts[i])
    return texts


def chunk_events(
    lines: Sequence[Line],
    *,
    layout: LayoutKind,
    params: FactoryParams,
    duration_s: float,
    keywords: Mapping[int, Sequence[str]],
    profanity: ProfanityMatcher | None,
    style: SubtitleStyle,
    measure: TextMeasurer = estimate_text_width,
) -> list[str]:
    """Caption Dialogue lines in the chunked look of docs/27: one event per chunk of one or two words.

    Each chunk pops in when its first word is spoken and stays until the next chunk (or `hold_s` after its
    last word). Keyword words are in the accent colour and their chunk pops harder; a chunk said again
    straight away shows once with `X2`. Profanity is masked when `factory.bleep` is on, and a chunk too
    wide for the frame gets a smaller font.
    """
    accent = ass_override_color(params.accent_color)
    white = ass_override_color(WHITE)
    bottom = round(caption_bottom(layout, params, style))
    max_width = REF_WIDTH - 2 * style.margin_x
    ordered = sorted(lines, key=lambda line: line.start_s)
    shown = {line.index: _display_words(line, params, profanity) for line in ordered}
    flags = {
        line.index: keyword_flags([word.text for word in line.words], keywords.get(line.index, ()))
        for line in ordered
    }
    chunks: list[Chunk] = []
    for line in ordered:
        chunks += chunk_line(line, style.chunk_words, style.chunk_chars)
    chunks = collapse_repeats(ordered, chunks)
    events: list[str] = []
    for n, chunk in enumerate(chunks):
        start = max(0.0, chunk.start_s)
        end = min(chunk.end_s + style.hold_s, duration_s)
        if n + 1 < len(chunks):
            end = min(end, chunks[n + 1].start_s)
        if round(end * 100) <= round(start * 100):
            continue
        texts = [shown[chunk.line_index][i] for i in chunk.words]
        if style.caps:
            texts = [text.upper() for text in texts]
        if chunk.repeat > 1:
            texts[-1] = f"{texts[-1]} X{chunk.repeat}"
        marked = [flags[chunk.line_index][i] for i in chunk.words]
        width = measure(" ".join(texts), style.size) + WORD_GAP_EXTRA * (len(texts) - 1)
        size = style.size if width <= max_width else max(style.min_size, int(style.size * max_width / width))
        pop = CHUNK_KEYWORD_POP if any(marked) else CHUNK_POP
        prefix = (
            f"{{\\an2\\pos({REF_WIDTH // 2},{bottom})"
            + (f"\\fs{size}" if size != style.size else "")
            + pop
            + "}"
        )
        parts = [
            f"{{\\1c{accent}}}{ass_text(text)}{{\\1c{white}}}" if keyword else ass_text(text)
            for text, keyword in zip(texts, marked, strict=True)
        ]
        events.append(dialogue(LAYER_CAPTION, start, end, "Caption", prefix + WORD_GAP.join(parts)))
    return events


def payoff_events(emoji: str, *, payoff_s: float, duration_s: float, layout: LayoutKind) -> list[str]:
    """The payoff emoji (docs/27): big, centred on the gameplay, popping in at `payoff_s` for 1.5 s."""
    text = " ".join(emoji.split())
    if not text or not 0 <= payoff_s < duration_s:
        return []
    centre_y = A_GAMEPLAY_H // 2 if layout == "A" else B_GAMEPLAY_Y + B_GAMEPLAY_H // 2
    end = min(payoff_s + PAYOFF_EMOJI_S, duration_s)
    tags = f"{{\\an5\\pos({REF_WIDTH // 2},{centre_y})\\fs{PAYOFF_EMOJI_SIZE}{PAYOFF_POP}}}"
    return [dialogue(LAYER_PAYOFF, payoff_s, end, "Caption", tags + ass_text_emoji(text))]


def hook_box_top(params: FactoryParams, *, live_badge: bool) -> float:
    """Top of the hook box on the reference canvas: below the LIVE badge on spike clips, inside the safe
    zone."""
    zone = safe_zone(params)
    badge_top = max(float(BADGE_Y), zone.top)
    top = max(float(HOOK_TOP), zone.top)
    if live_badge:
        top = max(top, badge_top + BADGE_HEIGHT + BADGE_GAP)
    return min(top, zone.bottom - (HOOK_BOTTOM - HOOK_TOP))


def title_bar_top(params: FactoryParams, *, live_badge: bool) -> int:
    """Top of the docs/27 title bar on the reference canvas: the top of the safe zone (about where the
    reference puts it), or below the LIVE badge on spike clips."""
    zone = safe_zone(params)
    top = zone.top
    if live_badge:
        top = max(top, max(float(BADGE_Y), zone.top) + BADGE_HEIGHT + BADGE_GAP)
    return round(top)


def hook_box_bottom(params: FactoryParams, *, live_badge: bool) -> float:
    """Bottom of the hook box on the reference canvas."""
    return hook_box_top(params, live_badge=live_badge) + (HOOK_BOTTOM - HOOK_TOP)


def _style(
    name: str, font: str, size: int, primary: str, outline_color: str, back: str, border: str, margin: int
) -> str:
    """One style row; `border` is `outline,shadow,alignment`."""
    return (
        f"Style: {name},{font},{size},{primary},{primary},{outline_color},{back},-1,0,0,0,100,100,0,0,1,"
        f"{border},{margin},{margin},0,1"
    )


def _styles(params: FactoryParams, style: SubtitleStyle) -> list[str]:
    font = ass_field(params.font)
    white, black = ass_color(WHITE), ass_color(BLACK)
    credit_alpha = round(255 * (1 - CREDIT_OPACITY))
    soft_black = ass_color(BLACK, alpha=style.shadow_alpha)
    fmt = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    caption_border = f"{style.outline},{style.shadow},2"
    caption_font = ass_field(style.font) if style.font else font
    return [
        fmt,
        _style("Caption", caption_font, style.size, white, black, soft_black, caption_border, style.margin_x),
        _style("Hook", font, 56, black, white, black, "0,0,5", 0),
        _style("Badge", font, BADGE_TEXT_SIZE, white, black, black, "0,0,4", 0),
        _style(
            "Credit",
            font,
            CREDIT_SIZE,
            ass_color(WHITE, alpha=credit_alpha),
            ass_color(BLACK, alpha=credit_alpha),
            soft_black,
            "2,1,1",
            0,
        ),
    ]


def build_ass(
    lines: Sequence[Line],
    *,
    layout: LayoutKind,
    params: FactoryParams,
    duration_s: float,
    keywords: Mapping[int, Sequence[str]] | None = None,
    profanity: ProfanityMatcher | None = None,
    hook: str | None = None,
    creator: str | None = None,
    live_badge: bool = False,
    style: SubtitleStyle | None = None,
    measure: TextMeasurer = estimate_text_width,
    hook_mode: HookMode = "box",
    payoff_s: float | None = None,
    payoff_emoji: str | None = None,
    caption_measure: TextMeasurer | None = None,
) -> str:
    """The complete `.ass` file for one clip: captions plus hook box, LIVE badge, creator credit and the
    payoff emoji.

    `duration_s` is the clip length without the end card; nothing is drawn after it. On spike-lane clips
    (`live_badge`) the hook box moves down so it clears the badge. With `hook_mode` `title_bar` the hook
    stays for the whole clip instead of `factory.hook_seconds`. `payoff_s` is in clip time.
    """
    style = style or SubtitleStyle()
    zone = safe_zone(params)
    badge_top = max(float(BADGE_Y), zone.top)
    hook_top = hook_box_top(params, live_badge=live_badge)
    events: list[str] = []
    if creator:
        events += credit_events(creator, end_s=duration_s, bottom=credit_bottom(layout, params, style))
    events += caption_events(
        lines,
        layout=layout,
        params=params,
        duration_s=duration_s,
        keywords=keywords,
        profanity=profanity,
        style=style,
        measure=caption_measure or measure,
    )
    if live_badge:
        events += badge_events(end_s=duration_s, top=badge_top, measure=measure)
    if hook:
        hook_end = duration_s if hook_mode == "title_bar" else min(params.hook_seconds, duration_s)
        events += hook_events(hook, end_s=hook_end, top=hook_top, measure=measure)
    if payoff_s is not None and payoff_emoji:
        events += payoff_events(payoff_emoji, payoff_s=payoff_s, duration_s=duration_s, layout=layout)
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {REF_WIDTH}",
        f"PlayResY: {REF_HEIGHT}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        *_styles(params, style),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    return "\n".join([*header, *events]) + "\n"

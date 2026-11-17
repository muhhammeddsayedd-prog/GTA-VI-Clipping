"""Builds the ASS subtitle file (styled, word-timed English subtitles) from a transcript.
Implemented in M06.

The file is authored on the 1080x1920 reference canvas (docs/09 section 4): one Dialogue per word state,
the current word in the accent colour, keywords popping as they are spoken, profanity masked, everything
inside the safe zone. The hook box, LIVE badge and creator credit are events in the same file.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sixstars.factory.bleep import ProfanityMatcher, mask_word, normalize_word
from sixstars.factory.escape import ass_color, ass_field, ass_override_color, ass_text
from sixstars.factory.layout import (
    BADGE_Y,
    CAPTION_Y_A,
    CAPTION_Y_B,
    HOOK_BOTTOM,
    HOOK_TOP,
    REF_HEIGHT,
    REF_WIDTH,
    LayoutKind,
)
from sixstars.factory.lines import Line
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
from sixstars.factory.textfit import TextMeasurer, estimate_text_width

WHITE = "#FFFFFF"
BLACK = "#000000"
# docs/09 section 4: keyword pop.
# The thick outline eats most of the font's narrow space, so the gap between words is widened.
WORD_GAP_EXTRA = 10
WORD_GAP = "{\\fsp" + str(WORD_GAP_EXTRA) + "} {\\fsp0}"
KEYWORD_POP = "\\t(0,80,\\fscx118\\fscy118)\\t(80,160,\\fscx100\\fscy100)"


@dataclass(frozen=True, slots=True)
class SubtitleStyle:
    """Caption look fixed by docs/09 section 4 (size 78, outline 6, soft 50% shadow 3, bold)."""

    size: int = 78
    outline: int = 6
    shadow: int = 3
    shadow_alpha: int = 0x80
    margin_x: int = 40
    min_size: int = 48
    hold_s: float = 0.3
    credit_gap: int = 14


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
    return [
        fmt,
        _style("Caption", font, style.size, white, black, soft_black, caption_border, style.margin_x),
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
) -> str:
    """The complete `.ass` file for one clip: captions plus hook box, LIVE badge and creator credit.

    `duration_s` is the clip length without the end card; nothing is drawn after it. On spike-lane clips
    (`live_badge`) the hook box moves down so it clears the badge.
    """
    style = style or SubtitleStyle()
    zone = safe_zone(params)
    badge_top = max(float(BADGE_Y), zone.top)
    hook_top = max(float(HOOK_TOP), zone.top)
    if live_badge:
        hook_top = max(hook_top, badge_top + BADGE_HEIGHT + BADGE_GAP)
    hook_top = min(hook_top, zone.bottom - (HOOK_BOTTOM - HOOK_TOP))
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
        measure=measure,
    )
    if live_badge:
        events += badge_events(end_s=duration_s, top=badge_top, measure=measure)
    if hook:
        events += hook_events(hook, end_s=min(params.hook_seconds, duration_s), top=hook_top, measure=measure)
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

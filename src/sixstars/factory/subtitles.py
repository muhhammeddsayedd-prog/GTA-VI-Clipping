"""Builds the ASS subtitle file (styled, word-timed English subtitles) from a transcript.
Implemented in M06.

The file is authored on the 1080x1920 reference canvas (docs/09 section 4): one Dialogue per word state,
the current word in the accent colour, keywords popping as they are spoken, profanity masked, everything
inside the safe zone. The hook box, LIVE badge and creator credit are events in the same file.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from sixstars.factory.bleep import ProfanityMatcher, mask_word, normalize_word
from sixstars.factory.escape import ass_color, ass_field, ass_override_color, ass_text, ass_text_emoji
from sixstars.factory.layout import (
    A_GAMEPLAY_H,
    B_GAMEPLAY_H,
    B_GAMEPLAY_Y,
    BADGE_Y,
    CAPTION_Y_A,
    CAPTION_Y_B,
    CAPTION_Y_C,
    H_VIDEO_H,
    H_VIDEO_Y,
    HOOK_BOTTOM,
    HOOK_TOP,
    LOW_CREDIT_BOTTOM,
    M_CREDIT_BOTTOM,
    M_GAME_H,
    M_SEAM_Y,
    M_TOP,
    P_VIDEO_H,
    P_VIDEO_Y,
    PAYOFF_Y_C,
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
    # M18 Phase C: the caption's centre (0: the layout's) and the spoken word's colour (empty: none).
    centre_y: int = 0
    active_color: str = ""
    # Phase E: two-tier chunks (a keyword big, italic and glowing; the other words small above it).
    tiers: bool = False
    # Cover-ready video (`cover.video_ready`): in these clip-time spans the chunks of a line are shown as
    # one still phrase of at most `phrase_words` words and `phrase_chars` characters, on one row.
    phrase_windows: tuple[tuple[float, float], ...] = ()
    phrase_words: int = 6
    phrase_chars: int = 26

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
            centre_y=rules.caption_y,
            active_color=rules.caption_active_color,
            tiers=rules.caption_tiers,
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

    A one-line caption is centred on y=1150 (layout A), y=1450 (layout B) or y=1400 (layout C), then pushed
    up if its outline and shadow would cross the bottom of the safe zone.
    """
    style = style or SubtitleStyle()
    zone = safe_zone(params)
    centre = (
        style.centre_y
        or {
            "A": CAPTION_Y_A,
            "B": CAPTION_Y_B,
            "C": CAPTION_Y_C,
            "H": CAPTION_Y_B,
            "P": CAPTION_Y_B,
            "M": M_SEAM_Y,
        }[layout]
    )
    bottom = min(centre + style.size / 2, zone.bottom - style.outline - style.shadow)
    return max(bottom, zone.top + style.size + style.outline)


def credit_bottom(layout: LayoutKind, params: FactoryParams, style: SubtitleStyle | None = None) -> float:
    """Y of the credit's bottom edge: just above a one-line caption, at the bottom of the gameplay area;
    in layouts H and P (no captions) at `LOW_CREDIT_BOTTOM`, under the stream or on the gameplay."""
    if layout in ("H", "P"):
        return float(LOW_CREDIT_BOTTOM)
    if layout == "M":
        return float(M_CREDIT_BOTTOM)
    # The credit keeps the layout's place even when a style moves its captions (`centre_y`).
    style = replace(style, centre_y=0) if style is not None else SubtitleStyle()
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


def _overlaps(start: float, end: float, windows: Sequence[tuple[float, float]]) -> bool:
    return any(start < w_end and w_start < max(end, start + 0.01) for w_start, w_end in windows)


def phrase_chunks(
    chunks: Sequence[Chunk],
    *,
    windows: Sequence[tuple[float, float]],
    max_words: int,
    max_chars: int,
    text_of: Callable[[int, tuple[int, ...]], str],
    fits: Callable[[str], bool],
) -> list[Chunk]:
    """Cover-ready captions: a chunk that overlaps a window starts a phrase that takes the next chunks of
    the same line while the phrase stays within `max_words`, `max_chars` and one row (`fits`). Repeated
    chunks (`X2`) and chunks outside the windows are kept as they are. Words are never split. Pure."""
    out: list[Chunk] = []
    i = 0
    while i < len(chunks):
        chunk = chunks[i]
        if not windows or chunk.repeat > 1 or not _overlaps(chunk.start_s, chunk.end_s, windows):
            out.append(chunk)
            i += 1
            continue
        words, end, j = chunk.words, chunk.end_s, i + 1
        while j < len(chunks):
            following = chunks[j]
            if following.line_index != chunk.line_index or following.repeat > 1:
                break
            candidate = words + following.words
            text = text_of(chunk.line_index, candidate)
            if len(candidate) > max_words or len(text) > max_chars or not fits(text):
                break
            words, end, j = candidate, following.end_s, j + 1
        out.append(Chunk(chunk.line_index, words, chunk.start_s, end, 1, phrase=True))
        i = j
    return out


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
    by_index = {line.index: line for line in ordered}
    flags = {
        line.index: keyword_flags([word.text for word in line.words], keywords.get(line.index, ()))
        for line in ordered
    }
    chunks: list[Chunk] = []
    for line in ordered:
        chunks += chunk_line(line, style.chunk_words, style.chunk_chars)
    chunks = collapse_repeats(ordered, chunks)
    if style.phrase_windows:

        def text_of(line_index: int, words: tuple[int, ...]) -> str:
            return " ".join(shown[line_index][i] for i in words)

        def fits(text: str) -> bool:
            gaps = WORD_GAP_EXTRA * text.count(" ")
            return measure(text.upper() if style.caps else text, style.min_size) + gaps <= max_width

        chunks = phrase_chunks(
            chunks,
            windows=style.phrase_windows,
            max_words=style.phrase_words,
            max_chars=style.phrase_chars,
            text_of=text_of,
            fits=fits,
        )
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
        place = f"{{\\an2\\pos({REF_WIDTH // 2},{bottom})" + (f"\\fs{size}" if size != style.size else "")
        if chunk.phrase:
            # Cover-ready phrase: whole and still (no pop, no word-by-word colour), keywords in the accent.
            still = [
                f"{{\\1c{accent}}}{ass_text(text)}{{\\1c{white}}}" if keyword else ass_text(text)
                for text, keyword in zip(texts, marked, strict=True)
            ]
            events.append(dialogue(LAYER_CAPTION, start, end, "Caption", place + "}" + WORD_GAP.join(still)))
            continue
        if style.tiers and any(marked):
            # Two tiers: the keyword words big, italic and glowing; the others small above them.
            main = WORD_GAP.join(ass_text(t) for t, m in zip(texts, marked, strict=True) if m)
            lead = WORD_GAP.join(ass_text(t) for t, m in zip(texts, marked, strict=True) if not m)
            # A glow is two layers: the keyword's accent halo (a wide, blurred, half-transparent outline) and
            # the crisp white keyword with a thin dark outline over it.
            halo = (
                f"\\i1\\1c{accent}\\3c{accent}\\3a&H40&\\bord{max(6, size // 10)}"
                f"\\blur{max(8, size // 8)}\\shad0"
            )
            crisp = f"\\i1\\1c{white}\\3c{ass_override_color(BLACK)}\\bord{max(2, size // 40)}\\shad0"
            lead_size = max(style.min_size // 2, round(size * 0.42))
            lead_place = f"{{\\an2\\pos({REF_WIDTH // 2},{bottom - size})\\fs{lead_size}"
            events.append(dialogue(LAYER_CAPTION, start, end, "Caption", place + pop + halo + "}" + main))
            events.append(
                dialogue(LAYER_CAPTION + 1, start, end, "Caption", place + pop + crisp + "}" + main)
            )
            if lead:
                events.append(dialogue(LAYER_CAPTION, start, end, "Caption", lead_place + pop + "}" + lead))
            continue
        if style.active_color:
            # The spoken word in the active colour: one event per word of the chunk, the pop on the first.
            active = ass_override_color(style.active_color)
            line = by_index[chunk.line_index]
            for k, word_index in enumerate(chunk.words):
                word_start = start if k == 0 else max(start, line.words[word_index].start_s)
                word_end = end
                if k + 1 < len(chunk.words):
                    word_end = min(end, max(word_start, line.words[chunk.words[k + 1]].start_s))
                if round(word_end * 100) <= round(word_start * 100):
                    continue
                parts = [
                    f"{{\\1c{active}}}{ass_text(text)}{{\\1c{white}}}" if j == k else ass_text(text)
                    for j, text in enumerate(texts)
                ]
                tags = place + (pop if k == 0 else "") + "}"
                events.append(
                    dialogue(LAYER_CAPTION, word_start, word_end, "Caption", tags + WORD_GAP.join(parts))
                )
            continue
        parts = [
            f"{{\\1c{accent}}}{ass_text(text)}{{\\1c{white}}}" if keyword else ass_text(text)
            for text, keyword in zip(texts, marked, strict=True)
        ]
        events.append(
            dialogue(LAYER_CAPTION, start, end, "Caption", place + pop + "}" + WORD_GAP.join(parts))
        )
    return events


def payoff_events(emoji: str, *, payoff_s: float, duration_s: float, layout: LayoutKind) -> list[str]:
    """The payoff emoji (docs/27): big, centred on the gameplay, popping in at `payoff_s` for 1.5 s."""
    text = " ".join(emoji.split())
    if not text or not 0 <= payoff_s < duration_s:
        return []
    centre_y = {
        "A": A_GAMEPLAY_H // 2,
        "B": B_GAMEPLAY_Y + B_GAMEPLAY_H // 2,
        "C": PAYOFF_Y_C,
        "H": H_VIDEO_Y + H_VIDEO_H // 2,
        "P": P_VIDEO_Y + P_VIDEO_H // 2,
        "M": M_TOP + M_GAME_H // 2,
    }[layout]
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
    captions: bool = True,
    hook_top: float | None = None,
    hook_whole_clip: bool = False,
) -> str:
    """The complete `.ass` file for one clip: captions plus hook box, LIVE badge, creator credit and the
    payoff emoji.

    `duration_s` is the clip length without the end card; nothing is drawn after it. On spike-lane clips
    (`live_badge`) the hook box moves down so it clears the badge. The hook shows for the first
    `factory.hook_seconds` in every `hook_mode` (the whole clip with `hook_whole_clip`), its box at
    `hook_top` when given. `payoff_s` is in clip time. Without `captions` the spoken words are not shown (a
    title-only style).
    """
    style = style or SubtitleStyle()
    zone = safe_zone(params)
    badge_top = max(float(BADGE_Y), zone.top)
    if hook_top is None:
        hook_top = hook_box_top(params, live_badge=live_badge)
    events: list[str] = []
    if creator:
        events += credit_events(creator, end_s=duration_s, bottom=credit_bottom(layout, params, style))
    if captions:
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
        hook_end = duration_s if hook_whole_clip else min(params.hook_seconds, duration_s)
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

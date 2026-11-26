"""The ASS overlay of one 16:9 segment (M20 L3): captions, the `#N` badge, the creator credit and the mark.

Everything drawn on a segment lives in this one file, so the segment has a single overlay filter. Positions
are in a PlayRes equal to the canvas; every size and time comes from setting `longform.style`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sixstars.factory.bleep import ProfanityMatcher, mask_word
from sixstars.factory.escape import ass_color, ass_field, ass_text
from sixstars.factory.lines import Word
from sixstars.factory.overlays import dialogue

WHITE, BLACK = "#FFFFFF", "#000000"
STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
    "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
    "Alignment, MarginL, MarginR, MarginV, Encoding"
)


@dataclass(frozen=True)
class OverlayStyle:
    """Setting `longform.style`."""

    caption_font: str = "Inter"
    caption_size: int = 54
    caption_margin_v: int = 70
    caption_max_chars: int = 42
    caption_max_lines: int = 2
    caption_gap_s: float = 0.35
    badge_font: str = "Anton"
    badge_size: int = 84
    badge_s: float = 2.0
    credit_font: str = "Inter"
    credit_size: int = 34
    credit_s: float = 3.0
    mark_text: str = "6 Stars Clips"
    mark_size: int = 26
    mark_opacity: float = 0.45
    margin: int = 48
    title_font: str = "Anton"
    title_size: int = 96
    cold_open_text: str = "#1 is insane. Stay till the end."
    end_card_text: str = "New GTA 6 TOP moments every day"
    thumb_text_size: int = 150

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "OverlayStyle":
        known = {k: v for k, v in value.items() if k in cls.__dataclass_fields__}
        base = cls()
        return cls(**{**base.__dict__, **known})


def _style(name: str, font: str, size: int, primary: str, back: str, border: str, align: int, mv: int) -> str:
    """`border` is `outline,shadow`; `align` is the ASS numpad alignment."""
    outline = ass_color(BLACK)
    return (
        f"Style: {name},{ass_field(font)},{size},{primary},{primary},{outline},{back},-1,0,0,0,100,100,0,0,1,"
        f"{border},{align},0,0,{mv},1"
    )


def caption_chunks(
    words: Sequence[Word], style: OverlayStyle, profanity: ProfanityMatcher | None = None
) -> list[tuple[float, float, str]]:
    """Group words into on-screen captions: a new caption at a pause or when the text would pass
    `caption_max_chars` x `caption_max_lines`. Profane words are masked (the audio is bleeped elsewhere)."""
    texts = [w.text for w in words]
    masked = set(profanity.find(texts)) if profanity else set()
    limit = style.caption_max_chars * style.caption_max_lines
    chunks: list[tuple[float, float, str]] = []
    current: list[tuple[Word, str]] = []

    def flush() -> None:
        if current:
            text = " ".join(t for _, t in current).strip()
            if text:
                chunks.append((current[0][0].start_s, current[-1][0].end_s, text))
            current.clear()

    for i, w in enumerate(words):
        shown = mask_word(w.text) if i in masked else w.text
        if current:
            gap = w.start_s - current[-1][0].end_s
            length = len(" ".join(t for _, t in current)) + 1 + len(shown)
            if gap >= style.caption_gap_s or length > limit:
                flush()
        current.append((w, shown))
    flush()
    return chunks


def wrap(text: str, max_chars: int) -> str:
    """Break a caption into balanced lines of at most `max_chars` (ASS `\\N`)."""
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > max_chars:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return "\\N".join(ass_text(part) for part in lines)


def build_segment_ass(
    *,
    words: Sequence[Word],
    duration_s: float,
    position: int,
    creator_label: str,
    style: OverlayStyle,
    width: int = 1920,
    height: int = 1080,
    profanity: ProfanityMatcher | None = None,
) -> str:
    """The whole ASS file of one segment (segment time, 0 = first frame), PlayRes = the canvas."""
    white = ass_color(WHITE)
    soft = ass_color(BLACK, alpha=96)
    mark_alpha = round(255 * (1 - style.mark_opacity))
    styles = [
        STYLE_FORMAT,
        _style(
            "Caption", style.caption_font, style.caption_size, white, soft, "4,2", 2, style.caption_margin_v
        ),
        _style("Badge", style.badge_font, style.badge_size, white, soft, "5,3", 7, 0),
        _style("Credit", style.credit_font, style.credit_size, white, soft, "3,2", 1, 0),
        _style(
            "Mark", style.credit_font, style.mark_size, ass_color(WHITE, alpha=mark_alpha), soft, "1,0", 9, 0
        ),
    ]
    end = max(duration_s, 0.1)
    events: list[str] = []
    for start, stop, text in caption_chunks(words, style, profanity):
        events.append(
            dialogue(0, start, min(stop + 0.15, end), "Caption", wrap(text, style.caption_max_chars))
        )
    m = style.margin
    events.append(
        dialogue(1, 0, min(style.badge_s, end), "Badge", f"{{\\pos({m},{m})}}{ass_text(f'#{position}')}")
    )
    if creator_label:
        credit_y = height - m
        events.append(
            dialogue(
                1,
                0,
                min(style.credit_s, end),
                "Credit",
                f"{{\\pos({m},{credit_y})}}{ass_text(creator_label)}",
            )
        )
    if style.mark_text:
        mark = f"{{\\pos({width - m},{m})}}{ass_text(style.mark_text)}"
        events.append(dialogue(1, 0, end, "Mark", mark))
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    return "\n".join([*header, *events]) + "\n"


def creator_label(platform: str, handle: str) -> str:
    """`twitch.tv/name` or `youtube.com/@name` for the lower-third credit."""
    name = handle.strip().lstrip("@")
    if not name:
        return ""
    return f"twitch.tv/{name}" if platform == "twitch" else f"youtube.com/@{name}"


def build_title_ass(text: str, *, duration_s: float, style: OverlayStyle, width: int, height: int) -> str:
    """A one-line centred title for the cold open and the end card (plus the brand mark)."""
    white = ass_color(WHITE)
    soft = ass_color(BLACK, alpha=96)
    styles = [
        STYLE_FORMAT,
        _style("Title", style.title_font, style.title_size, white, soft, "6,3", 5, 0),
        _style("Mark", style.credit_font, style.mark_size, white, soft, "1,0", 9, 0),
    ]
    title = f"{{\\pos({width // 2},{height // 2})}}{wrap(text, 28)}"
    events = [dialogue(0, 0, duration_s, "Title", title)]
    if style.mark_text:
        mark = f"{{\\pos({width - style.margin},{style.margin})}}{ass_text(style.mark_text)}"
        events.append(dialogue(1, 0, duration_s, "Mark", mark))
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    return "\n".join([*header, *events]) + "\n"

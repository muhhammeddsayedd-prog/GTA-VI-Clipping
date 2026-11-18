"""The hook as a TikTok-style title bar (docs/27): black text in a regular-weight sans on a tight white
rectangle, colour emoji inline, at the top of the frame for the whole clip.

libass cannot draw colour emoji, so the bar is drawn with Pillow into a transparent PNG on the 1080x1920
reference canvas and laid over the video by ffmpeg. The layout (`fit_title`) is pure; `draw_title_bar`
writes the image.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from sixstars.factory.escape import EMOJI_RUN, single_line

# Measured on the reference (docs/27 V3), on the 1080x1920 canvas.
TITLE_SIZES = (64, 60, 56, 52, 48, 44)
TITLE_MAX_LINES = 2
TITLE_MAX_WIDTH = 700  # widest the white box may be: TikTok's text box wraps at about 65% of the frame
TITLE_PAD_X = 14
TITLE_PAD_Y = 8
TITLE_LINE_HEIGHT = 1.18  # line pitch as a multiple of the font size
TITLE_RADIUS = 6
TITLE_WEIGHT = 420  # TikTok Sans is variable; the reference is a regular weight
EMOJI_SCALE = 1.0  # emoji height as a multiple of the font size
# CBDT colour emoji fonts (Noto Color Emoji) only exist at this pixel size; they are scaled after drawing.
CBDT_SIZE = 109

TextWidth = Callable[[str, int], float]
Run = tuple[str, bool]  # (text, is_emoji)


def split_runs(text: str) -> list[Run]:
    """The text cut into alternating plain and emoji runs."""
    runs: list[Run] = []
    position = 0
    for match in EMOJI_RUN.finditer(text):
        if match.start() > position:
            runs.append((text[position : match.start()], False))
        runs.append((match.group(0), True))
        position = match.end()
    if position < len(text):
        runs.append((text[position:], False))
    return runs


def emoji_count(run: str) -> int:
    """How many emoji a run of emoji holds (joiners and variation selectors do not count)."""
    return len(re.sub("[‍️\U0001f3fb-\U0001f3ff]", "", run))


def line_width(text: str, size: int, text_width: TextWidth) -> float:
    """Width of one line: plain runs measured by `text_width`, each emoji a square of the emoji size."""
    total = 0.0
    for run, is_emoji in split_runs(text):
        total += emoji_count(run) * size * EMOJI_SCALE if is_emoji else text_width(run, size)
    return total


@dataclass(frozen=True, slots=True)
class TitleFit:
    """The hook wrapped into lines at a font size, and the size of its white box."""

    lines: tuple[str, ...]
    size: int
    box_w: int
    box_h: int


def _box(lines: Sequence[str], size: int, text_width: TextWidth) -> tuple[int, int]:
    widest = max(line_width(line, size, text_width) for line in lines)
    height = round(len(lines) * size * TITLE_LINE_HEIGHT) + 2 * TITLE_PAD_Y
    return round(widest) + 2 * TITLE_PAD_X, height


def wrap_greedy(words: Sequence[str], size: int, limit: float, text_width: TextWidth) -> list[str]:
    """Fills each line with as many words as fit in `limit`, like TikTok's own text box (a single word
    wider than the limit gets a line of its own)."""
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join([*current, word])
        if current and line_width(trial, size, text_width) > limit:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def fit_title(text: str, text_width: TextWidth) -> TitleFit | None:
    """The largest size at which the hook, wrapped fill-first, takes at most two lines inside the box
    width. Text too long even then is cut at a word and ends with an ellipsis. None for an empty hook."""
    words = [word for word in single_line(text).split(" ") if word]
    if not words:
        return None
    limit = TITLE_MAX_WIDTH - 2 * TITLE_PAD_X
    truncated = False
    while words:
        shown = [*words[:-1], words[-1].rstrip(".,;:!?…") + "…"] if truncated else words
        for size in TITLE_SIZES:
            lines = wrap_greedy(shown, size, limit, text_width)
            if len(lines) <= TITLE_MAX_LINES and all(line_width(x, size, text_width) <= limit for x in lines):
                box_w, box_h = _box(lines, size, text_width)
                return TitleFit(tuple(lines), size, box_w, box_h)
        truncated = True
        words = words[:-1] if len(words) > 1 else []
    return None


# ── drawing ──────────────────────────────────────────────────


@lru_cache(maxsize=16)
def _text_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size)
    try:
        axes = font.get_variation_axes()
    except OSError:
        return font  # not a variable font
    values: list[float] = []
    for axis in axes:
        name = axis["name"].decode() if isinstance(axis["name"], bytes) else str(axis["name"])
        default = axis["default"]
        values.append(float(TITLE_WEIGHT) if name == "Weight" else float(default or 0))
    font.set_variation_by_axes(values)
    return font


def pillow_text_width(font_file: Path) -> TextWidth:
    """Plain-text width from the title font file, in pixels at `size`."""

    def width(text: str, size: int) -> float:
        return float(_text_font(str(font_file), size).getlength(text))

    return width


@lru_cache(maxsize=64)
def _emoji_image(path: str, emoji: str, size: int, colour: bool) -> Image.Image:
    """One emoji drawn with the emoji font and scaled to `size` pixels high."""
    try:
        font = ImageFont.truetype(path, size)
        drawn_size = size
    except OSError:
        font = ImageFont.truetype(path, CBDT_SIZE)  # bitmap colour font: one fixed size
        drawn_size = CBDT_SIZE
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    try:
        left, top, right, bottom = probe.textbbox((0, 0), emoji, font=font, embedded_color=colour)
    except OSError:
        font = ImageFont.truetype(path, CBDT_SIZE)
        drawn_size = CBDT_SIZE
        left, top, right, bottom = probe.textbbox((0, 0), emoji, font=font, embedded_color=colour)
    width, height = max(1, round(right - left)), max(1, round(bottom - top))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((-left, -top), emoji, font=font, fill=(0, 0, 0, 255), embedded_color=colour)
    if drawn_size == size and image.height == size:
        return image
    scale = size / max(1, image.height)
    return image.resize((max(1, round(image.width * scale)), size), Image.Resampling.LANCZOS)


@dataclass(frozen=True, slots=True)
class TitleBar:
    """A drawn title bar: the PNG and its size on the reference canvas."""

    path: Path
    width: int
    height: int


def draw_title_bar(
    text: str, out_path: Path, *, font_file: Path, emoji_font: Path | None, emoji_colour: bool
) -> TitleBar | None:
    """Draws the hook as a title bar PNG (transparent outside the white box). None for an empty hook.

    `emoji_font` draws the emoji (in colour when `emoji_colour`); without it emoji are left out.
    """
    text_width = pillow_text_width(font_file)
    fit = fit_title(text, text_width)
    if fit is None:
        return None
    image = Image.new("RGBA", (fit.box_w, fit.box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, fit.box_w - 1, fit.box_h - 1), radius=TITLE_RADIUS, fill=(255, 255, 255, 255)
    )
    font = _text_font(str(font_file), fit.size)
    ascent, descent = font.getmetrics()
    pitch = fit.size * TITLE_LINE_HEIGHT
    emoji_size = round(fit.size * EMOJI_SCALE)
    for n, line in enumerate(fit.lines):
        top = TITLE_PAD_Y + n * pitch
        baseline = top + (pitch - (ascent + descent)) / 2 + ascent
        x = (fit.box_w - line_width(line, fit.size, text_width)) / 2
        for run, is_emoji in split_runs(line):
            if not is_emoji:
                draw.text((x, baseline), run, font=font, fill=(0, 0, 0, 255), anchor="ls")
                x += text_width(run, fit.size)
                continue
            slot = emoji_count(run) * emoji_size
            if emoji_font is not None:
                glyph = _emoji_image(str(emoji_font), run, emoji_size, emoji_colour)
                y = round(baseline - ascent * 0.5 - glyph.height / 2 + fit.size * 0.06)
                image.alpha_composite(glyph, (round(x + (slot - glyph.width) / 2), max(0, y)))
            x += slot
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    return TitleBar(out_path, fit.box_w, fit.box_h)

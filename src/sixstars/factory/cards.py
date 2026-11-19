"""The on-screen texts of the Phase B design styles (development-plan/M18, docs/27 section 5), drawn with
Pillow into transparent PNGs on the 1080x1920 reference canvas and laid over the video by ffmpeg, like the
title bar (`titlebar.py`): libass cannot draw colour emoji.

- `draw_headline_card`: the news card of layout H (reference R09): the brand avatar and name, then a
  headline of up to three lines, left-aligned, white.
- `draw_outline_text`: one to two centred lines with an outline, for the POV line of layout P (white,
  reference R08) and the question title of `cam_top_question` (red capitals, reference R05).

The fitting (`fit_lines`) is pure; the `draw_*` functions write the image.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from sixstars.factory.escape import single_line
from sixstars.factory.layout import H_HEADER_TOP, H_VIDEO_Y
from sixstars.factory.titlebar import (
    TextWidth,
    _emoji_image,
    emoji_count,
    line_width,
    split_runs,
    wrap_greedy,
)

Rgba = tuple[int, int, int, int]
WHITE: Rgba = (255, 255, 255, 255)
BLACK: Rgba = (0, 0, 0, 255)
QUESTION_RED: Rgba = (255, 46, 46, 255)

# Headline card (layout H), reference canvas pixels.
CARD_WIDTH = 1080
CARD_MARGIN_X = 60
CARD_AVATAR = 92
CARD_NAME_SIZE = 40
CARD_NAME_WEIGHT = 700
CARD_GAP = 26
CARD_HEADLINE_SIZES = (60, 56, 52, 48, 44, 40)
CARD_HEADLINE_WEIGHT = 500
CARD_HEADLINE_LINES = 3
# The whole card ends at least this far above the stream of layout H.
CARD_BOTTOM_GAP = 10
LINE_PITCH = 1.18

# POV line (layout P) and question title (cam_top_question).
POV_SIZES = (66, 62, 58, 54, 50, 46)
POV_WEIGHT = 600
POV_MAX_WIDTH = 940
POV_STROKE = 3
QUESTION_SIZES = (84, 78, 72, 66, 60, 54)
QUESTION_MAX_WIDTH = 980
QUESTION_STROKE = 7
OUTLINE_LINES = 2


@dataclass(frozen=True, slots=True)
class Drawn:
    """A drawn PNG and its size on the reference canvas."""

    path: Path
    width: int
    height: int


@lru_cache(maxsize=32)
def _font(path: str, size: int, weight: int | None) -> ImageFont.FreeTypeFont:
    """The font at `size`; a variable font is set to `weight` (None: its default)."""
    font = ImageFont.truetype(path, size)
    if weight is None:
        return font
    try:
        axes = font.get_variation_axes()
    except OSError:
        return font
    values: list[float] = []
    for axis in axes:
        name = axis["name"].decode() if isinstance(axis["name"], bytes) else str(axis["name"])
        values.append(float(weight) if name == "Weight" else float(axis["default"] or 0))
    font.set_variation_by_axes(values)
    return font


def text_width_of(font_file: Path, weight: int | None) -> TextWidth:
    def width(text: str, size: int) -> float:
        return float(_font(str(font_file), size, weight).getlength(text))

    return width


@dataclass(frozen=True, slots=True)
class LinesFit:
    lines: tuple[str, ...]
    size: int


def fit_lines(
    text: str,
    sizes: Sequence[int],
    limit: float,
    max_lines: int,
    text_width: TextWidth,
    max_height: float | None = None,
) -> LinesFit | None:
    """The largest size at which `text`, wrapped fill-first, takes at most `max_lines` lines no wider than
    `limit` (and, with `max_height`, no taller than it at `LINE_PITCH`); text too long even at the smallest
    size is cut at a word and ends with an ellipsis. None for empty text. Pure."""
    words = [word for word in single_line(text).split(" ") if word]
    truncated = False
    while words:
        shown = [*words[:-1], words[-1].rstrip(".,;:!?…") + "…"] if truncated else words
        for size in sizes:
            lines = wrap_greedy(shown, size, limit, text_width)
            tall = max_height is not None and len(lines) * size * LINE_PITCH > max_height
            if (
                len(lines) <= max_lines
                and not tall
                and all(line_width(x, size, text_width) <= limit for x in lines)
            ):
                return LinesFit(tuple(lines), size)
        truncated = True
        words = words[:-1] if len(words) > 1 else []
    return None


def _draw_line(
    image: Image.Image,
    line: str,
    *,
    x: float,
    baseline: float,
    size: int,
    font: ImageFont.FreeTypeFont,
    text_width: TextWidth,
    fill: Rgba,
    stroke: int,
    emoji_font: Path | None,
    emoji_colour: bool,
) -> None:
    draw = ImageDraw.Draw(image)
    ascent = font.getmetrics()[0]
    for run, is_emoji in split_runs(line):
        if not is_emoji:
            draw.text(
                (x, baseline), run, font=font, fill=fill, anchor="ls", stroke_width=stroke, stroke_fill=BLACK
            )
            x += text_width(run, size)
            continue
        slot = emoji_count(run) * size
        if emoji_font is not None:
            glyph = _emoji_image(str(emoji_font), run, size, emoji_colour)
            y = round(baseline - ascent * 0.5 - glyph.height / 2 + size * 0.06)
            image.alpha_composite(glyph, (round(x + (slot - glyph.width) / 2), max(0, y)))
        x += slot


def draw_outline_text(
    text: str,
    out_path: Path,
    *,
    font_file: Path,
    weight: int | None,
    sizes: Sequence[int],
    max_width: int,
    fill: Rgba,
    stroke: int,
    emoji_font: Path | None,
    emoji_colour: bool,
    caps: bool = False,
) -> Drawn | None:
    """One or two centred lines with a black outline on a transparent PNG. None for empty text."""
    shown = text.upper() if caps else text
    text_width = text_width_of(font_file, weight)
    pad = stroke + 8
    fit = fit_lines(shown, sizes, max_width - 2 * pad, OUTLINE_LINES, text_width)
    if fit is None:
        return None
    font = _font(str(font_file), fit.size, weight)
    ascent, descent = font.getmetrics()
    pitch = fit.size * LINE_PITCH
    width = round(max(line_width(line, fit.size, text_width) for line in fit.lines)) + 2 * pad
    height = round(len(fit.lines) * pitch) + 2 * pad
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for n, line in enumerate(fit.lines):
        top = pad + n * pitch
        baseline = top + (pitch - (ascent + descent)) / 2 + ascent
        x = (width - line_width(line, fit.size, text_width)) / 2
        _draw_line(
            image,
            line,
            x=x,
            baseline=baseline,
            size=fit.size,
            font=font,
            text_width=text_width,
            fill=fill,
            stroke=stroke,
            emoji_font=emoji_font,
            emoji_colour=emoji_colour,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    return Drawn(out_path, width, height)


def _avatar(path: Path, size: int) -> Image.Image:
    picture = Image.open(path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    round_picture = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    round_picture.paste(picture, (0, 0), mask)
    return round_picture


def draw_headline_card(
    name: str,
    headline: str,
    out_path: Path,
    *,
    font_file: Path,
    avatar: Path | None,
    emoji_font: Path | None,
    emoji_colour: bool,
) -> Drawn | None:
    """The brand header and the headline, CARD_WIDTH wide, on a transparent PNG. None for an empty
    headline."""
    limit = CARD_WIDTH - 2 * CARD_MARGIN_X
    headline_width = text_width_of(font_file, CARD_HEADLINE_WEIGHT)
    room = H_VIDEO_Y - H_HEADER_TOP - CARD_BOTTOM_GAP - CARD_AVATAR - CARD_GAP - 12
    fit = fit_lines(headline, CARD_HEADLINE_SIZES, limit, CARD_HEADLINE_LINES, headline_width, room)
    if fit is None:
        return None
    pitch = fit.size * LINE_PITCH
    height = CARD_AVATAR + CARD_GAP + round(len(fit.lines) * pitch) + 12
    image = Image.new("RGBA", (CARD_WIDTH, height), (0, 0, 0, 0))
    name_x = CARD_MARGIN_X
    if avatar is not None and avatar.is_file():
        image.alpha_composite(_avatar(avatar, CARD_AVATAR), (CARD_MARGIN_X, 0))
        name_x = CARD_MARGIN_X + CARD_AVATAR + 22
    name_font = _font(str(font_file), CARD_NAME_SIZE, CARD_NAME_WEIGHT)
    ImageDraw.Draw(image).text(
        (name_x, CARD_AVATAR / 2), single_line(name), font=name_font, fill=WHITE, anchor="lm"
    )
    font = _font(str(font_file), fit.size, CARD_HEADLINE_WEIGHT)
    ascent, descent = font.getmetrics()
    for n, line in enumerate(fit.lines):
        top = CARD_AVATAR + CARD_GAP + n * pitch
        baseline = top + (pitch - (ascent + descent)) / 2 + ascent
        _draw_line(
            image,
            line,
            x=CARD_MARGIN_X,
            baseline=baseline,
            size=fit.size,
            font=font,
            text_width=headline_width,
            fill=WHITE,
            stroke=0,
            emoji_font=emoji_font,
            emoji_colour=emoji_colour,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    return Drawn(out_path, CARD_WIDTH, height)

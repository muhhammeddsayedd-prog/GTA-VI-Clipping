"""Text width measurement used to size captions and the hook box before libass draws them.

Sizes are ASS font sizes. libass treats the font size as the line height (ascender plus descender), not
the em square, so a glyph is narrower than the same size in em pixels: about 0.82 of it for Montserrat.
The default measurer is an estimate tuned for Montserrat Black that needs no font file and errs on the wide
side; `pillow_measurer` reads real widths when the font file is known.
"""

import unicodedata
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

# (text, ASS font size) -> width in px on the reference canvas
TextMeasurer = Callable[[str, float], float]

# Em pixels per unit of ASS font size for Montserrat (ascender 968 + descender 251 per 1000 units).
EM_PER_ASS_SIZE = 0.82

_NARROW = frozenset("iljI.,:;!|'`")
_SPACE = 0.25
_NARROW_W = 0.3
_LOWER = 0.56
_UPPER = 0.68
_OTHER = 0.58
_WIDE = 0.88


def estimate_text_width(text: str, size: float) -> float:
    """Approximate width of `text` at an ASS font size in a heavy display font; errs on the wide side."""
    total = 0.0
    for ch in text:
        if ch == " ":
            total += _SPACE
        elif ch in _NARROW:
            total += _NARROW_W
        elif unicodedata.east_asian_width(ch) in ("W", "F") or ord(ch) > 0x2FFF:
            total += _WIDE
        elif unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Cf"):
            continue
        elif ch.isupper() or ch.isdigit():
            total += _UPPER
        elif ch.islower():
            total += _LOWER
        else:
            total += _OTHER
    return total * size


def estimate_em_text_width(text: str, em_size: float) -> float:
    """The same estimate for a size given in em pixels, as ffmpeg `drawtext=fontsize=` uses."""
    return estimate_text_width(text, em_size / EM_PER_ASS_SIZE)


_METRICS_SIZE = 1000


def pillow_measurer(font_file: Path) -> TextMeasurer:
    """A measurer that reads real advance widths from a TrueType/OpenType file, in ASS font sizes."""
    ascent, descent = ImageFont.truetype(str(font_file), _METRICS_SIZE).getmetrics()
    em_per_size = _METRICS_SIZE / max(1, ascent + descent)

    @lru_cache(maxsize=32)
    def load(em_px: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(font_file), em_px)

    def measure(text: str, size: float) -> float:
        em = size * em_per_size
        reference = max(8, round(em))
        return float(load(reference).getlength(text)) * (em / reference)

    return measure

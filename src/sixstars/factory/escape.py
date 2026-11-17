"""Escaping and colour helpers for everything the factory writes into ASS files and ffmpeg filter graphs.

Two targets with different rules:

* ASS event text (libass): braces open override blocks, a backslash starts the escapes N, n and h, and the
  file is line based.
* ffmpeg `-filter_complex`: a value is unescaped twice, once by the graph parser (backslash, quote, comma,
  semicolon, square brackets) and once by the filter's option parser (backslash, quote, colon).
"""

import re
import unicodedata

# Invisible character libass skips when shaping. Placed after a backslash it stops the following character
# from being read as an escape, so the backslash is drawn as a backslash.
WORD_JOINER = "⁠"

_NEWLINES = re.compile("\r\n|[\r\n\v\f\u0085  ]")
_HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/]|\\\\")
_GRAPH_SPECIALS = frozenset("\\',;[]")
# The text font has no emoji. libass would pick a fallback from the system fonts, which a server image does
# not have, so emoji runs are switched to the bundled emoji font by name (`assets/fonts/NotoEmoji.ttf`).
EMOJI_FONT = "Noto Emoji"
_EMOJI = "\U0001f000-\U0001faff⌀-⏿☀-➿⬀-⯿"
_EMOJI_RUN = re.compile(f"[{_EMOJI}][{_EMOJI}‍️]*")


def _strip_controls(text: str) -> str:
    """Drops control characters (Unicode category Cc)."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cc")


def single_line(text: str) -> str:
    """Collapses every kind of line break and tab to one space and removes other control characters."""
    flat = _NEWLINES.sub(" ", text).replace("\t", " ")
    return re.sub(r" {2,}", " ", _strip_controls(flat)).strip()


def ass_text(text: str, *, keep_newlines: bool = False) -> str:
    """Escapes plain text for an ASS Dialogue Text field.

    Line breaks become a space, or the ASS hard break when `keep_newlines` is set. Braces are escaped,
    backslashes are neutralised with a word joiner, control characters are removed.
    """
    parts = [_escape_ass_line(part) for part in _NEWLINES.split(text)]
    parts = [part for part in parts if part]
    return ("\\N" if keep_newlines else " ").join(parts)


def ass_text_emoji(text: str, *, keep_newlines: bool = False, emoji_font: str = EMOJI_FONT) -> str:
    """`ass_text`, with every run of emoji drawn in `emoji_font`.

    The font is switched with an `fn` override tag; the tag with no name returns to the style's font.
    """
    escaped = ass_text(text, keep_newlines=keep_newlines)
    font = ass_field(emoji_font)
    return _EMOJI_RUN.sub(lambda match: "{\\fn" + font + "}" + match.group(0) + "{\\fn}", escaped)


def _escape_ass_line(line: str) -> str:
    clean = _strip_controls(line.replace("\t", " ")).replace(WORD_JOINER, "").strip()
    out: list[str] = []
    for ch in clean:
        if ch == "\\":
            out.append("\\" + WORD_JOINER)
        elif ch in "{}":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def ass_field(text: str) -> str:
    """Sanitises a value for a comma-separated ASS field (font name, style name): no commas or breaks."""
    return single_line(single_line(text).replace(",", " "))


def parse_hex_color(value: str) -> tuple[int, int, int]:
    """Parses `#RRGGBB` (the `factory.accent_color` format) into an (r, g, b) tuple."""
    match = _HEX_COLOR.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"not a #RRGGBB colour: {value!r}")
    digits = match.group(1)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def ass_color(value: str, *, alpha: int = 0) -> str:
    """`#RRGGBB` to an ASS style colour `&HAABBGGRR` (alpha 0 = opaque, 255 = transparent)."""
    if not 0 <= alpha <= 255:
        raise ValueError("alpha must be within 0..255")
    r, g, b = parse_hex_color(value)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_override_color(value: str) -> str:
    """`#RRGGBB` to the colour form used inside override tags: `&HBBGGRR&`."""
    r, g, b = parse_hex_color(value)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def ffmpeg_color(value: str, *, opacity: float = 1.0) -> str:
    """`#RRGGBB` to an ffmpeg colour (`0xRRGGBB`, with `@opacity` when not fully opaque)."""
    if not 0.0 <= opacity <= 1.0:
        raise ValueError("opacity must be within 0..1")
    r, g, b = parse_hex_color(value)
    base = f"0x{r:02X}{g:02X}{b:02X}"
    return base if opacity >= 1.0 else f"{base}@{opacity:g}"


def filter_value(value: str) -> str:
    """Escapes one option value for use inside a `-filter_complex` graph (both parsing levels).

    Works for free text, paths and expressions (commas in `between(t,1,2)` are escaped rather than quoted).
    Line breaks are not allowed in a value; pass text through `single_line` first.
    """
    if _NEWLINES.search(value):
        raise ValueError("filter values must be single-line")
    option_level = value.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    out: list[str] = []
    last = len(option_level) - 1
    for i, ch in enumerate(option_level):
        if ch in _GRAPH_SPECIALS or (ch == " " and i in (0, last)):
            out.append("\\")
        out.append(ch)
    return "".join(out)


def normalize_filter_path(path: str) -> str:
    """Rewrites a Windows path (drive letter or UNC) with forward slashes; other paths are unchanged."""
    return path.replace("\\", "/") if _WINDOWS_PATH.match(path) else path


def filter_path(path: str) -> str:
    """Escapes a file path for a filter option. Windows paths are first rewritten with forward slashes."""
    return filter_value(normalize_filter_path(path))


def ass_time(seconds: float) -> str:
    """Seconds to the ASS timestamp `H:MM:SS.cc` (centiseconds, rounded)."""
    total = max(0, round(seconds * 100))
    hours, rest = divmod(total, 360000)
    minutes, rest = divmod(rest, 6000)
    return f"{hours}:{minutes:02d}:{rest // 100:02d}.{rest % 100:02d}"


def drawtext_text(text: str) -> str:
    """Escapes text for `drawtext=text=...` when the filter runs with `expansion=none`."""
    return filter_value(single_line(text))


def render_filter(name: str, options: list[tuple[str, str]]) -> str:
    """Renders `name=k1=v1:k2=v2`, escaping every value with `filter_value`."""
    if not options:
        return name
    return name + "=" + ":".join(f"{key}={filter_value(value)}" for key, value in options)

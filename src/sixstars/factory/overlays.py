"""Brand overlays: watermark, creator credit and end card placement for the render. Implemented in M06.

Two kinds of output, both pure strings:

* ASS events for the overlays that need rounded shapes, animation or emoji (hook box, LIVE badge, creator
  credit). `subtitles.build_ass` places them in the same file as the captions.
* ffmpeg filters for the seam line, watermark, progress bar and end card.

Positions follow docs/09 section 5 on the 1080x1920 reference canvas.
"""

from dataclasses import dataclass
from pathlib import PurePath
from typing import Final

from sixstars.factory.escape import (
    ass_text,
    ass_text_emoji,
    ass_time,
    drawtext_text,
    ffmpeg_color,
    filter_value,
    normalize_filter_path,
    render_filter,
    single_line,
)
from sixstars.factory.layout import BADGE_X, HOOK_BOTTOM, HOOK_TOP, REF_HEIGHT, REF_WIDTH, Geometry
from sixstars.factory.params import FontFiles
from sixstars.factory.textfit import TextMeasurer, estimate_em_text_width, estimate_text_width

# Text and look fixed by docs/09 section 5 (not settings).
BRAND_NAME = "6 STARS CLIPS"
END_CARD_FOLLOW = "Follow for more"
LIVE_BADGE_TEXT = "LIVE · JUST HAPPENED"
CREDIT_PREFIX = "🎥 @"
CREDIT_SIZE = 34
CREDIT_OPACITY = 0.85
WATERMARK_SIZE = 30
WATERMARK_OPACITY = 0.6
HOOK_POP_MS = 150
END_CARD_COLOR = "#0D0D12"
# Layout A: the seam is the track of the progress bar, so it is drawn in white and the bar in the accent.
SEAM_TRACK_COLOR = "#FFFFFF"

# Design choices inside the positions the docs fix.
HOOK_MAX_LINES = 2
HOOK_MAX_WIDTH = 960
HOOK_PAD_X = 48
HOOK_MIN_WIDTH = 260
HOOK_RADIUS = 30
HOOK_SIZES = (76, 72, 68, 64, 60, 56, 52, 48, 44)
HOOK_LINE_SPACING = 1.0
HOOK_PAD_Y = 14
BADGE_HEIGHT = 56
BADGE_PAD_X = 26
BADGE_TEXT_SIZE = 30
BADGE_COLOR = "&H2B21E8&"  # red, BGR
BADGE_GAP = 16
# The dot of the LIVE badge is drawn as a shape: neither bundled font has a glyph for it.
BADGE_DOT_SIZE = 18
BADGE_DOT_GAP = 12
CREDIT_X = 40
END_CARD_BRAND_Y = 800
END_CARD_BRAND_SIZE = 92
END_CARD_SERIES_Y = 932
END_CARD_SERIES_SIZE = 58
END_CARD_FOLLOW_Y = 1034
END_CARD_FOLLOW_SIZE = 42
END_CARD_MAX_TEXT_WIDTH = 980
END_CARD_LOGO_SIZE = 200
END_CARD_LOGO_Y = 560

LAYER_CREDIT = 1
LAYER_CAPTION = 2
LAYER_BADGE = 3
LAYER_HOOK = 5

_KAPPA = 0.5523


def dialogue(layer: int, start_s: float, end_s: float, style: str, text: str) -> str:
    """One ASS Dialogue line. `text` must already be escaped ASS text (override tags allowed)."""
    return f"Dialogue: {layer},{ass_time(start_s)},{ass_time(end_s)},{style},,0,0,0,,{text}"


def rounded_rect_path(width: int, height: int, radius: int) -> str:
    """ASS vector drawing commands for a rounded rectangle with its top-left corner at the origin."""
    r = max(0, min(radius, width // 2, height // 2))
    if r == 0:
        return f"m 0 0 l {width} 0 {width} {height} 0 {height}"
    c = round(r * (1 - _KAPPA))
    w, h = width, height
    return (
        f"m {r} 0 l {w - r} 0 b {w - c} 0 {w} {c} {w} {r} "
        f"l {w} {h - r} b {w} {h - c} {w - c} {h} {w - r} {h} "
        f"l {r} {h} b {c} {h} 0 {h - c} 0 {h - r} "
        f"l 0 {r} b 0 {c} {c} 0 {r} 0"
    )


@dataclass(frozen=True, slots=True)
class HookFit:
    """A hook title wrapped and sized for the hook box."""

    lines: tuple[str, ...]
    font_size: int
    box_width: int


def _best_split(words: list[str], size: int, measure: TextMeasurer) -> tuple[str, str] | None:
    best: tuple[float, tuple[str, str]] | None = None
    for i in range(1, len(words)):
        first, second = " ".join(words[:i]), " ".join(words[i:])
        widest = max(measure(first, size), measure(second, size))
        if best is None or widest < best[0]:
            best = (widest, (first, second))
    return None if best is None else best[1]


def fit_hook(text: str, measure: TextMeasurer = estimate_text_width) -> HookFit | None:
    """Wraps the hook into at most two lines and picks the largest font size that fits the box.

    Text too long for two lines at the smallest size is cut at a word and ends with an ellipsis. Returns
    None for an empty hook.
    """
    words = single_line(text).split(" ")
    words = [word for word in words if word]
    if not words:
        return None
    box_height = HOOK_BOTTOM - HOOK_TOP
    max_text_width = HOOK_MAX_WIDTH - 2 * HOOK_PAD_X
    two_line_limit = (box_height - 2 * HOOK_PAD_Y) / (HOOK_MAX_LINES * HOOK_LINE_SPACING)
    truncated = False
    while True:
        shown = [*words[:-1], words[-1].rstrip(".,;:!?…") + "…"] if truncated else words
        for size in HOOK_SIZES:
            lines: tuple[str, ...] | None = None
            if measure(" ".join(shown), size) <= max_text_width:
                lines = (" ".join(shown),)
            elif size <= two_line_limit:
                split = _best_split(shown, size, measure)
                if split is not None and max(measure(part, size) for part in split) <= max_text_width:
                    lines = split
            if lines is not None:
                widest = max(measure(line, size) for line in lines)
                box_width = int(min(HOOK_MAX_WIDTH, max(HOOK_MIN_WIDTH, widest + 2 * HOOK_PAD_X)))
                return HookFit(lines, size, box_width)
        truncated = True
        if len(words) > 1:
            words = words[:-1]
        elif len(words[0]) > 1:
            words = [words[0][:-1]]
        else:
            return HookFit((words[0],), HOOK_SIZES[-1], HOOK_MIN_WIDTH)


def hook_events(
    text: str,
    *,
    end_s: float,
    top: float = HOOK_TOP,
    measure: TextMeasurer = estimate_text_width,
) -> list[str]:
    """Hook title: white rounded box with black text between `top` and `top + 160`, pop-in over 150 ms."""
    fit = fit_hook(text, measure)
    if fit is None or end_s <= 0:
        return []
    height = HOOK_BOTTOM - HOOK_TOP
    centre = f"\\an5\\pos({REF_WIDTH // 2},{round(top + height / 2)})"
    pop = f"\\fscx82\\fscy82\\t(0,{HOOK_POP_MS},\\fscx100\\fscy100)\\fad(60,120)"
    shape = (
        f"{{{centre}{pop}\\p1\\bord0\\shad4\\1c&HFFFFFF&\\4c&H000000&\\4a&HA0&}}"
        f"{rounded_rect_path(fit.box_width, height, HOOK_RADIUS)}{{\\p0}}"
    )
    body = "\\N".join(ass_text_emoji(line) for line in fit.lines)
    label = f"{{{centre}{pop}\\q2\\fs{fit.font_size}}}{body}"
    return [
        dialogue(LAYER_HOOK, 0.0, end_s, "Hook", shape),
        dialogue(LAYER_HOOK + 1, 0.0, end_s, "Hook", label),
    ]


def badge_width(text: str = LIVE_BADGE_TEXT, measure: TextMeasurer = estimate_text_width) -> int:
    """Width of the LIVE pill: padding, the dot, a gap and the text."""
    text_width = round(measure(single_line(text), BADGE_TEXT_SIZE))
    return text_width + 2 * BADGE_PAD_X + BADGE_DOT_SIZE + BADGE_DOT_GAP


def badge_events(
    *,
    end_s: float,
    top: float,
    text: str = LIVE_BADGE_TEXT,
    measure: TextMeasurer = estimate_text_width,
) -> list[str]:
    """Spike-lane badge: red pill with a white dot and white text, top-left at (40, `top`), for the whole
    clip."""
    if end_s <= 0 or not single_line(text):
        return []
    width = badge_width(text, measure)
    shape = (
        f"{{\\an7\\pos({BADGE_X},{round(top)})\\p1\\bord0\\shad0\\1c{BADGE_COLOR}}}"
        f"{rounded_rect_path(width, BADGE_HEIGHT, BADGE_HEIGHT // 2)}{{\\p0}}"
    )
    dot = (
        f"{{\\an7\\pos({BADGE_X + BADGE_PAD_X},{round(top + (BADGE_HEIGHT - BADGE_DOT_SIZE) / 2)})"
        f"\\p1\\bord0\\shad0\\1c&HFFFFFF&}}"
        f"{rounded_rect_path(BADGE_DOT_SIZE, BADGE_DOT_SIZE, BADGE_DOT_SIZE // 2)}{{\\p0}}"
    )
    text_x = BADGE_X + BADGE_PAD_X + BADGE_DOT_SIZE + BADGE_DOT_GAP
    label = f"{{\\an4\\pos({text_x},{round(top + BADGE_HEIGHT / 2)})\\q2}}{ass_text(text)}"
    return [
        dialogue(LAYER_BADGE, 0.0, end_s, "Badge", shape),
        dialogue(LAYER_BADGE + 1, 0.0, end_s, "Badge", dot),
        dialogue(LAYER_BADGE + 1, 0.0, end_s, "Badge", label),
    ]


def credit_text(creator: str) -> str:
    """`🎥 @creator` from a handle given with or without the leading `@`. Empty for an empty handle."""
    handle = single_line(creator).lstrip("@").strip()
    return f"{CREDIT_PREFIX}{handle}" if handle else ""


def credit_events(creator: str, *, end_s: float, bottom: float) -> list[str]:
    """Creator credit, bottom-left, its text bottom at `bottom`, for the whole clip."""
    text = credit_text(creator)
    if not text or end_s <= 0:
        return []
    return [
        dialogue(
            LAYER_CREDIT,
            0.0,
            end_s,
            "Credit",
            f"{{\\an1\\pos({CREDIT_X},{round(bottom)})\\q2}}{ass_text_emoji(text)}",
        )
    ]


# ---------------------------------------------------------------------------------------------------------
# ffmpeg filters
# ---------------------------------------------------------------------------------------------------------


def _seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def seam_filter(geometry: Geometry, accent_color: str, *, y: int | None = None) -> str:
    """Seam of layouts A and C: accent line across the frame where gameplay meets facecam (at row `y`, by
    default the layout A seam)."""
    options = [
        ("x", "0"),
        ("y", str(geometry.a_seam_y if y is None else y)),
        ("w", str(geometry.width)),
        ("h", str(geometry.a_seam_h)),
        ("color", ffmpeg_color(accent_color)),
        ("thickness", "fill"),
    ]
    return render_filter("drawbox", options)


def ass_filter(ass_path: PurePath | str, fonts_dir: PurePath | str | None = None) -> str:
    """The libass filter for a subtitle file, with an optional directory of bundled fonts."""
    options = [("filename", normalize_filter_path(str(ass_path)))]
    if fonts_dir is not None:
        options.append(("fontsdir", normalize_filter_path(str(fonts_dir))))
    return render_filter("ass", options)


def _font_options(font_name: str, fonts: FontFiles) -> list[tuple[str, str]]:
    if fonts.font_file is not None:
        return [("fontfile", normalize_filter_path(str(fonts.font_file)))]
    return [("font", single_line(font_name))]


def drawtext_filter(
    text: str,
    *,
    font_name: str,
    fonts: FontFiles,
    size: int,
    color: str,
    x: str,
    y: str,
    extra: list[tuple[str, str]] | None = None,
) -> str:
    """A `drawtext` filter with text expansion turned off, so `%` and backslashes are drawn literally."""
    name_and_text = f"drawtext=text={drawtext_text(text)}"
    options = [
        ("expansion", "none"),
        *_font_options(font_name, fonts),
        ("fontsize", str(size)),
        ("fontcolor", color),
        ("x", x),
        ("y", y),
        *(extra or []),
    ]
    return name_and_text + ":" + ":".join(f"{key}={filter_value(value)}" for key, value in options)


def watermark_filter(
    text: str,
    *,
    geometry: Geometry,
    font_name: str,
    fonts: FontFiles,
    start_s: float,
    y: int | None = None,
) -> str | None:
    """Watermark, right-aligned at (1040, 240) or at output row `y`, 60% white, shown from `start_s` (once
    the hook has gone). None if empty."""
    if not single_line(text):
        return None
    scale = geometry.height / REF_HEIGHT
    extra = [("shadowcolor", "black@0.35"), ("shadowx", "2"), ("shadowy", "2")]
    if start_s > 0:
        extra.append(("enable", f"gte(t,{_seconds(start_s)})"))
    return drawtext_filter(
        text,
        font_name=font_name,
        fonts=fonts,
        size=max(8, round(WATERMARK_SIZE * scale)),
        color=f"white@{WATERMARK_OPACITY:g}",
        x=f"{geometry.watermark_right}-text_w",
        y=str(geometry.watermark_y if y is None else y),
        extra=extra,
    )


def progress_filters(
    *,
    geometry: Geometry,
    accent_color: str,
    fps: int,
    duration_s: float,
    source: str,
    out: str,
    y: int | None = None,
) -> list[str]:
    """Progress bar: an accent strip that slides in from the left over the clip, at y=1500 unless `y`
    is given."""
    length = _seconds(duration_s)
    bar = render_filter(
        "color",
        [
            ("color", ffmpeg_color(accent_color)),
            ("size", f"{geometry.width}x{geometry.progress_h}"),
            ("rate", str(fps)),
            ("duration", length),
        ],
    )
    overlay = render_filter(
        "overlay",
        [
            ("x", f"-w+w*t/{length}"),
            ("y", str(geometry.progress_y if y is None else y)),
            ("eof_action", "pass"),
        ],
    )
    return [f"{bar}[{out}_bar]", f"[{source}][{out}_bar]{overlay}[{out}]"]


def series_line(series_label: str, series_no: int) -> str:
    """The series counter shown on the end card, e.g. `6 Stars Moments #128`."""
    label = single_line(series_label)
    return f"{label} #{series_no}" if label else f"#{series_no}"


# Follow line (YouTube lane tuning C): reference-canvas size, width and where its centre sits inside the safe
# zone (0 = top of the safe zone, 1 = bottom). Low in the frame, above the bottom safe area.
FOLLOW_SIZE = 64
FOLLOW_MAX_TEXT_WIDTH = 900
FOLLOW_ZONE_POSITION = 0.82
FOLLOW_BOX_PAD = 22


@dataclass(frozen=True, slots=True)
class FollowLayout:
    """The follow line on the 1080x1920 reference canvas: font size and the top of the text."""

    size: int
    top: int

    def inside(self, safe_top_px: int, safe_bottom_px: int) -> bool:
        """The text and its box stay between the top and bottom safe areas."""
        return (
            self.top - FOLLOW_BOX_PAD >= safe_top_px
            and self.top + self.size + FOLLOW_BOX_PAD <= REF_HEIGHT - safe_bottom_px
        )


def follow_layout(text: str, safe_top_px: int, safe_bottom_px: int) -> FollowLayout:
    """Where the follow line goes: shrunk to fit `FOLLOW_MAX_TEXT_WIDTH`, centred on
    `FOLLOW_ZONE_POSITION` of the safe zone, and moved up if its box would reach the bottom safe area."""
    size = _fitted_size(text, FOLLOW_SIZE, FOLLOW_MAX_TEXT_WIDTH)
    zone_top, zone_bottom = safe_top_px, REF_HEIGHT - safe_bottom_px
    centre = zone_top + (zone_bottom - zone_top) * FOLLOW_ZONE_POSITION
    top = round(centre - size / 2)
    top = min(top, zone_bottom - FOLLOW_BOX_PAD - size)
    top = max(top, zone_top + FOLLOW_BOX_PAD)
    return FollowLayout(size=size, top=top)


def follow_filter(
    text: str,
    *,
    geometry: Geometry,
    font_name: str,
    fonts: FontFiles,
    accent_color: str,
    clip_s: float,
    show_s: float,
    safe_top_px: int,
    safe_bottom_px: int,
) -> str | None:
    """The follow line over the last `show_s` seconds of a `clip_s` clip, centred on a dark box with an
    accent border. None when off or empty."""
    line = single_line(text)
    if not line or show_s <= 0 or clip_s <= 0:
        return None
    layout = follow_layout(line, safe_top_px, safe_bottom_px)
    scale = geometry.height / REF_HEIGHT
    start = max(0.0, clip_s - show_s)
    return drawtext_filter(
        line,
        font_name=font_name,
        fonts=fonts,
        size=max(8, round(layout.size * scale)),
        color="white",
        x="(w-text_w)/2",
        y=str(round(layout.top * scale)),
        extra=[
            ("box", "1"),
            ("boxcolor", "black@0.72"),
            ("boxborderw", str(max(2, round(FOLLOW_BOX_PAD * scale)))),
            ("bordercolor", accent_color),
            ("borderw", "2"),
            ("enable", f"gte(t,{_seconds(start)})"),
        ],
    )


DISCLOSURE_SIZE: Final = 40  # reference pixels
DISCLOSURE_LEFT: Final = 48
DISCLOSURE_GAP: Final = 24


def disclosure_filter(
    text: str,
    *,
    geometry: Geometry,
    font_name: str,
    fonts: FontFiles,
    clip_s: float,
    show_s: float,
    safe_top_px: int,
) -> str | None:
    """D118: the disclosure line ("Paid partnership") at the top left of the safe zone, on a dark box, over
    the first `show_s` seconds. None when off or empty."""
    line = single_line(text)
    if not line or show_s <= 0 or clip_s <= 0:
        return None
    scale = geometry.height / REF_HEIGHT
    return drawtext_filter(
        line,
        font_name=font_name,
        fonts=fonts,
        size=max(8, round(DISCLOSURE_SIZE * scale)),
        color="white",
        x=str(round(DISCLOSURE_LEFT * scale)),
        y=str(round((safe_top_px + DISCLOSURE_GAP) * scale)),
        extra=[
            ("box", "1"),
            ("boxcolor", "black@0.6"),
            ("boxborderw", str(max(2, round(12 * scale)))),
            ("enable", f"lt(t,{_seconds(min(show_s, clip_s))})"),
        ],
    )


def _fitted_size(text: str, size: int, max_width: float) -> int:
    width = estimate_em_text_width(single_line(text), size)
    return size if width <= max_width else max(8, int(size * max_width / width))


def end_card_filters(
    *,
    geometry: Geometry,
    fps: int,
    end_card_s: float,
    series_label: str,
    series_no: int,
    accent_color: str,
    font_name: str,
    fonts: FontFiles,
    out: str,
    brand: str = BRAND_NAME,
    follow: str = END_CARD_FOLLOW,
    logo_path: PurePath | str | None = None,
) -> list[str]:
    """End card: dark frame with the brand, the series counter and the follow line, centred.

    With `logo_path` (an image with transparency) the logo is drawn above the brand name.
    """
    scale = geometry.height / REF_HEIGHT
    max_width = END_CARD_MAX_TEXT_WIDTH * geometry.width / REF_WIDTH
    rows = [
        (brand, END_CARD_BRAND_SIZE, END_CARD_BRAND_Y, "white"),
        (
            series_line(series_label, series_no),
            END_CARD_SERIES_SIZE,
            END_CARD_SERIES_Y,
            ffmpeg_color(accent_color),
        ),
        (follow, END_CARD_FOLLOW_SIZE, END_CARD_FOLLOW_Y, "white@0.85"),
    ]
    chain = [
        render_filter(
            "color",
            [
                ("color", ffmpeg_color(END_CARD_COLOR)),
                ("size", f"{geometry.width}x{geometry.height}"),
                ("rate", str(fps)),
                ("duration", _seconds(end_card_s)),
            ],
        )
    ]
    for text, size, y, color in rows:
        if not single_line(text):
            continue
        chain.append(
            drawtext_filter(
                text,
                font_name=font_name,
                fonts=fonts,
                size=_fitted_size(text, max(8, round(size * scale)), max_width),
                color=color,
                x="(w-text_w)/2",
                y=str(round(y * scale)),
            )
        )
    tail = ["setsar=1", "format=yuv420p"]
    if logo_path is None:
        return [f"{','.join([*chain, *tail])}[{out}]"]
    side = _logo_side(scale)
    logo = [
        render_filter("movie", [("filename", normalize_filter_path(str(logo_path)))]),
        render_filter("scale", [("w", str(side)), ("h", str(side))]),
        "format=rgba",
    ]
    overlay = render_filter("overlay", [("x", "(W-w)/2"), ("y", str(round(END_CARD_LOGO_Y * scale)))])
    return [
        f"{','.join(chain)}[{out}_card]",
        f"{','.join(logo)}[{out}_logo]",
        f"[{out}_card][{out}_logo]{','.join([overlay, *tail])}[{out}]",
    ]


def _logo_side(scale: float) -> int:
    return max(2, int(END_CARD_LOGO_SIZE * scale // 2) * 2)

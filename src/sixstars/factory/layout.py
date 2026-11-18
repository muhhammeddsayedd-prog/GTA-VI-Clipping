"""Pure layout maths for the 9:16 frame: where gameplay, facecam, subtitles and branding go. Implemented in
M06.

Positions come from docs/09 sections 3 to 5 and are written for the 1080x1920 reference canvas. ASS files
always use that canvas (libass scales them); the ffmpeg filters use `Geometry`, the same numbers scaled to
the configured output size.

Three frames exist (`LayoutKind`): "A" gameplay on top and the facecam below (docs/09), "C" the facecam on
top and the gameplay below (decision D112, docs/27 section 5, the look of the owner's references) and "B"
no facecam. A and C are both `LayoutA` (a facecam was found); the style decides which one is drawn.
"""

from dataclasses import dataclass
from typing import Literal

from sixstars.factory.escape import render_filter
from sixstars.factory.window import ZOOM_MAX, zoom_start_s

LayoutKind = Literal["A", "B", "C"]

REF_WIDTH = 1080
REF_HEIGHT = 1920

# Layout A: gameplay 1080x1150 at y=0, facecam 1080x770 at y=1150, 6 px seam at y=1147.
A_GAMEPLAY_H = 1150
A_SEAM_Y = 1147
A_SEAM_H = 6
# Layout C (D112): facecam 1080x672 at y=0 (about 35% of the frame, as in the references), gameplay
# 1080x1248 below it, 6 px seam at y=669.
C_FACECAM_H = 672
C_SEAM_H = 6
# Layout B: blurred cover background darkened 30%, 4:5 gameplay 1080x1350 at y=285.
B_GAMEPLAY_H = 1350
B_GAMEPLAY_Y = 285
B_BLUR_RADIUS = 30
B_BLUR_POWER = 3
# The blur runs on a frame this many times smaller and is scaled back up: same look, a fraction of the cost.
B_BLUR_DOWNSCALE = 4
B_DARKEN = 0.3
# Subtitle line centre per layout.
CAPTION_Y_A = 1150
CAPTION_Y_B = 1450
# Layout C: captions in the lower half of the gameplay (the title sits on the seam), the payoff emoji in its
# upper half.
CAPTION_Y_C = 1400
PAYOFF_Y_C = 1000
# Layout C: the title's bottom edge sits this far below the seam, so the pill straddles the facecam's
# bottom edge like the references.
C_TITLE_OVERLAP = 20
# Overlays.
HOOK_TOP = 260
HOOK_BOTTOM = 420
BADGE_X = 40
BADGE_Y = 240
WATERMARK_RIGHT = 1040
WATERMARK_Y = 240
PROGRESS_Y = 1500
PROGRESS_H = 8


@dataclass(frozen=True, slots=True)
class Box:
    """A rectangle in source-video pixels."""

    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0 or self.w <= 0 or self.h <= 0:
            raise ValueError(f"invalid box {self}")


@dataclass(frozen=True, slots=True)
class LayoutA:
    """Facecam found: gameplay on top and facecam below (kind "A"), or with `cam_top` the facecam on top
    and the gameplay below (kind "C"). `gameplay_box` None means the whole frame."""

    facecam_box: Box
    gameplay_box: Box | None = None
    cam_top: bool = False

    @property
    def kind(self) -> LayoutKind:
        return "C" if self.cam_top else "A"


@dataclass(frozen=True, slots=True)
class LayoutB:
    """No facecam: blurred fill with the 4:5 centre of the source on top."""

    @property
    def kind(self) -> LayoutKind:
        return "B"


Layout = LayoutA | LayoutB


def _even(value: float) -> int:
    return max(2, int(value // 2) * 2)


@dataclass(frozen=True, slots=True)
class Geometry:
    """The docs/09 pixel positions scaled to an output canvas (identical to the docs at 1080x1920)."""

    width: int
    height: int
    a_gameplay_h: int
    a_facecam_h: int
    a_seam_y: int
    a_seam_h: int
    c_facecam_h: int
    c_gameplay_h: int
    c_seam_y: int
    b_gameplay_h: int
    b_gameplay_y: int
    b_blur_radius: int
    progress_y: int
    progress_h: int
    watermark_right: int
    watermark_y: int

    @classmethod
    def for_canvas(cls, width: int, height: int) -> "Geometry":
        sx, sy = width / REF_WIDTH, height / REF_HEIGHT
        a_gameplay_h = _even(A_GAMEPLAY_H * sy)
        b_gameplay_h = min(_even(B_GAMEPLAY_H * sy), height)
        c_facecam_h = _even(C_FACECAM_H * sy)
        return cls(
            width=width,
            height=height,
            a_gameplay_h=a_gameplay_h,
            a_facecam_h=height - a_gameplay_h,
            a_seam_y=a_gameplay_h - max(1, round(A_SEAM_H * sy / 2)),
            a_seam_h=max(2, round(A_SEAM_H * sy)),
            c_facecam_h=c_facecam_h,
            c_gameplay_h=height - c_facecam_h,
            c_seam_y=c_facecam_h - max(1, round(C_SEAM_H * sy / 2)),
            b_gameplay_h=b_gameplay_h,
            b_gameplay_y=min(round(B_GAMEPLAY_Y * sy), height - b_gameplay_h),
            b_blur_radius=max(1, round(B_BLUR_RADIUS * sy)),
            progress_y=round(PROGRESS_Y * sy),
            progress_h=max(2, round(PROGRESS_H * sy)),
            watermark_right=round(WATERMARK_RIGHT * sx),
            watermark_y=round(WATERMARK_Y * sy),
        )

    def seam_y(self, kind: LayoutKind) -> int:
        """Top row of the seam between gameplay and facecam (layouts A and C)."""
        return self.c_seam_y if kind == "C" else self.a_seam_y


def gameplay_band(kind: LayoutKind | None) -> tuple[float, float]:
    """The part of the frame (fractions of its height, top and bottom) that shows nothing but moving
    gameplay: the gate looks there for a frozen or black source (`gate.visual`). Below the hook or title
    and above the captions and the credit line of the layout. None (a clip rendered before layout C
    existed) gives the band of layouts A and B."""
    if kind == "C":
        return (C_FACECAM_H + 60) / REF_HEIGHT, (CAPTION_Y_C - 250) / REF_HEIGHT
    return (HOOK_BOTTOM + 60) / REF_HEIGHT, (min(CAPTION_Y_A, CAPTION_Y_B) - 150) / REF_HEIGHT


def fit_crop(box: Box, aspect_w: int, aspect_h: int, source_w: int, source_h: int) -> Box:
    """The largest centred rectangle of the given aspect inside `box` (clamped to the source frame).

    Sizes and offsets are even so the crop is valid for 4:2:0 video.
    """
    x0, y0 = min(box.x, source_w - 2), min(box.y, source_h - 2)
    avail_w, avail_h = min(box.w, source_w - x0), min(box.h, source_h - y0)
    if avail_w * aspect_h > avail_h * aspect_w:
        h = _even(avail_h)
        w = _even(h * aspect_w / aspect_h)
    else:
        w = _even(avail_w)
        h = _even(w * aspect_h / aspect_w)
    w, h = min(w, _even(avail_w)), min(h, _even(avail_h))
    x = x0 + int((avail_w - w) // 4) * 2
    y = y0 + int((avail_h - h) // 4) * 2
    return Box(x, y, w, h)


def gameplay_crop(layout: LayoutA, aspect_w: int, aspect_h: int, source_w: int, source_h: int) -> Box:
    """Layout A gameplay crop: centre-weighted inside the gameplay box.

    Without a gameplay box the whole frame is used, and the crop slides sideways just far enough to leave
    out an overlaid facecam when the frame allows it.
    """
    if layout.gameplay_box is not None:
        return fit_crop(layout.gameplay_box, aspect_w, aspect_h, source_w, source_h)
    crop = fit_crop(Box(0, 0, source_w, source_h), aspect_w, aspect_h, source_w, source_h)
    cam = layout.facecam_box
    overlaps = (
        cam.x < crop.x + crop.w
        and crop.x < cam.x + cam.w
        and cam.y < crop.y + crop.h
        and crop.y < cam.y + cam.h
    )
    if not overlaps:
        return crop
    candidates = [x for x in (cam.x - crop.w, cam.x + cam.w) if 0 <= x <= source_w - crop.w]
    if not candidates:
        return crop
    best = min(candidates, key=lambda x: abs(x - crop.x))
    return Box(int(best // 2) * 2, crop.y, crop.w, crop.h)


def _number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _crop(box: Box) -> str:
    return render_filter("crop", [("w", str(box.w)), ("h", str(box.h)), ("x", str(box.x)), ("y", str(box.y))])


@dataclass(frozen=True, slots=True)
class ZoomCut:
    """docs/27 step zoom: the frame alternates between 100% and `scale` every `every_s` seconds."""

    every_s: float
    scale: float

    def __post_init__(self) -> None:
        if self.every_s <= 0 or self.scale < 1:
            raise ValueError("a zoom cut needs every_s > 0 and scale >= 1")


@dataclass(frozen=True, slots=True)
class PunchIns:
    """docs/27 punch-ins: hard cuts to a full-frame close-up during `spans` (clip time, seconds).

    The close-up is the facecam in layout A and the centre of the source in layout B, cropped to the frame's
    aspect and enlarged by `zoom`.
    """

    spans: tuple[tuple[float, float], ...]
    zoom: float = 1.0

    def __post_init__(self) -> None:
        if self.zoom < 1:
            raise ValueError("punch-in zoom must be at least 1")
        if any(end <= start for start, end in self.spans):
            raise ValueError("punch-in spans must have end > start")

    def expression(self) -> str:
        """An ffmpeg expression that is non-zero during any punch-in."""
        return "+".join(f"between(t,{_number(start)},{_number(end)})" for start, end in self.spans)


def scale_filters(
    width: int, height: int, duration_s: float, *, zoom: bool, zoom_cut: ZoomCut | None = None
) -> list[str]:
    """Scale to `width`x`height`. With `zoom`, grow from 100% to 108% over the last 40% of the clip, or
    with `zoom_cut`, step between 100% and its scale every `every_s` seconds.

    The zoom scales each frame slightly larger and crops the centre back to the target size.
    """
    if not zoom:
        return [render_filter("scale", [("w", str(width)), ("h", str(height))]), "setsar=1"]
    if zoom_cut is not None:
        step = _number(zoom_cut.scale - 1)
        factor = f"(1+{step}*mod(floor(t/{_number(zoom_cut.every_s)}),2))"
    else:
        start = zoom_start_s(duration_s)
        span = max(duration_s - start, 0.001)
        factor = f"(1+{_number(ZOOM_MAX - 1)}*clip((t-{_number(start)})/{_number(span)},0,1))"
    zoomed_w, zoomed_h = f"2*trunc({width}*{factor}/2)", f"2*trunc({height}*{factor}/2)"
    return [
        render_filter("scale", [("w", zoomed_w), ("h", zoomed_h), ("eval", "frame")]),
        render_filter(
            "crop",
            [
                ("w", str(width)),
                ("h", str(height)),
                ("x", f"max(0,({zoomed_w}-{width})/2)"),
                ("y", f"max(0,({zoomed_h}-{height})/2)"),
            ],
        ),
        "setsar=1",
    ]


def layout_filters(
    layout: Layout,
    *,
    source_w: int,
    source_h: int,
    geometry: Geometry,
    fps: int,
    duration_s: float,
    zoom: bool = True,
    source: str = "0:v",
    out: str = "v_layout",
    zoom_cut: ZoomCut | None = None,
    punch_ins: PunchIns | None = None,
) -> list[str]:
    """Filter chains from the source video link to a `geometry.width`x`geometry.height` frame.

    With `punch_ins`, a full-frame close-up is cut in over the layout during each span.
    """
    g = geometry
    punch = punch_ins if punch_ins is not None and punch_ins.spans else None
    branches = 3 if punch is not None else 2
    head = f"[{source}]{render_filter('fps', [('fps', str(fps))])},split={branches}"
    base_out = f"{out}_base" if punch is not None else out
    if isinstance(layout, LayoutA):
        game_h = g.c_gameplay_h if layout.cam_top else g.a_gameplay_h
        face_h = g.c_facecam_h if layout.cam_top else g.a_facecam_h
        game = gameplay_crop(layout, g.width, game_h, source_w, source_h)
        face = fit_crop(layout.facecam_box, g.width, face_h, source_w, source_h)
        game_chain = [
            _crop(game),
            *scale_filters(g.width, game_h, duration_s, zoom=zoom, zoom_cut=zoom_cut),
        ]
        face_chain = [_crop(face), *scale_filters(g.width, face_h, duration_s, zoom=False)]
        stack = "[v_face][v_game]" if layout.cam_top else "[v_game][v_face]"
        chains = [
            f"{head}[v_game_in][v_face_in]" + ("[v_punch_in]" if punch else ""),
            f"[v_game_in]{','.join(game_chain)}[v_game]",
            f"[v_face_in]{','.join(face_chain)}[v_face]",
            f"{stack}vstack=inputs=2[{base_out}]",
        ]
        if punch is not None:
            close_up = fit_crop(layout.facecam_box, g.width, g.height, source_w, source_h)
            chains += _punch_filters(close_up, punch, g, base_out, out)
        return chains
    game = fit_crop(Box(0, 0, source_w, source_h), g.width, g.b_gameplay_h, source_w, source_h)
    small_w, small_h = _even(g.width / B_BLUR_DOWNSCALE), _even(g.height / B_BLUR_DOWNSCALE)
    background = [
        render_filter(
            "scale",
            [("w", str(small_w)), ("h", str(small_h)), ("force_original_aspect_ratio", "increase")],
        ),
        render_filter("crop", [("w", str(small_w)), ("h", str(small_h))]),
        render_filter(
            "boxblur",
            [
                ("luma_radius", str(max(1, round(g.b_blur_radius / B_BLUR_DOWNSCALE)))),
                ("luma_power", str(B_BLUR_POWER)),
            ],
        ),
        # Darkened while the frame is still small: drawbox with alpha on the full frame took most of the
        # render time of layout B.
        render_filter("drawbox", [("color", f"black@{B_DARKEN:g}"), ("thickness", "fill")]),
        render_filter("scale", [("w", str(g.width)), ("h", str(g.height)), ("flags", "bilinear")]),
        "setsar=1",
    ]
    game_chain = [
        _crop(game),
        *scale_filters(g.width, g.b_gameplay_h, duration_s, zoom=zoom, zoom_cut=zoom_cut),
    ]
    overlay = render_filter("overlay", [("x", "0"), ("y", str(g.b_gameplay_y))])
    chains = [
        f"{head}[v_bg_in][v_game_in]" + ("[v_punch_in]" if punch else ""),
        f"[v_bg_in]{','.join(background)}[v_bg]",
        f"[v_game_in]{','.join(game_chain)}[v_game]",
        f"[v_bg][v_game]{overlay}[{base_out}]",
    ]
    if punch is not None:
        close_up = fit_crop(Box(0, 0, source_w, source_h), g.width, g.height, source_w, source_h)
        chains += _punch_filters(close_up, punch, g, base_out, out)
    return chains


def _punch_filters(close_up: Box, punch: PunchIns, geometry: Geometry, base: str, out: str) -> list[str]:
    """The close-up branch, cropped and enlarged to fill the frame, laid over `base` during the punch-ins."""
    w, h = geometry.width, geometry.height
    big_w, big_h = _even(w * punch.zoom), _even(h * punch.zoom)
    chain = [
        _crop(close_up),
        render_filter("scale", [("w", str(big_w)), ("h", str(big_h))]),
        render_filter("crop", [("w", str(w)), ("h", str(h))]),
        "setsar=1",
    ]
    overlay = render_filter("overlay", [("x", "0"), ("y", "0"), ("enable", punch.expression())])
    return [
        f"[v_punch_in]{','.join(chain)}[v_punch]",
        f"[{base}][v_punch]{overlay}[{out}]",
    ]

"""Pure layout maths for the 9:16 frame: where gameplay, facecam, subtitles and branding go. Implemented in
M06.

Positions come from docs/09 sections 3 to 5 and are written for the 1080x1920 reference canvas. ASS files
always use that canvas (libass scales them); the ffmpeg filters use `Geometry`, the same numbers scaled to
the configured output size.

Three frames exist (`LayoutKind`): "A" gameplay on top and the facecam below (docs/09), "C" the facecam on
top and the gameplay below (decision D112, docs/27 section 5, the look of the owner's references) and "B"
no facecam. A and C are both `LayoutA` (a facecam was found); the style decides which one is drawn.
Two frames come from the style alone (development-plan/M18 Phase B): "H" the news card (`LayoutCard`) and
"P" the POV crop (`LayoutPov`).

Render v2 (D119, development-plan/M19) adds the two frames that replace all of these when
`factory.layout_v2.enabled` is on: "G" (`LayoutG`, a verified facecam: the cam panel 1080x`cam_h` on top,
the gameplay below) and "G0" (`LayoutG0`, no verified cam: 4:5 gameplay over the blurred copy). Their
gameplay crop slides clear of the facecam and the streamer's overlays (`gameplay_crop_v2`). The old frames
stay until R7.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from sixstars.factory.escape import render_filter

LayoutKind = Literal["A", "B", "C", "H", "P", "M", "G", "G0"]

# ffmpeg scaler names allowed in `factory.layout_v2.scaler` (Render v2, D119). Without the setting the
# renders keep ffmpeg's own default, bicubic.
SCALERS = ("bicubic", "bilinear", "lanczos", "spline", "area")
DEFAULT_SCALER = "bicubic"
# Render v2: no panel of layout G or G0 is shorter than this (reference pixels).
MIN_PANEL_H = 200

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
# Layout H (news card, reference R09): black frame, brand header and headline drawn from y=300 (below the
# LIVE badge), the stream as a 4:3 crop 1080x810 at y=630, the credit under it.
H_HEADER_TOP = 300
H_VIDEO_Y = 630
H_VIDEO_H = 810
# Layout P (POV, reference R08): the gameplay as a 3:4 crop 1080x1440 at y=240 between black bars, the
# line centred on y=900.
P_VIDEO_Y = 240
P_VIDEO_H = 1440
P_TEXT_CENTER_Y = 900
# Layout M (montage, reference R10): a 3:4 letterbox 1080x1440 at y=240: gameplay 1080x720 on top, the
# facecam 1080x720 below, a soft dark fade over the seam at y=960 (where the captions sit), the credit
# at the top of the gameplay.
M_TOP = 240
M_GAME_H = 720
M_FACE_H = 720
M_SEAM_Y = M_TOP + M_GAME_H
M_FADE_H = 220
M_CREDIT_BOTTOM = M_TOP + 52
# Layouts H and P: the credit line's bottom edge (no captions there to sit above).
LOW_CREDIT_BOTTOM = 1480
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
# Layout C: a question title (style cam_top_question) starts this far below the seam, at the top of the
# gameplay; the gate band starts below the tallest one (two lines of `cards.QUESTION_SIZES[0]`) and ends
# above the credit line.
C_QUESTION_GAP = 40
C_BAND_TOP = 960
C_BAND_BOTTOM = 1240
# Render v2 gate band: the G band ends this far above the caption centre and is never thinner than
# V2_BAND_MIN (reference pixels).
V2_BAND_CAPTION_CLEAR = 160
V2_BAND_MIN = 100
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


@dataclass(frozen=True, slots=True)
class LayoutCard:
    """The news card (kind "H"): the stream as the streamer had it, cropped to 4:3 so that their facecam
    (when one was found) stays in the picture."""

    facecam_box: Box | None = None

    @property
    def kind(self) -> LayoutKind:
        return "H"


@dataclass(frozen=True, slots=True)
class LayoutPov:
    """The POV crop (kind "P"): the gameplay alone, cropped to 3:4 away from the facecam when one was
    found."""

    facecam_box: Box | None = None

    @property
    def kind(self) -> LayoutKind:
        return "P"


@dataclass(frozen=True, slots=True)
class LayoutMontage:
    """The montage (kind "M"): gameplay above the facecam in a 3:4 letterbox; B-roll cuts are laid over
    the gameplay panel (`BrollCut`)."""

    facecam_box: Box

    @property
    def kind(self) -> LayoutKind:
        return "M"


@dataclass(frozen=True, slots=True)
class BrollCut:
    """One B-roll insert of layout M: `duration_s` of the source from `source_start_s` (file time, read
    as its own ffmpeg input `input_index`) over the gameplay panel from `at_s` (clip time)."""

    input_index: int
    source_start_s: float
    at_s: float
    duration_s: float


@dataclass(frozen=True, slots=True)
class LayoutV2Rules:
    """The setting `factory.layout_v2` (Render v2, D119; docs/16). Pixel values are on the 1080x1920
    reference canvas; `Geometry` scales them to the output.

    * `enabled`: the master switch. Off, the frames of `factory.styles` are drawn as before.
    * `cam_h`: height of the cam panel of layout G (the gameplay takes the rest of the frame).
    * `g0_game_h` / `g0_game_y`: the gameplay of layout G0 and its top.
    * `max_slide`: how far (share of the source width) the gameplay crop may move from the centre to stay
      clear of the facecam and the overlays.
    * `seam_y`, `safe_right_x`, `safe_bottom_y`: where text goes (R4).
    * `cam_face_*`, `cam_fill_*`, `cam_unsharp`: the face-aware cam panel (R2).
    * `scaler`: the ffmpeg scaler of every crop (also used while `enabled` is off).
    """

    enabled: bool = False
    cam_h: int = 720
    g0_game_h: int = 1350
    g0_game_y: int = 285
    max_slide: float = 0.25
    seam_y: int = 720
    safe_right_x: int = 920
    safe_bottom_y: int = 1540
    cam_face_center_y: float = 0.45
    cam_face_h_min: float = 0.40
    cam_face_h_max: float = 0.60
    cam_fill_blur: int = 30
    cam_fill_darken: float = 0.30
    cam_unsharp: float = 0.5
    scaler: str = DEFAULT_SCALER

    def __post_init__(self) -> None:
        if not MIN_PANEL_H <= self.cam_h <= REF_HEIGHT - MIN_PANEL_H:
            raise ValueError("cam_h leaves no room for the cam or the gameplay")
        if self.g0_game_h < MIN_PANEL_H or self.g0_game_y < 0 or self.g0_game_y + self.g0_game_h > REF_HEIGHT:
            raise ValueError("the G0 gameplay must fit the frame")
        if not 0 <= self.max_slide <= 0.5:
            raise ValueError("max_slide must be within 0..0.5")
        if not 0 < self.seam_y < REF_HEIGHT or not 0 < self.safe_right_x <= REF_WIDTH:
            raise ValueError("seam_y and safe_right_x must be inside the frame")
        if not 0 < self.safe_bottom_y <= REF_HEIGHT:
            raise ValueError("safe_bottom_y must be inside the frame")
        if not 0 < self.cam_face_h_min <= self.cam_face_h_max < 1 or not 0 < self.cam_face_center_y < 1:
            raise ValueError("the cam face shares must satisfy 0 < min <= max < 1")
        if self.cam_fill_blur < 0 or not 0 <= self.cam_fill_darken < 1 or not 0 <= self.cam_unsharp <= 3:
            raise ValueError("cam fill blur, darken or unsharp out of range")
        if self.scaler not in SCALERS:
            raise ValueError(f"scaler must be one of {SCALERS}")

    @classmethod
    def from_setting(cls, raw: object) -> "LayoutV2Rules":
        """The rules from the JSON object of the setting. A missing key keeps its default; a missing,
        malformed or invalid setting gives the defaults, which have the switch off. Pure."""
        if not isinstance(raw, Mapping):
            return cls()
        base = cls()
        values: dict[str, object] = {}
        for name in cls.__dataclass_fields__:
            fallback = getattr(base, name)
            value = raw.get(name, fallback)
            if isinstance(fallback, bool):
                values[name] = value if isinstance(value, bool) else fallback
            elif isinstance(fallback, int):
                ok = (
                    isinstance(value, int | float)
                    and not isinstance(value, bool)
                    and float(value).is_integer()
                )
                values[name] = int(value) if ok else fallback
            elif isinstance(fallback, float):
                ok = isinstance(value, int | float) and not isinstance(value, bool)
                values[name] = float(value) if ok else fallback
            else:
                values[name] = value if isinstance(value, str) else fallback
        try:
            return cls(**values)  # type: ignore[arg-type]
        except ValueError:
            return base


@dataclass(frozen=True, slots=True)
class LayoutG:
    """Render v2 frame G: a verified facecam. The cam panel shows `facecam_box` (framed around `face_box`
    from R2 on), the gameplay below slides clear of the facecam and `overlay_boxes` (the streamer's chat,
    alerts and panels, from R3 on)."""

    facecam_box: Box
    face_box: Box | None = None
    overlay_boxes: tuple[Box, ...] = ()

    @property
    def kind(self) -> LayoutKind:
        return "G"


@dataclass(frozen=True, slots=True)
class LayoutG0:
    """Render v2 frame G0: no verified facecam. 4:5 gameplay over a blurred, darkened copy of the frame,
    sliding clear of `overlay_boxes`."""

    overlay_boxes: tuple[Box, ...] = ()

    @property
    def kind(self) -> LayoutKind:
        return "G0"


Layout = LayoutA | LayoutB | LayoutCard | LayoutPov | LayoutMontage | LayoutG | LayoutG0


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
    h_video_y: int
    h_video_h: int
    p_video_y: int
    p_video_h: int
    m_top: int
    m_game_h: int
    m_face_h: int
    b_gameplay_h: int
    b_gameplay_y: int
    b_blur_radius: int
    progress_y: int
    progress_h: int
    watermark_right: int
    watermark_y: int
    # Render v2 (`LayoutV2Rules`).
    g_cam_h: int = 0
    g_game_h: int = 0
    g0_game_h: int = 0
    g0_game_y: int = 0

    @classmethod
    def for_canvas(cls, width: int, height: int, v2: "LayoutV2Rules | None" = None) -> "Geometry":
        """The positions for a `width`x`height` output; layouts G and G0 from `v2` (its defaults when
        None)."""
        v2 = v2 or LayoutV2Rules()
        sx, sy = width / REF_WIDTH, height / REF_HEIGHT
        g_cam_h = _even(v2.cam_h * sy)
        g0_game_h = min(_even(v2.g0_game_h * sy), height)
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
            h_video_y=_even(H_VIDEO_Y * sy),
            h_video_h=min(_even(H_VIDEO_H * sy), height - _even(H_VIDEO_Y * sy)),
            p_video_y=_even(P_VIDEO_Y * sy),
            p_video_h=min(_even(P_VIDEO_H * sy), height - _even(P_VIDEO_Y * sy)),
            m_top=_even(M_TOP * sy),
            m_game_h=_even(M_GAME_H * sy),
            m_face_h=_even(M_FACE_H * sy),
            b_gameplay_h=b_gameplay_h,
            b_gameplay_y=min(round(B_GAMEPLAY_Y * sy), height - b_gameplay_h),
            b_blur_radius=max(1, round(B_BLUR_RADIUS * sy)),
            progress_y=round(PROGRESS_Y * sy),
            progress_h=max(2, round(PROGRESS_H * sy)),
            watermark_right=round(WATERMARK_RIGHT * sx),
            watermark_y=round(WATERMARK_Y * sy),
            g_cam_h=g_cam_h,
            g_game_h=height - g_cam_h,
            g0_game_h=g0_game_h,
            g0_game_y=min(round(v2.g0_game_y * sy), height - g0_game_h),
        )

    def seam_y(self, kind: LayoutKind) -> int:
        """Top row of the seam between gameplay and facecam (layouts A, C and G)."""
        if kind == "G":
            return self.g_cam_h
        return self.c_seam_y if kind == "C" else self.a_seam_y


def gameplay_band(kind: LayoutKind | None, v2: LayoutV2Rules | None = None) -> tuple[float, float]:
    """The part of the frame (fractions of its height, top and bottom) that shows nothing but moving
    gameplay: the gate looks there for a frozen or black source (`gate.visual`). Below the hook or title
    and above the captions and the credit line of the layout. None (a clip rendered before layout C
    existed) gives the band of layouts A and B."""
    if kind == "C":
        return C_BAND_TOP / REF_HEIGHT, C_BAND_BOTTOM / REF_HEIGHT
    if kind in ("G", "G0"):
        v2 = v2 or LayoutV2Rules()
        if kind == "G":
            # Below the title on the seam (and a question title under it), above the captions.
            top = v2.cam_h + (C_BAND_TOP - C_FACECAM_H)
            bottom = max(CAPTION_Y_C - V2_BAND_CAPTION_CLEAR, top + V2_BAND_MIN)
        else:
            top = max(HOOK_BOTTOM + 60, v2.g0_game_y + 60)
            bottom = min(CAPTION_Y_B - 150, v2.g0_game_y + v2.g0_game_h - 60)
            bottom = max(bottom, top + V2_BAND_MIN)
        return top / REF_HEIGHT, min(bottom, REF_HEIGHT) / REF_HEIGHT
    if kind == "H":
        return (H_VIDEO_Y + 40) / REF_HEIGHT, (H_VIDEO_Y + H_VIDEO_H - 40) / REF_HEIGHT
    if kind == "P":
        return (P_VIDEO_Y + 60) / REF_HEIGHT, (P_TEXT_CENTER_Y - 140) / REF_HEIGHT
    if kind == "M":
        return (M_CREDIT_BOTTOM + 40) / REF_HEIGHT, (M_SEAM_Y - M_FADE_H // 2 - 20) / REF_HEIGHT
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


def card_crop(facecam: Box | None, source_w: int, source_h: int) -> Box:
    """Layout H: the largest centred 4:3 crop of the source, moved sideways just far enough to keep the
    facecam in it when it fits. Pure."""
    crop = fit_crop(Box(0, 0, source_w, source_h), 4, 3, source_w, source_h)
    if facecam is None or facecam.w > crop.w:
        return crop
    x = crop.x
    if facecam.x < x:
        x = facecam.x
    elif facecam.x + facecam.w > x + crop.w:
        x = facecam.x + facecam.w - crop.w
    x = min(max(0, x), source_w - crop.w)
    return Box(int(x // 2) * 2, crop.y, crop.w, crop.h)


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


def overlap_area(a: Box, b: Box) -> int:
    """Area shared by two boxes (0 when they do not overlap). Pure."""
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return w * h if w > 0 and h > 0 else 0


def gameplay_crop_v2(
    avoid: Sequence[Box], aspect_w: int, aspect_h: int, source_w: int, source_h: int, max_slide: float
) -> Box:
    """Render v2 gameplay crop (layouts G and G0): the largest crop of the aspect at full source height,
    centred, then slid sideways to the nearest position that overlaps none of `avoid` (the facecam and the
    overlays), at most `max_slide` x the source width from the centre. With no clear position, the one
    with the least overlapping area (the nearest of equals). Even offsets; one crop for the whole clip.
    Pure."""
    crop = fit_crop(Box(0, 0, source_w, source_h), aspect_w, aspect_h, source_w, source_h)
    centre = crop.x
    reach = int(max_slide * source_w)
    low, high = max(0, centre - reach), min(source_w - crop.w, centre + reach)
    # The overlap is piecewise linear in x, so its minimum lies at a range end or where an edge of the
    # crop meets an edge of a box.
    raw = {low, high, centre}
    for box in avoid:
        raw |= {box.x + box.w, box.x - crop.w, box.x, box.x + box.w - crop.w}
    candidates = {even for x in raw for even in (x // 2 * 2, x // 2 * 2 + 2) if low <= even <= high}
    if not candidates:
        return crop

    def cost(x: int) -> tuple[int, int]:
        moved = Box(x, crop.y, crop.w, crop.h)
        return sum(overlap_area(moved, box) for box in avoid), abs(x - centre)

    best = min(sorted(candidates), key=cost)
    return Box(best, crop.y, crop.w, crop.h)


def _number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _crop(box: Box) -> str:
    return render_filter("crop", [("w", str(box.w)), ("h", str(box.h)), ("x", str(box.x)), ("y", str(box.y))])


def scale_filters(width: int, height: int, *, scaler: str = DEFAULT_SCALER) -> list[str]:
    """Scale to `width`x`height` with the named ffmpeg scaler. Static: nothing changes over time (Render v2,
    D119: no zoom of any kind)."""
    if scaler not in SCALERS:
        raise ValueError(f"unknown scaler {scaler!r}")
    return [render_filter("scale", [("w", str(width)), ("h", str(height)), ("flags", scaler)]), "setsar=1"]


def layout_filters(
    layout: Layout,
    *,
    source_w: int,
    source_h: int,
    geometry: Geometry,
    fps: int,
    source: str = "0:v",
    out: str = "v_layout",
    broll: Sequence[BrollCut] = (),
    scaler: str = DEFAULT_SCALER,
    max_slide: float | None = None,
) -> list[str]:
    """Filter chains from the source video link to a `geometry.width`x`geometry.height` frame. Every crop
    and scale is fixed for the whole clip (D119). `max_slide` bounds the gameplay slide of layouts G and
    G0."""
    g = geometry
    if isinstance(layout, LayoutG | LayoutG0):
        return _v2_filters(
            layout,
            source_w=source_w,
            source_h=source_h,
            geometry=g,
            fps=fps,
            source=source,
            out=out,
            scaler=scaler,
            max_slide=LayoutV2Rules().max_slide if max_slide is None else max_slide,
        )
    if isinstance(layout, LayoutMontage):
        return _montage_filters(
            layout,
            source_w=source_w,
            source_h=source_h,
            geometry=g,
            fps=fps,
            source=source,
            out=out,
            broll=broll,
            scaler=scaler,
        )
    if isinstance(layout, LayoutCard | LayoutPov):
        return _framed_filters(
            layout,
            source_w=source_w,
            source_h=source_h,
            geometry=g,
            fps=fps,
            source=source,
            out=out,
            scaler=scaler,
        )
    head = f"[{source}]{render_filter('fps', [('fps', str(fps))])},split=2"
    if isinstance(layout, LayoutA):
        game_h = g.c_gameplay_h if layout.cam_top else g.a_gameplay_h
        face_h = g.c_facecam_h if layout.cam_top else g.a_facecam_h
        game = gameplay_crop(layout, g.width, game_h, source_w, source_h)
        face = fit_crop(layout.facecam_box, g.width, face_h, source_w, source_h)
        game_chain = [_crop(game), *scale_filters(g.width, game_h, scaler=scaler)]
        face_chain = [_crop(face), *scale_filters(g.width, face_h, scaler=scaler)]
        stack = "[v_face][v_game]" if layout.cam_top else "[v_game][v_face]"
        return [
            f"{head}[v_game_in][v_face_in]",
            f"[v_game_in]{','.join(game_chain)}[v_game]",
            f"[v_face_in]{','.join(face_chain)}[v_face]",
            f"{stack}vstack=inputs=2[{out}]",
        ]
    game = fit_crop(Box(0, 0, source_w, source_h), g.width, g.b_gameplay_h, source_w, source_h)
    background = blur_background(g)
    game_chain = [_crop(game), *scale_filters(g.width, g.b_gameplay_h, scaler=scaler)]
    overlay = render_filter("overlay", [("x", "0"), ("y", str(g.b_gameplay_y))])
    return [
        f"{head}[v_bg_in][v_game_in]",
        f"[v_bg_in]{','.join(background)}[v_bg]",
        f"[v_game_in]{','.join(game_chain)}[v_game]",
        f"[v_bg][v_game]{overlay}[{out}]",
    ]


def blur_background(geometry: Geometry) -> list[str]:
    """The cover background of layouts B and G0: the frame scaled to cover the canvas, blurred and darkened
    on a smaller copy, scaled back up."""
    g = geometry
    small_w, small_h = _even(g.width / B_BLUR_DOWNSCALE), _even(g.height / B_BLUR_DOWNSCALE)
    return [
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


def v2_gameplay_crop(layout: "LayoutG | LayoutG0", geometry: Geometry, source_w: int, source_h: int,
                     max_slide: float) -> Box:  # fmt: skip
    """The source crop of the gameplay panel of layout G or G0. Pure."""
    g = geometry
    if isinstance(layout, LayoutG):
        avoid = (layout.facecam_box, *layout.overlay_boxes)
        return gameplay_crop_v2(avoid, g.width, g.g_game_h, source_w, source_h, max_slide)
    return gameplay_crop_v2(layout.overlay_boxes, g.width, g.g0_game_h, source_w, source_h, max_slide)


def _v2_filters(
    layout: "LayoutG | LayoutG0",
    *,
    source_w: int,
    source_h: int,
    geometry: Geometry,
    fps: int,
    source: str,
    out: str,
    scaler: str,
    max_slide: float,
) -> list[str]:
    """Layouts G and G0 (Render v2). G: the cam panel stacked over the gameplay, no seam. G0: the 4:5
    gameplay over the blurred background. Both still for the whole clip."""
    g = geometry
    head = f"[{source}]{render_filter('fps', [('fps', str(fps))])},split=2"
    game = v2_gameplay_crop(layout, g, source_w, source_h, max_slide)
    if isinstance(layout, LayoutG):
        # Until R2 frames the panel around the face: the largest centred crop of the cam box.
        cam = fit_crop(layout.facecam_box, g.width, g.g_cam_h, source_w, source_h)
        cam_chain = [_crop(cam), *scale_filters(g.width, g.g_cam_h, scaler=scaler)]
        game_chain = [_crop(game), *scale_filters(g.width, g.g_game_h, scaler=scaler)]
        return [
            f"{head}[v_face_in][v_game_in]",
            f"[v_face_in]{','.join(cam_chain)}[v_face]",
            f"[v_game_in]{','.join(game_chain)}[v_game]",
            f"[v_face][v_game]vstack=inputs=2[{out}]",
        ]
    game_chain = [_crop(game), *scale_filters(g.width, g.g0_game_h, scaler=scaler)]
    overlay = render_filter("overlay", [("x", "0"), ("y", str(g.g0_game_y))])
    return [
        f"{head}[v_bg_in][v_game_in]",
        f"[v_bg_in]{','.join(blur_background(g))}[v_bg]",
        f"[v_game_in]{','.join(game_chain)}[v_game]",
        f"[v_bg][v_game]{overlay}[{out}]",
    ]


def _framed_filters(
    layout: "LayoutCard | LayoutPov",
    *,
    source_w: int,
    source_h: int,
    geometry: Geometry,
    fps: int,
    source: str,
    out: str,
    scaler: str,
) -> list[str]:
    """Layouts H and P: one crop of the source scaled to its band of the frame, black around it."""
    g = geometry
    if isinstance(layout, LayoutCard):
        crop = card_crop(layout.facecam_box, source_w, source_h)
        top, height = g.h_video_y, g.h_video_h
    else:
        top, height = g.p_video_y, g.p_video_h
        if layout.facecam_box is not None:
            crop = gameplay_crop(LayoutA(layout.facecam_box), g.width, height, source_w, source_h)
        else:
            crop = fit_crop(Box(0, 0, source_w, source_h), g.width, height, source_w, source_h)
    chain = [
        render_filter("fps", [("fps", str(fps))]),
        _crop(crop),
        *scale_filters(g.width, height, scaler=scaler),
        render_filter(
            "pad",
            [("w", str(g.width)), ("h", str(g.height)), ("x", "0"), ("y", str(top)), ("color", "black")],
        ),
        "setsar=1",
    ]
    return [f"[{source}]{','.join(chain)}[{out}]"]


def _montage_filters(
    layout: LayoutMontage,
    *,
    source_w: int,
    source_h: int,
    geometry: Geometry,
    fps: int,
    source: str,
    out: str,
    broll: Sequence[BrollCut],
    scaler: str,
) -> list[str]:
    """Layout M: the gameplay panel (with each B-roll cut laid over it for its span) stacked over the
    facecam panel, padded into the frame with black above and below."""
    g = geometry
    game_crop = gameplay_crop(LayoutA(layout.facecam_box), g.width, g.m_game_h, source_w, source_h)
    face_crop = fit_crop(layout.facecam_box, g.width, g.m_face_h, source_w, source_h)
    head = f"[{source}]{render_filter('fps', [('fps', str(fps))])},split=2[m_game_in][m_face_in]"
    game_chain = [_crop(game_crop), *scale_filters(g.width, g.m_game_h, scaler=scaler)]
    face_chain = [_crop(face_crop), *scale_filters(g.width, g.m_face_h, scaler=scaler)]
    chains = [
        head,
        f"[m_game_in]{','.join(game_chain)}[m_game_0]",
        f"[m_face_in]{','.join(face_chain)}[m_face]",
    ]
    current = "m_game_0"
    for n, cut in enumerate(broll):
        clip = [
            render_filter("trim", [("duration", _number(cut.duration_s))]),
            f"setpts=PTS-STARTPTS+{_number(cut.at_s)}/TB",
            render_filter("fps", [("fps", str(fps))]),
            _crop(game_crop),
            *scale_filters(g.width, g.m_game_h, scaler=scaler),
        ]
        until = _number(cut.at_s + cut.duration_s)
        overlay = render_filter(
            "overlay",
            [
                ("x", "0"),
                ("y", "0"),
                ("eof_action", "pass"),
                ("enable", f"between(t,{_number(cut.at_s)},{until})"),
            ],
        )
        chains.append(f"[{cut.input_index}:v]{','.join(clip)}[m_broll_{n}]")
        chains.append(f"[{current}][m_broll_{n}]{overlay}[m_game_{n + 1}]")
        current = f"m_game_{n + 1}"
    pad = render_filter(
        "pad",
        [("w", str(g.width)), ("h", str(g.height)), ("x", "0"), ("y", str(g.m_top)), ("color", "black")],
    )
    chains.append(f"[{current}][m_face]vstack=inputs=2,{pad},setsar=1[{out}]")
    return chains

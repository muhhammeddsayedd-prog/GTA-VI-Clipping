"""Stage 4 of the cover engine (docs/29a sections 6 and 7): compose a 1080x1920 cover from real frames.

Python prepares the pictures (all deterministic, no generated pixels): the facecam frame is cropped so the
face sits where the layout wants it, contrast and saturation get a mild lift (+10%), the face gets an
unsharp mask (radius 1.2, amount 0.6), the areas outside the panels are a blurred, darkened continuation of
the nearest panel, and the moment-led inset is the streamer cut out with MediaPipe's selfie segmenter. The
HTML/CSS templates in `templates/` set the line (Anton, white with one yellow key word, black outline, soft
shadow, auto-fit to 900 px wide with a cap height of 110-170 px and never under 96 px) or the TikTok-native
white box (Inter SemiBold 56-64 px), and the small VI mark. Playwright (headless Chromium, one browser kept
warm per process) renders the page; the result is saved as a JPEG (quality 90, under 2 MB) plus the 3:4
preview the profile grids show (the centre 1080x1440).
"""

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sixstars.factory.assets import assets_dir

CoverLayout = Literal["split", "face_led", "moment_led", "tiktok_native"]

CANVAS_W, CANVAS_H = 1080, 1920
PREVIEW_BOX = (0, 240, 1080, 1680)  # the centre 3:4 (1080x1440) the TikTok and Instagram grids show
SAFE_TOP, SAFE_BOTTOM = 400, 1520
# Panels (docs/29a 6.2).
SPLIT_FACE = (1080, 620)
SPLIT_GAME = (1080, 540)
SPLIT_FACE_SHARE, SPLIT_FACE_CENTRE = 0.42, 0.45
FACE_LED_SHARE = 0.47 * (SAFE_BOTTOM - SAFE_TOP) / CANVAS_H  # face 40-55% of the safe band
FACE_LED_CENTRE = 800 / CANVAS_H
FACE_LED_BAND_SHARE = (0.40, 0.55)
CUTOUT_FACE_SHARE, CUTOUT_ASPECT = 0.45, 0.8
LINE_Y: dict[str, int] = {"split": 1000, "tiktok_native": 1000, "face_led": 1300, "moment_led": 560}
# A facecam crop smaller than this (source pixels tall) is too soft to fill the whole canvas.
FACE_LED_MIN_CROP_PX = 400
# Finish (6.5).
CONTRAST, SATURATION = 1.10, 1.10
UNSHARP_RADIUS, UNSHARP_PERCENT = 1.2, 60
FILL_BLUR, FILL_DARKEN = 40, 0.65
JPEG_QUALITY, JPEG_MAX_BYTES = 90, 2_000_000
SEGMENTER_ENV = "SIXSTARS_SELFIE_SEGMENTER"
SEGMENTER_DEFAULT = "/models/mediapipe/selfie_segmenter.tflite"
TEMPLATES = Path(__file__).resolve().parent / "templates"

Rect = tuple[int, int, int, int]


# ── Pure geometry ─────────────────────────────────────────────


def _even(value: float) -> int:
    return max(2, int(value // 2) * 2)


def face_crop(
    face: Rect, image_w: int, image_h: int, panel_w: int, panel_h: int, share: float, centre_y: float
) -> Rect:
    """A crop of the panel's aspect that puts the face's centre at `centre_y` of the crop height and makes
    the face `share` of it, inside the image; as large as needed, as small as the face allows. Pure."""
    fx, fy, fw, fh = face
    aspect = panel_w / panel_h
    max_h = min(image_h, image_w / aspect)
    crop_h = min(max(fh / max(share, 0.05), 2.0), max_h)
    crop_w = crop_h * aspect
    w, h = min(_even(crop_w), _even(image_w)), min(_even(crop_h), _even(image_h))
    x = fx + fw / 2 - w / 2
    y = fy + fh / 2 - centre_y * h
    x = min(max(x, 0), image_w - w)
    y = min(max(y, 0), image_h - h)
    return int(x), int(y), w, h


def cover_crop(image_w: int, image_h: int, panel_w: int, panel_h: int, focus_x: float = 0.5) -> Rect:
    """The largest crop of the panel's aspect (object-fit: cover), centred (horizontally on `focus_x`).
    Pure."""
    aspect = panel_w / panel_h
    if image_w / image_h > aspect:
        h = image_h
        w = round(h * aspect)
    else:
        w = image_w
        h = round(w / aspect)
    x = min(max(round(focus_x * image_w - w / 2), 0), image_w - w)
    y = (image_h - h) // 2
    return x, y, w, h


def sentence_case(text: str) -> str:
    """`FIRST CAR STOLEN` -> `First car stolen` (masked words keep their stars). Pure."""
    lowered = text.lower()
    return lowered[:1].upper() + lowered[1:]


def choose_layout(proposed: CoverLayout, face_crop_h: int | None, face_h: int | None = None) -> CoverLayout:
    """Face-led needs a facecam large enough to fill the canvas, and a face that lands at 40-55% of the
    safe band once scaled (a small webcam box cannot zoom out far enough); otherwise split. Pure."""
    if proposed != "face_led":
        return proposed
    if face_crop_h is None or face_crop_h < FACE_LED_MIN_CROP_PX:
        return "split"
    if face_h is not None:
        on_canvas = face_h * CANVAS_H / face_crop_h
        band = SAFE_BOTTOM - SAFE_TOP
        if not FACE_LED_BAND_SHARE[0] * band <= on_canvas <= FACE_LED_BAND_SHARE[1] * band:
            return "split"
    return proposed


# ── Pictures (Pillow, MediaPipe) ──────────────────────────────


def _finish(image: Any, *, sharpen: bool) -> Any:
    from PIL import ImageEnhance, ImageFilter

    image = ImageEnhance.Contrast(image).enhance(CONTRAST)
    image = ImageEnhance.Color(image).enhance(SATURATION)
    if sharpen:
        image = image.filter(
            ImageFilter.UnsharpMask(radius=UNSHARP_RADIUS, percent=UNSHARP_PERCENT, threshold=0)
        )
    return image


def _crop_resize(image: Any, rect: Rect, size: tuple[int, int]) -> Any:
    from PIL import Image

    x, y, w, h = rect
    return image.crop((x, y, x + w, y + h)).resize(size, Image.Resampling.LANCZOS)


def _fill(image: Any) -> Any:
    from PIL import Image, ImageEnhance, ImageFilter

    small = image.resize((CANVAS_W // 4, max(2, round(image.height * CANVAS_W / 4 / image.width))))
    small = small.filter(ImageFilter.GaussianBlur(FILL_BLUR / 4))
    small = ImageEnhance.Brightness(small).enhance(FILL_DARKEN)
    return small.resize((CANVAS_W, max(2, round(small.height * 4))), Image.Resampling.BILINEAR)


_segmenter: Any = None


def _selfie_segmenter(model: str | None) -> Any:
    """The process's warm MediaPipe selfie segmenter."""
    global _segmenter
    if _segmenter is None:
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        path = model or os.environ.get(SEGMENTER_ENV, SEGMENTER_DEFAULT)
        options = vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_path=path), output_confidence_masks=True
        )
        _segmenter = vision.ImageSegmenter.create_from_options(options)
    return _segmenter


def cut_out(image: Any, model: str | None = None) -> Any:
    """The person in `image` on a transparent background (MediaPipe selfie segmenter, no generated
    pixels: the mask only removes the background)."""
    import mediapipe as mp
    import numpy as np
    from PIL import Image

    rgb = np.ascontiguousarray(np.asarray(image.convert("RGB")))
    result = _selfie_segmenter(model).segment(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    mask = np.squeeze(result.confidence_masks[0].numpy_view())
    alpha = (np.clip(mask, 0.0, 1.0) * 255).astype(np.uint8)
    cut = image.convert("RGBA")
    cut.putalpha(Image.fromarray(alpha).resize(cut.size))
    return cut


@dataclass(frozen=True, slots=True)
class CoverSpec:
    """One cover to compose: the frames (paths), the face box in the face frame, the layout and the text."""

    layout: CoverLayout
    face_path: Path | None
    game_path: Path | None
    face_box: Rect | None = None
    lines: tuple[str, ...] = ()
    key_word: str = ""
    brand_mark: bool = False
    # The line's centre (canvas y); None = the layout's place (`LINE_Y`).
    line_y: int | None = None


@dataclass(frozen=True, slots=True)
class ComposedCover:
    """The rendered cover and what the fit loop did."""

    jpeg: Path
    preview: Path
    layout: CoverLayout
    font_size: int
    cap_height: int
    fits: bool
    render_ms: int
    extras: Mapping[str, Any] = field(default_factory=dict)


def placed_face(face: Rect, crop: Rect, x0: int, y0: int, scale: float) -> Rect:
    """Where a face box of the source picture lands on the canvas after the crop and scale. Pure."""
    fx, fy, fw, fh = face
    cx, cy, cw, _ch = crop
    s = scale / cw
    return round(x0 + (fx - cx) * s), round(y0 + (fy - cy) * s), round(fw * s), round(fh * s)


def prepare(spec: CoverSpec, work: Path) -> tuple[CoverLayout, dict[str, str]]:
    """Write the prepared pictures for the template into `work` and return the layout used (face-led
    falls back to split for a small facecam) and the template's image paths."""
    from PIL import Image

    work.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    face_img = Image.open(spec.face_path).convert("RGB") if spec.face_path else None
    game_img = Image.open(spec.game_path).convert("RGB") if spec.game_path else None
    box = spec.face_box
    layout = spec.layout
    if face_img is not None and box is not None:
        led = face_crop(box, face_img.width, face_img.height, 9, 16, FACE_LED_SHARE, FACE_LED_CENTRE)
        layout = choose_layout(layout, led[3], box[3])
    elif layout in ("face_led", "moment_led") and face_img is None:
        layout = "split"

    def save(name: str, image: Any) -> None:
        path = work / f"{name}.png"
        image.save(path)
        files[name] = path.resolve().as_uri()

    if face_img is not None:
        if layout == "face_led":
            assert box is not None
            rect = face_crop(box, face_img.width, face_img.height, 9, 16, FACE_LED_SHARE, FACE_LED_CENTRE)
            save("face", _finish(_crop_resize(face_img, rect, (CANVAS_W, CANVAS_H)), sharpen=True))
            files["face_rect"] = ",".join(map(str, placed_face(box, rect, 0, 0, CANVAS_W)))
        elif layout in ("split", "tiktok_native"):
            rect = (
                face_crop(
                    box, face_img.width, face_img.height, *SPLIT_FACE, SPLIT_FACE_SHARE, SPLIT_FACE_CENTRE
                )
                if box is not None
                else cover_crop(face_img.width, face_img.height, *SPLIT_FACE)
            )
            face_panel = _finish(_crop_resize(face_img, rect, SPLIT_FACE), sharpen=True)
            save("face", face_panel)
            if box is not None:
                files["face_rect"] = ",".join(map(str, placed_face(box, rect, 0, 380, SPLIT_FACE[0])))
            save("fillTop", _fill(face_panel))
        elif layout == "moment_led" and box is not None:
            rect = face_crop(box, face_img.width, face_img.height, 4, 5, CUTOUT_FACE_SHARE, 0.42)
            portrait = _finish(_crop_resize(face_img, rect, (480, 600)), sharpen=True)
            save("cutout", cut_out(portrait))
            files["face_rect"] = ",".join(map(str, placed_face(box, rect, 64, 1180, 480 * 340 / 600)))
    if game_img is not None:
        if layout == "moment_led":
            rect = cover_crop(game_img.width, game_img.height, CANVAS_W, CANVAS_H)
            save("game", _finish(_crop_resize(game_img, rect, (CANVAS_W, CANVAS_H)), sharpen=False))
        elif layout in ("split", "tiktok_native"):
            rect = cover_crop(game_img.width, game_img.height, *SPLIT_GAME)
            game_panel = _finish(_crop_resize(game_img, rect, SPLIT_GAME), sharpen=False)
            save("game", game_panel)
            save("fillBottom", _fill(game_panel))
    return layout, files


# ── Rendering (Playwright) ────────────────────────────────────


class Renderer:
    """One headless Chromium kept warm; `render` is one page per cover."""

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(args=["--disable-gpu", "--font-render-hinting=none"])

    def close(self) -> None:
        self._browser.close()
        self._playwright.stop()

    def render(self, layout: CoverLayout, data: Mapping[str, Any], out_png: Path) -> dict[str, Any]:
        page = self._browser.new_page(viewport={"width": CANVAS_W, "height": CANVAS_H}, device_scale_factor=1)
        try:
            page.goto((TEMPLATES / f"{layout}.html").as_uri())
            result: dict[str, Any] = page.evaluate("data => renderCover(data)", dict(data))
            page.locator("#canvas").screenshot(path=str(out_png), type="png")
            return result
        finally:
            page.close()


_renderer: Renderer | None = None


def renderer() -> Renderer:
    """The process's warm renderer (started on first use)."""
    global _renderer
    if _renderer is None:
        _renderer = Renderer()
    return _renderer


def _save_jpeg(png: Path, jpeg: Path, preview: Path) -> None:
    from PIL import Image

    with Image.open(png) as image:
        rgb = image.convert("RGB")
        quality = JPEG_QUALITY
        rgb.save(jpeg, "JPEG", quality=quality, optimize=True)
        while jpeg.stat().st_size > JPEG_MAX_BYTES and quality > 60:
            quality -= 5
            rgb.save(jpeg, "JPEG", quality=quality, optimize=True)
        rgb.crop(PREVIEW_BOX).save(preview, "JPEG", quality=85, optimize=True)


def compose(
    spec: CoverSpec, work: Path, name: str = "cover", *, render: Renderer | None = None
) -> ComposedCover:
    """Prepare, render and save one cover under `work` (`<name>.jpg` and `<name>_3x4.jpg`)."""
    started = time.monotonic()
    work = work.resolve()
    layout, files = prepare(spec, work / f"{name}_parts")
    fonts = assets_dir() / "fonts"
    mark = assets_dir() / "covers" / "brand" / "vi-mark.svg"
    data: dict[str, Any] = {
        "anton": (fonts / "Anton-Regular.ttf").as_uri(),
        "inter": (fonts / "Inter-Variable.ttf").as_uri() if (fonts / "Inter-Variable.ttf").is_file() else "",
        "face": files.get("face", ""),
        "game": files.get("game", ""),
        "fillTop": files.get("fillTop", ""),
        "fillBottom": files.get("fillBottom", ""),
        "cutout": files.get("cutout", ""),
        "mark": mark.as_uri() if spec.brand_mark and mark.is_file() else "",
        "lines": list(spec.lines) if layout != "tiktok_native" else [],
        "keyWord": spec.key_word,
        "boxText": sentence_case(" ".join(spec.lines)) if layout == "tiktok_native" and spec.lines else "",
        "lineY": spec.line_y if spec.line_y is not None else LINE_Y[layout],
        "clamp": spec.line_y is None,
    }
    png = work / f"{name}.png"
    result = (render or renderer()).render(layout, data, png)
    jpeg, preview = work / f"{name}.jpg", work / f"{name}_3x4.jpg"
    _save_jpeg(png, jpeg, preview)
    png.unlink(missing_ok=True)
    return ComposedCover(
        jpeg=jpeg,
        preview=preview,
        layout=layout,
        font_size=int(result.get("size") or result.get("boxSize") or 0),
        cap_height=int(result.get("capHeight") or 0),
        fits=bool(result.get("fits", True)),
        render_ms=int((time.monotonic() - started) * 1000),
        extras={
            "text_rect": result.get("textRect"),
            "parts_dir": str(work / f"{name}_parts"),
            "has_line": bool(data["lines"] or data["boxText"]),
            "face_rect": [int(v) for v in files["face_rect"].split(",")] if "face_rect" in files else None,
        },
    )

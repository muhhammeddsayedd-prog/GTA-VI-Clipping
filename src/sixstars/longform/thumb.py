"""The episode thumbnail (M20 go-live): an OpenAI image in the GTA 6 Vice City look, our own quiet type.

1. Frames: the peak of the best segments (#1 first), plus the creator's picture on a creator day.
2. The image model repaints them into one clean, minimalist picture (`assets/longform/thumb_style.md` + the
   P14 scene). The model never writes text: the words are rendered by `covers/compose` from the
   `thumb_16x9.html` template, so they always look the same and spell correctly.
3. Fallbacks, in order: a refusal with the creator picture retries once without it; any other failure, the
   daily budget (`longform.thumb.usd_max` per day) or `longform.thumb.ai=false` uses the #1 frame as the
   picture. A render failure keeps the picture without words. A thumbnail is always produced.
"""

import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from sixstars import usage
from sixstars.clients import _http
from sixstars.clients.openai_images import SERVICE, ImageRefusedError, OpenAIImagesClient
from sixstars.covers.compose import Renderer, renderer
from sixstars.factory.assets import assets_dir
from sixstars.factory.tools import grab_frame, probe
from sixstars.logging import get_logger
from sixstars.longform.config import ThumbSpec

log = get_logger(__name__)

W, H = 1280, 720
FRAME_AT = 0.6  # share of a segment where its payoff usually is
INPUT_MAX_SIDE = 1280
TEMPLATE = "thumb_16x9.html"
AVATAR_TIMEOUT = httpx.Timeout(15.0, connect=_http.CONNECT_TIMEOUT_S)


@dataclass(frozen=True)
class ThumbResult:
    path: Path
    source: str  # "ai", "ai_no_avatar" or "frame"
    cost_usd: float


def style_text() -> str:
    path = assets_dir() / "longform" / "thumb_style.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def build_prompt(style: str, fmt: str, scene: str, featured: str | None, with_avatar: bool) -> str:
    """The image prompt. Pure."""
    parts = [style.strip()]
    if scene:
        parts.append(f"The scene: {scene.strip()}")
    if fmt == "creator" and featured and with_avatar:
        parts.append(
            f"This video is about the streamer {featured}. The last attached image is their profile picture: "
            "paint them in the same GTA loading-screen style as a big, expressive reaction close-up on the "
            "right side of the frame, with the game scene behind them. Keep their likeness recognisable and "
            "flattering; no webcam frame."
        )
    else:
        parts.append("Show game characters and action only; no real person's face.")
    return "\n\n".join(parts)


def headline_and_eyebrow(fmt: str, n: int, thumb_text: str, featured: str | None) -> tuple[str, str]:
    """The words on the thumbnail: a 1-3 word headline and a small eyebrow line. Pure."""
    top = f"TOP {n}"
    headline = thumb_text.strip() or top
    if fmt == "creator" and featured:
        return headline, featured.upper()
    eyebrow = "GTA 6 · STREAM MOMENTS" if headline == top else f"{top} · GTA 6"
    return headline, eyebrow


def to_jpeg(data: bytes, quality: int = 88) -> bytes:
    """Re-encode to JPEG at most INPUT_MAX_SIDE on the long side (smaller inputs cost fewer tokens)."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB")
    image.thumbnail((INPUT_MAX_SIDE, INPUT_MAX_SIDE))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


def cover_16x9(data: bytes, out: Path) -> Path:
    """Centre-crop to 16:9 and scale to 1280x720."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as opened:
        image = ImageOps.fit(opened.convert("RGB"), (W, H), Image.Resampling.LANCZOS)
    image.save(out, "JPEG", quality=92)
    return out


def save_capped(png: Path, out: Path, max_bytes: int) -> Path:
    """The final JPEG under YouTube's thumbnail size limit."""
    from PIL import Image

    with Image.open(png) as image:
        rgb = image.convert("RGB")
    quality = 92
    rgb.save(out, "JPEG", quality=quality, optimize=True)
    while out.stat().st_size > max_bytes and quality > 55:
        quality -= 6
        rgb.save(out, "JPEG", quality=quality, optimize=True)
    return out


def frames(segments: Sequence[Path], count: int) -> list[bytes]:
    out: list[bytes] = []
    for seg in segments[: max(count, 1)]:
        try:
            out.append(to_jpeg(grab_frame(seg, max(probe(seg).duration_s * FRAME_AT, 0.0))))
        except Exception as exc:  # noqa: BLE001 - a missing frame only weakens the input
            log.warning("thumb_frame_failed", segment=str(seg), error=str(exc)[:200])
    return out


def fetch_avatar(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        with _http.new_client(AVATAR_TIMEOUT) as http:
            response = _http.send("avatar", "GET avatar", lambda: http.get(url, follow_redirects=True))
        if response.status_code != 200 or not response.content:
            return None
        return to_jpeg(response.content)
    except Exception as exc:  # noqa: BLE001 - no avatar: a creator day thumbnail without it
        log.warning("thumb_avatar_failed", error=str(exc)[:200])
        return None


def picture(
    inputs: list[bytes],
    avatar: bytes | None,
    *,
    fmt: str,
    scene: str,
    featured: str | None,
    spec: ThumbSpec,
    episode_id: str,
    client: OpenAIImagesClient,
) -> tuple[bytes, str, float]:
    """The background picture and where it came from."""
    fallback = inputs[0] if inputs else b""
    if not spec.ai or not inputs:
        return fallback, "frame", 0.0
    if usage.spent_today(SERVICE) >= spec.usd_max:
        log.warning("thumb_budget_used", usd_max=spec.usd_max)
        return fallback, "frame", 0.0
    attempts: list[tuple[list[bytes], bool, str]] = []
    if avatar is not None:
        attempts.append(([*inputs, avatar], True, "ai"))
    attempts.append((inputs, False, "ai" if avatar is None else "ai_no_avatar"))
    style = style_text()
    for images, with_avatar, source in attempts:
        try:
            got = client.edit(
                images,
                build_prompt(style, fmt, scene, featured, with_avatar),
                model=spec.model,
                size=spec.size,
                quality=spec.quality,
                ref_id=episode_id,
            )
            return got.data, source, got.cost_usd
        except ImageRefusedError as exc:
            log.warning("thumb_refused", episode_id=episode_id, with_avatar=with_avatar, error=str(exc)[:200])
            continue
        except Exception as exc:  # noqa: BLE001 - the frame thumbnail is always there
            log.warning("thumb_ai_failed", episode_id=episode_id, error=str(exc)[:300])
            break
    return fallback, "frame", 0.0


def make(
    episode_id: str,
    segments_best_first: Sequence[Path],
    work: Path,
    *,
    fmt: str,
    n: int,
    thumb_text: str,
    scene: str,
    featured: str | None,
    avatar_url: str | None,
    spec: ThumbSpec,
    client: OpenAIImagesClient | None = None,
    render: Renderer | None = None,
) -> ThumbResult:
    """Build `thumb.jpg` (1280x720, under `spec.max_bytes`) in `work`."""
    inputs = frames(segments_best_first, spec.frames)
    avatar = fetch_avatar(avatar_url) if fmt == "creator" else None
    data, source, cost = picture(
        inputs,
        avatar,
        fmt=fmt,
        scene=scene,
        featured=featured,
        spec=spec,
        episode_id=episode_id,
        client=client or OpenAIImagesClient(timeout_s=spec.timeout_s),
    )
    if not data:
        raise RuntimeError("no frame for the thumbnail")
    bg = cover_16x9(data, work / "thumb_bg.jpg")
    headline, eyebrow = headline_and_eyebrow(fmt, n, thumb_text, featured)
    fonts = assets_dir() / "fonts"
    out = work / "thumb.jpg"
    png = work / "thumb.png"
    try:
        (render or renderer()).render_page(
            TEMPLATE,
            (W, H),
            {
                "bg": bg.as_uri(),
                "anton": (fonts / "Anton-Regular.ttf").as_uri(),
                "inter": (fonts / "Inter-Variable.ttf").as_uri(),
                "eyebrow": eyebrow,
                "headline": headline,
            },
            png,
        )
        save_capped(png, out, spec.max_bytes)
    except Exception as exc:  # noqa: BLE001 - the picture alone is still a good thumbnail
        log.warning("thumb_render_failed", episode_id=episode_id, error=str(exc)[:300])
        save_capped(bg, out, spec.max_bytes)
    finally:
        png.unlink(missing_ok=True)
    log.info("thumb_made", episode_id=episode_id, source=source, cost_usd=cost, bytes=out.stat().st_size)
    return ThumbResult(out, source, cost)

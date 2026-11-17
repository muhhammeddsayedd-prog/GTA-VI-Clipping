"""Detects the streamer's facecam region in the source video so the layout can stack it with gameplay.
Implemented in M06.

The result is cached per creator in `creator_layouts` for seven days (the column default). A cached row is
used when it has not expired, was made for the same source resolution and is confident enough; otherwise
one frame is sent to the vision model (prompt P4) and the row is replaced.
"""

from collections.abc import Mapping
from pathlib import Path

from psycopg.types.json import Jsonb

from sixstars import db
from sixstars.ai import calls
from sixstars.ai.schemas import Layout as DetectedLayout
from sixstars.ai.schemas import PixelBox
from sixstars.clients.anthropic_client import AnthropicClient, BudgetKind
from sixstars.factory import tools
from sixstars.factory.layout import Box, Layout, LayoutA, LayoutB
from sixstars.logging import get_logger

# docs/09 section 3: below this confidence the detection is not trusted (no facecam, detect again).
MIN_CONFIDENCE = 0.6
# A gameplay box covering at least this share of the frame in both directions counts as the whole frame.
FULL_FRAME_SHARE = 0.95
# A facecam narrower or lower than this share of the frame is noise (an avatar, an icon), not a webcam.
MIN_FACECAM_SHARE = 0.06


def box_from_json(value: object, source_w: int, source_h: int) -> Box | None:
    """A stored `{"x", "y", "w", "h"}` box clamped to the frame; None when absent or unusable."""
    if not isinstance(value, Mapping):
        return None
    try:
        x, y, w, h = (int(value[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    x, y = max(0, x), max(0, y)
    w, h = min(w, source_w - x), min(h, source_h - y)
    if w < 2 or h < 2:
        return None
    return Box(x, y, w, h)


def _box_json(box: PixelBox | None) -> Jsonb | None:
    return Jsonb({"x": box.x, "y": box.y, "w": box.w, "h": box.h}) if box is not None else None


def choose_layout(
    facecam: Box | None, gameplay: Box | None, confidence: float, source_w: int, source_h: int
) -> Layout:
    """Layout A when a believable facecam was found with enough confidence, else layout B.

    A gameplay box that is nearly the whole frame is dropped, so the gameplay crop is free to slide away
    from a facecam drawn over the game.
    """
    if facecam is None or confidence < MIN_CONFIDENCE:
        return LayoutB()
    if facecam.w < source_w * MIN_FACECAM_SHARE or facecam.h < source_h * MIN_FACECAM_SHARE:
        return LayoutB()
    if gameplay is not None and (
        gameplay.w >= source_w * FULL_FRAME_SHARE and gameplay.h >= source_h * FULL_FRAME_SHARE
    ):
        gameplay = None
    return LayoutA(facecam_box=facecam, gameplay_box=gameplay)


def cached_layout(creator_id: str, source_w: int, source_h: int) -> Layout | None:
    """The cached layout for this creator and resolution, or None when detection must run."""
    row = db.fetch_one(
        """
        select facecam_box, gameplay_box, confidence
        from creator_layouts
        where creator_id = %s and source_width = %s and source_height = %s and expires_at > now()
        """,
        (creator_id, source_w, source_h),
    )
    if row is None or row["confidence"] is None or float(row["confidence"]) < MIN_CONFIDENCE:
        return None
    return choose_layout(
        box_from_json(row["facecam_box"], source_w, source_h),
        box_from_json(row["gameplay_box"], source_w, source_h),
        float(row["confidence"]),
        source_w,
        source_h,
    )


def store_layout(
    creator_id: str, source_w: int, source_h: int, detected: DetectedLayout, detected_from: str
) -> None:
    """Replace the creator's cached layout; the expiry restarts at the column default (seven days)."""
    db.execute(
        """
        insert into creator_layouts
          (creator_id, source_width, source_height, facecam_box, gameplay_box, confidence, detected_from)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (creator_id) do update set
          source_width = excluded.source_width,
          source_height = excluded.source_height,
          facecam_box = excluded.facecam_box,
          gameplay_box = excluded.gameplay_box,
          confidence = excluded.confidence,
          detected_from = excluded.detected_from,
          detected_at = now(),
          expires_at = default
        """,
        (
            creator_id,
            source_w,
            source_h,
            _box_json(detected.facecam),
            _box_json(detected.gameplay),
            detected.confidence,
            detected_from,
        ),
    )


def _to_box(box: PixelBox | None, source_w: int, source_h: int) -> Box | None:
    if box is None:
        return None
    return box_from_json({"x": box.x, "y": box.y, "w": box.w, "h": box.h}, source_w, source_h)


def detect(
    source: Path,
    time_s: float,
    source_w: int,
    source_h: int,
    *,
    lane: BudgetKind = "vod",
    ref_id: str | None = None,
    client: AnthropicClient | None = None,
    ffmpeg: str = "ffmpeg",
) -> DetectedLayout:
    """Grab the frame at `time_s` and ask the vision model (P4) where the facecam and the gameplay are."""
    frame = tools.grab_frame(source, time_s, ffmpeg)
    return calls.detect_layout(frame, source_w, source_h, lane=lane, ref_id=ref_id, client=client)


def layout_from_detection(detected: DetectedLayout, source_w: int, source_h: int) -> Layout:
    """The render layout for a P4 result."""
    return choose_layout(
        _to_box(detected.facecam, source_w, source_h),
        _to_box(detected.gameplay, source_w, source_h),
        detected.confidence,
        source_w,
        source_h,
    )


def get_layout(
    creator_id: str | None,
    source: Path,
    time_s: float,
    source_w: int,
    source_h: int,
    *,
    lane: BudgetKind = "vod",
    ref_id: str | None = None,
    client: AnthropicClient | None = None,
    ffmpeg: str = "ffmpeg",
) -> Layout:
    """The layout for a clip of this creator: from the cache, else detected now and cached.

    Without a `creator_id` nothing is read from or written to the cache.
    """
    log = get_logger("factory")
    if creator_id is not None:
        cached = cached_layout(creator_id, source_w, source_h)
        if cached is not None:
            log.info("layout_cached", creator_id=creator_id, layout=cached.kind)
            return cached
    detected = detect(
        source, time_s, source_w, source_h, lane=lane, ref_id=ref_id, client=client, ffmpeg=ffmpeg
    )
    if creator_id is not None:
        store_layout(creator_id, source_w, source_h, detected, f"{ref_id or source.name}@{time_s:.1f}s")
    layout = layout_from_detection(detected, source_w, source_h)
    log.info(
        "layout_detected",
        creator_id=creator_id,
        layout=layout.kind,
        confidence=detected.confidence,
        facecam=detected.facecam is not None,
    )
    return layout

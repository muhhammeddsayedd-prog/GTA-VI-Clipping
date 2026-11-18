"""Clip thumbnails for the admin console (docs/26, console media).

Right after a clip is rendered, one frame of the finished mp4 is written as a small JPEG, uploaded to the
private `clips` bucket next to the clip (`storage.thumb_key`) and its key saved in `clips.thumb_path`.
This happens before the gate, so gate-rejected clips get a frame too; clips the writer rejected are never
rendered and get none. A thumbnail is a nicety: `try_make_thumb` never raises, it logs and records
`clip.thumb_failed` instead.

The backfill (`sixstars console-media-backfill`) makes the same frame for older clips from the local file
when it still exists, else from a signed URL of the stored mp4 (ffmpeg reads it over https).
"""

import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sixstars import db, events, settings
from sixstars import storage as clip_storage
from sixstars.factory import tools
from sixstars.logging import get_logger

_log = get_logger("factory")


@dataclass(frozen=True, slots=True)
class ThumbParams:
    """The `console.thumb_*` settings."""

    frame_s: float
    width_px: int
    qscale: int

    @classmethod
    def load(cls) -> "ThumbParams":
        return cls(
            frame_s=settings.get_float("console.thumb_frame_s"),
            width_px=settings.get_int("console.thumb_width_px"),
            qscale=settings.get_int("console.thumb_jpeg_qscale"),
        )


_TOKEN = re.compile(r"(token=)[^&\s'\"]+")


def redact(text: str) -> str:
    """`text` without the token of a signed URL (ffmpeg names its input in error messages)."""
    return _TOKEN.sub(r"\1***", text)


def frame_time(duration_s: float | None, wanted_s: float) -> float:
    """When to take the frame: `wanted_s` in, or the middle of a clip shorter than that (unknown
    duration: `wanted_s`)."""
    wanted = max(0.0, wanted_s)
    if duration_s is None or duration_s <= 0 or wanted < duration_s:
        return wanted
    return duration_s / 2


def make_thumb(
    clip_id: str,
    source: str,
    *,
    created_at: datetime | None,
    duration_s: float | None,
    storage: clip_storage.ClipStorage,
    params: ThumbParams,
    ffmpeg: str = "ffmpeg",
) -> str:
    """Grab, upload and record the thumbnail of one clip. `source` is a local path or an https URL.
    Returns the storage key. Raises on any failure. `created_at` None: read from the clip row."""
    if created_at is None:
        row = db.fetch_one("select created_at from clips where id = %s", (clip_id,))
        created_at = row["created_at"] if row is not None else None
    key = clip_storage.thumb_key(clip_id, created_at)
    with tempfile.TemporaryDirectory(prefix="sixstars-thumb-") as scratch:
        jpeg = Path(scratch) / f"{clip_id}.jpg"
        tools.grab_thumb(
            source,
            jpeg,
            time_s=frame_time(duration_s, params.frame_s),
            width=params.width_px,
            qscale=params.qscale,
            ffmpeg=ffmpeg,
        )
        storage.upload(jpeg, key, content_type=clip_storage.THUMB_CONTENT_TYPE)
    db.execute("update clips set thumb_path = %s where id = %s", (key, clip_id))
    return key


@dataclass(frozen=True, slots=True)
class FrameSource:
    """Where the backfill reads a clip from: the local file, else a signed URL of the stored mp4."""

    clip_id: str
    local_path: str | None
    storage_path: str | None

    def local(self) -> str | None:
        if self.local_path and Path(self.local_path).is_file():
            return self.local_path
        return None


def backfill_frames(
    limit: int, *, storage: clip_storage.ClipStorage | None = None, ffmpeg: str = "ffmpeg"
) -> dict[str, int]:
    """Make the missing thumbnails of up to `limit` rendered clips, newest first: clips with a stored
    file or a local file and no `thumb_path`. Idempotent: a clip with a thumbnail is never selected
    again. Returns `{"selected", "made", "failed", "no_file"}`."""
    store = storage or clip_storage.ClipStorage()
    rows = db.fetch_all(
        """
        select id::text as id, local_path, storage_path, created_at, duration_s from clips
        where thumb_path is null and (storage_path is not null or local_path is not null)
        order by created_at desc limit %s
        """,
        (max(0, limit),),
    )
    counts = {"selected": len(rows), "made": 0, "failed": 0, "no_file": 0}
    for row in rows:
        source = FrameSource(row["id"], row["local_path"], row["storage_path"])
        path = source.local()
        if path is None and source.storage_path:
            try:
                path = store.signed_url(source.storage_path)
            except Exception as exc:
                _log.warning("clip_thumb_sign_failed", clip_id=source.clip_id, error=type(exc).__name__)
                counts["failed"] += 1
                continue
        if path is None:
            counts["no_file"] += 1
            continue
        duration = float(row["duration_s"]) if row["duration_s"] is not None else None
        key = try_make_thumb(
            source.clip_id,
            path,
            created_at=row["created_at"],
            duration_s=duration,
            storage=store,
            ffmpeg=ffmpeg,
        )
        counts["made" if key else "failed"] += 1
    return counts


def try_make_thumb(
    clip_id: str,
    source: str,
    *,
    created_at: datetime | None,
    duration_s: float | None,
    storage: clip_storage.ClipStorage | None = None,
    ffmpeg: str = "ffmpeg",
) -> str | None:
    """`make_thumb` that never raises: a failure is logged and recorded as `clip.thumb_failed`, and None
    is returned. Without Storage configured nothing is attempted."""
    store = storage or clip_storage.ClipStorage()
    if not store.is_configured():
        _log.info("clip_thumb_skipped", clip_id=clip_id, reason="storage_not_configured")
        return None
    try:
        return make_thumb(
            clip_id,
            source,
            created_at=created_at,
            duration_s=duration_s,
            storage=store,
            params=ThumbParams.load(),
            ffmpeg=ffmpeg,
        )
    except Exception as exc:
        detail = redact(f"{type(exc).__name__}: {exc}")[:500]
        _log.warning("clip_thumb_failed", clip_id=clip_id, error=type(exc).__name__)
        try:
            events.emit(
                "clip.thumb_failed",
                level="warn",
                entity_type="clip",
                entity_id=clip_id,
                message=detail,
            )
        except Exception as emit_exc:  # the render must go on even when the event cannot be written
            _log.warning("clip_thumb_event_failed", clip_id=clip_id, error=type(emit_exc).__name__)
        return None

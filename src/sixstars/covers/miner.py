"""Stage 1 of the cover engine (docs/29a section 7): mine frames from the clean layers.

`capture` runs in the render job right after the clip is rendered, while the source still exists: it keeps
a frame-exact copy of the clip's span (a hard link to the variants copy when there is one, so the express
lane pays nothing) and a manifest with the crop boxes and the peak, then queues `cover.mine` on the normal
queue. The renderer itself never keeps clean layers: the facecam and gameplay boxes of the source are the
clean layers.

`mine` (the job) extracts 4 frames a second from peak - 2 s to peak + 2 s and 1 a second elsewhere, for the
facecam box (native pixels) and the gameplay region (scaled to at most 1280 px wide), scores them (stage 2,
`scorer`), keeps the top 5 locally for the compositor and in Storage (`covers-work/<clip_id>/`), scores the
rendered file's frames for TikTok's `cover_timestamp`, writes `mine.json` and deletes everything else.
"""

import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sixstars import db, events, jobs
from sixstars import storage as clip_storage
from sixstars.config import env
from sixstars.covers import ocr, scorer
from sixstars.covers.config import CoverConfig
from sixstars.factory import tools
from sixstars.factory.layout import Box
from sixstars.factory.window import ClipWindow
from sixstars.logging import get_logger

JOB_MINE = "cover.mine"
SOURCE_NAME = "source.mkv"
MANIFEST_NAME = "manifest.json"
MINE_NAME = "mine.json"
PEAK_FPS = 4
BASE_FPS = 1
PEAK_HALF_S = 2.0
CHANGE_LAG_S = 0.5
GAME_MAX_W = 1280
TOP_COUNT = 5
COPY_TIMEOUT_S = 180.0
EXTRACT_TIMEOUT_S = 300.0
LANDMARKER_ENV = "SIXSTARS_FACE_LANDMARKER"
LANDMARKER_DEFAULT = "/models/mediapipe/face_landmarker.task"


def cover_dir(clip_id: str) -> Path:
    """Local work folder of a clip's cover: `$DATA_DIR/covers/<clip_id>`."""
    return env().DATA_DIR / "covers" / clip_id


@dataclass(frozen=True, slots=True)
class Manifest:
    """What the mining job needs (times in clip time, boxes in source pixels)."""

    clip_id: str
    offset_s: float  # where the clip starts inside source.mkv
    duration_s: float
    source_w: int
    source_h: int
    facecam: Box | None
    gameplay: Box | None
    peak_s: float | None
    moment_type: str | None
    lane: str
    rendered_path: str | None

    def to_json(self) -> dict[str, Any]:
        def box(value: Box | None) -> list[int] | None:
            return None if value is None else [value.x, value.y, value.w, value.h]

        return {
            "clip_id": self.clip_id,
            "offset_s": self.offset_s,
            "duration_s": self.duration_s,
            "source_w": self.source_w,
            "source_h": self.source_h,
            "facecam": box(self.facecam),
            "gameplay": box(self.gameplay),
            "peak_s": self.peak_s,
            "moment_type": self.moment_type,
            "lane": self.lane,
            "rendered_path": self.rendered_path,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "Manifest":
        def box(raw: Any) -> Box | None:
            return Box(*(int(v) for v in raw)) if isinstance(raw, list) and len(raw) == 4 else None

        peak = value.get("peak_s")
        return cls(
            clip_id=str(value["clip_id"]),
            offset_s=float(value.get("offset_s") or 0.0),
            duration_s=float(value["duration_s"]),
            source_w=int(value["source_w"]),
            source_h=int(value["source_h"]),
            facecam=box(value.get("facecam")),
            gameplay=box(value.get("gameplay")),
            peak_s=None if peak is None else float(peak),
            moment_type=value.get("moment_type"),
            lane=str(value.get("lane") or "hourly"),
            rendered_path=value.get("rendered_path"),
        )


# ── Pure helpers ──────────────────────────────────────────────


def sample_times_ms(duration_s: float, peak_s: float | None) -> list[int]:
    """The frames to score (ms): every 1/4 s from peak - 2 s to peak + 2 s, every second elsewhere.
    Pure."""
    times: set[int] = set()
    step_ms = int(1000 / BASE_FPS)
    t = step_ms // 2
    while t < duration_s * 1000:
        times.add(t)
        t += step_ms
    if peak_s is not None:
        start = max(0.0, peak_s - PEAK_HALF_S)
        end = min(duration_s, peak_s + PEAK_HALF_S)
        t_s = start
        while t_s < end:
            times.add(int(round(t_s * 1000)))
            t_s += 1.0 / PEAK_FPS
    return sorted(t for t in times if 0 <= t < duration_s * 1000)


def frame_index(t_ms: int, fps: int = PEAK_FPS) -> int:
    """The index of the extracted frame (extracted at `fps`) nearest to `t_ms`. Pure."""
    return max(0, round(t_ms * fps / 1000))


# ── Capture (render job) ──────────────────────────────────────


def capture(
    clip_id: str,
    *,
    source: Path,
    window: ClipWindow,
    duration_s: float,
    has_audio: bool,
    source_w: int,
    source_h: int,
    facecam: Box | None,
    gameplay: Box | None,
    peak_s: float | None,
    moment_type: str | None,
    lane: str,
    rendered_path: Path | None,
    peak_source: str = "",
    variants_source: Path | None = None,
    variants_offset_s: float = 0.0,
    ffmpeg: str = "ffmpeg",
) -> bool:
    """Keep the clip's source span and queue `cover.mine` when the cover engine is on. Never raises;
    returns True when the job was queued."""
    log = get_logger("covers")
    try:
        config = CoverConfig.load()
        if not config.enabled:
            return False
        folder = cover_dir(clip_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / SOURCE_NAME
        offset = 0.0
        linked = False
        if variants_source is not None and variants_source.is_file():
            try:
                target.unlink(missing_ok=True)
                os.link(variants_source, target)
                offset, linked = variants_offset_s, True
            except OSError:
                linked = False
        if not linked:
            from sixstars.factory.variants import copy_command

            tools.run(
                copy_command(source, window, target, has_audio=has_audio, ffmpeg=ffmpeg),
                timeout_s=COPY_TIMEOUT_S,
                what="cover source copy",
            )
        manifest = Manifest(
            clip_id=clip_id,
            offset_s=offset,
            duration_s=duration_s,
            source_w=source_w,
            source_h=source_h,
            facecam=facecam,
            gameplay=gameplay,
            peak_s=peak_s,
            moment_type=moment_type,
            lane=lane,
            rendered_path=None if rendered_path is None else str(rendered_path),
        )
        (folder / MANIFEST_NAME).write_text(json.dumps(manifest.to_json()), encoding="utf-8")
        db.execute(
            "update clips set peak_s = %s, peak_source = %s where id = %s",
            (peak_s, peak_source or None, clip_id),
        )
        queued = jobs.enqueue("normal", JOB_MINE, {"clip_id": clip_id}, dedupe_key=f"{JOB_MINE}:{clip_id}")
        log.info("cover_captured", clip_id=clip_id, linked=linked, queued=queued is not None)
        return queued is not None
    except Exception as exc:  # noqa: BLE001 - a cover must never fail a render
        log.warning("cover_capture_failed", clip_id=clip_id, error=f"{type(exc).__name__}: {exc}"[:300])
        return False


# ── Mining (job) ──────────────────────────────────────────────


def _extract(manifest: Manifest, source: Path, out: Path, ffmpeg: str) -> None:
    """Every 1/4 s of the clip: facecam crops (face_00001.jpg, native) and gameplay (game_00001.jpg)."""
    fps = f"fps={PEAK_FPS}"
    graph = [f"[0:v]{fps},split=2[f][g]"]
    face = manifest.facecam
    if face is not None:
        graph.append(f"[f]crop={face.w}:{face.h}:{face.x}:{face.y}[fo]")
    else:
        graph.append("[f]null[fo]")
    game = manifest.gameplay
    crop = f"crop={game.w}:{game.h}:{game.x}:{game.y}," if game is not None else ""
    graph.append(f"[g]{crop}scale='min({GAME_MAX_W},iw)':-2[go]")
    command = [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-ss",
        f"{manifest.offset_s:.3f}",
        "-t",
        f"{manifest.duration_s:.3f}",
    ]
    command += ["-i", str(source), "-filter_complex", ";".join(graph)]
    command += ["-map", "[fo]", "-q:v", "2", str(out / "face_%05d.jpg")]
    command += ["-map", "[go]", "-q:v", "2", str(out / "game_%05d.jpg")]
    tools.run(command, timeout_s=EXTRACT_TIMEOUT_S, what="cover frames")


def _rgb(path: Path) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"))


@dataclass(frozen=True, slots=True)
class MineResult:
    """The outcome of stage 1 + 2 for one clip."""

    clip_id: str
    top: tuple[scorer.FrameScore, ...]
    scored: int
    rejected: Mapping[str, int]
    layout: scorer.Layout | None
    cover_timestamp_ms: int | None
    rendered: Sequence[Mapping[str, Any]]

    def as_json(self, config_version: int) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "config_version": config_version,
            "scored": self.scored,
            "rejected": dict(self.rejected),
            "layout": self.layout,
            "cover_timestamp_ms": self.cover_timestamp_ms,
            "top": [frame.as_json() for frame in self.top],
            "rendered": list(self.rendered),
        }


def _score_rendered(
    manifest: Manifest, scored: Sequence[scorer.FrameScore], ffmpeg: str
) -> tuple[int | None, list[dict[str, Any]]]:
    """Best rendered frame for TikTok's cover_timestamp: clean score of the nearest frame, + a whole
    caption phrase, - a cut word (OCR against the clip's transcript)."""
    if not manifest.rendered_path or not Path(manifest.rendered_path).is_file() or not scored:
        return None, []
    from rapidocr import RapidOCR

    vocabulary = _vocabulary(manifest.clip_id)
    engine = RapidOCR()
    results: list[dict[str, Any]] = []
    best: tuple[float, int] | None = None
    for frame in sorted(scored, key=lambda f: -f.score)[:12]:
        if frame.rejected is not None:
            continue
        rgb_frame = ocr.frame_rgb(Path(manifest.rendered_path), frame.t_ms / 1000, ffmpeg)
        tokens = ocr.caption_tokens(engine, rgb_frame)
        phrase, cut = ocr.caption_verdict(tokens, vocabulary)
        value = scorer.rendered_score(frame.score, phrase=phrase, cut=cut)
        results.append({"t_ms": frame.t_ms, "score": round(value, 4), "text": " ".join(tokens), "cut": cut})
        if best is None or value > best[0]:
            best = (value, frame.t_ms)
    return (None if best is None else best[1]), results


def _vocabulary(clip_id: str) -> list[str]:
    from sixstars import db
    from sixstars.gate.store import transcript_words

    row = db.fetch_one("select transcript from clips where id = %s", (clip_id,))
    words = transcript_words(row["transcript"]) if row is not None else []
    return sorted({ocr.normalise(w) for w, _s, _e in words if ocr.normalise(w)})


def mine(
    clip_id: str, *, storage: clip_storage.ClipStorage | None = None, ffmpeg: str = "ffmpeg"
) -> MineResult:
    """Stages 1 and 2 for one captured clip. Raises when the capture is missing or the tools fail."""
    log = get_logger("covers")
    folder = cover_dir(clip_id)
    manifest = Manifest.from_json(json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8")))
    config = CoverConfig.load()
    model = os.environ.get(LANDMARKER_ENV, LANDMARKER_DEFAULT)
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        work = Path(tmp)
        _extract(manifest, folder / SOURCE_NAME, work, ffmpeg)
        times = sample_times_ms(manifest.duration_s, manifest.peak_s)
        landmarker = scorer.Landmarker(model)
        items: list[tuple[int, scorer.FaceFeatures, scorer.GameFeatures]] = []
        try:
            for t_ms in times:
                n = frame_index(t_ms) + 1  # ffmpeg numbers from 1
                face_path, game_path = work / f"face_{n:05d}.jpg", work / f"game_{n:05d}.jpg"
                if not face_path.is_file() or not game_path.is_file():
                    continue
                prev_path = work / f"game_{max(1, n - round(CHANGE_LAG_S * PEAK_FPS)):05d}.jpg"
                face = (
                    landmarker.face(_rgb(face_path))
                    if manifest.facecam is not None
                    else scorer.FaceFeatures(False)
                )
                game = scorer.game_features(
                    _rgb(game_path), _rgb(prev_path) if prev_path != game_path else None
                )
                items.append((t_ms, face, game))
        finally:
            landmarker.close()
        scored = scorer.score_frames(items, config.weights)
        top = scorer.top_frames(scored, TOP_COUNT)
        keep = folder / "top"
        shutil.rmtree(keep, ignore_errors=True)
        keep.mkdir(parents=True)
        for rank, frame in enumerate(top, start=1):
            n = frame_index(frame.t_ms) + 1
            for layer in ("face", "game"):
                shutil.copyfile(work / f"{layer}_{n:05d}.jpg", keep / f"{rank}_{layer}_{frame.t_ms}.jpg")
    rejected: dict[str, int] = {}
    for frame in scored:
        if frame.rejected:
            rejected[frame.rejected] = rejected.get(frame.rejected, 0) + 1
    layout = scorer.propose_layout(top[0].gameplay, top[0].face_area, manifest.moment_type) if top else None
    timestamp, rendered = _score_rendered(manifest, scored, ffmpeg)
    result = MineResult(clip_id, tuple(top), len(scored), rejected, layout, timestamp, rendered)
    (folder / MINE_NAME).write_text(json.dumps(result.as_json(config.version)), encoding="utf-8")
    store = storage or clip_storage.ClipStorage()
    if store.is_configured():
        for path in sorted(keep.iterdir()):
            store.upload(path, clip_storage.cover_work_key(clip_id, path.name), content_type="image/jpeg")
        store.upload(
            folder / MINE_NAME,
            clip_storage.cover_work_key(clip_id, MINE_NAME),
            content_type="application/json",
        )
    (folder / SOURCE_NAME).unlink(missing_ok=True)
    events.emit(
        "cover.mined",
        entity_type="clip",
        entity_id=clip_id,
        data={"top": len(top), "scored": len(scored), "layout": layout, "cover_timestamp_ms": timestamp},
    )
    log.info(
        "cover_mined", clip_id=clip_id, top=len(top), scored=len(scored), rejected=rejected, layout=layout
    )
    return result


def handle_mine(job: jobs.Job) -> None:
    """`cover.mine` job: mine one clip (no-op when the engine was switched off meanwhile)."""
    clip_id = str(job.payload["clip_id"])
    if not CoverConfig.load().enabled:
        get_logger("covers").info("cover_mine_skipped", clip_id=clip_id, reason="disabled")
        return
    mine(clip_id)


def register_handlers() -> None:
    """Register `cover.mine` with the worker (safe to call more than once)."""
    from sixstars.services import worker

    if worker.HANDLERS.get(JOB_MINE) is not handle_mine:
        worker.register(JOB_MINE, handle_mine)

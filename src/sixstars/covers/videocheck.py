"""C1 acceptance check (docs/29a section 9): are the frames a platform is likely to pick cover-ready?

For each recent clip rendered with `cover.video_ready`, the rendered file is read at the opening (0.5, 1.0,
1.5 s) and around the peak (peak - 0.5, peak, peak + 0.5 s):

- face: MediaPipe finds the streamer's face in the facecam panel and measures its height against the
  panel's (pass: at least `min_face_share`, 0.35, in most frames of each window);
- caption: RapidOCR reads the caption band; every word it reads must be a whole word of the clip's
  transcript (a prefix or suffix of a word is a cut word) and at least one frame per window shows two or
  more words (pass: no cut word and a phrase in each window);
- length: the file is the clip's length (`clips.duration_s`, plus the end card when one is drawn).

`sixstars covers check-video` prints one row per clip and a summary, and keeps the checked frames as JPEGs
under `$DATA_DIR/covers-check/<clip_id>/` for a contact sheet. Writes nothing to the database.
"""

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sixstars import db, settings
from sixstars.config import env
from sixstars.factory import coverready, tools
from sixstars.factory.layout import REF_HEIGHT, REF_WIDTH
from sixstars.gate.store import transcript_words
from sixstars.logging import get_logger

MIN_FACE_SHARE = 0.35
OPEN_TIMES = (0.5, 1.0, 1.5)
PEAK_OFFSETS = (-0.5, 0.0, 0.5)
# Caption band on the reference canvas (layout C captions sit around y=1400) and the smallest text height
# that counts as a caption (the credit line and the watermark are smaller).
CAPTION_TOP, CAPTION_BOTTOM = 1150, 1720
MIN_CAPTION_TEXT_H = 45
FUZZY_MATCH = 0.8
LENGTH_TOLERANCE_S = 0.15

TokenStatus = Literal["ok", "cut", "unknown", "masked"]

_WORD = re.compile(r"[^a-z0-9*]+")
# Digits OCR reads for letters in heavy caption fonts.
_OCR_SLIPS = str.maketrans({"0": "o", "1": "l", "5": "s"})


def normalise(text: str) -> str:
    """Lower case, letters, digits and the mask star only. Pure."""
    return _WORD.sub("", text.lower())


def token_status(token: str, vocabulary: Sequence[str]) -> TokenStatus:
    """What an OCR'd caption word is: a word of the transcript (`ok`, fuzzy for OCR slips), a piece of one
    (`cut`), a masked profanity (`masked`) or something else (`unknown`: noise, `X2`, an emoji). Pure."""
    word = normalise(token)
    if not word:
        return "unknown"
    if "*" in word:
        return "masked"
    if word in vocabulary or word.translate(_OCR_SLIPS) in vocabulary:
        return "ok"
    if any(difflib.SequenceMatcher(None, word, known).ratio() >= FUZZY_MATCH for known in vocabulary):
        return "ok"
    if len(word) >= 2 and any(
        len(known) >= len(word) + 2 and (known.startswith(word) or known.endswith(word))
        for known in vocabulary
    ):
        return "cut"
    return "unknown"


@dataclass(frozen=True, slots=True)
class FrameCheck:
    """One checked frame."""

    window: Literal["open", "peak"]
    t_s: float
    face_share: float | None
    tokens: tuple[str, ...]
    statuses: tuple[TokenStatus, ...]

    @property
    def cut(self) -> bool:
        return "cut" in self.statuses

    @property
    def phrase(self) -> bool:
        return sum(1 for s in self.statuses if s in ("ok", "masked")) >= 2


@dataclass(frozen=True, slots=True)
class ClipCheck:
    """The verdict for one clip."""

    clip_id: str
    frames: tuple[FrameCheck, ...]
    length_ok: bool
    duration_s: float | None
    expected_s: float | None
    errors: tuple[str, ...] = field(default=())

    def _window(self, name: str) -> list[FrameCheck]:
        return [frame for frame in self.frames if frame.window == name]

    def face_ok(self, min_share: float = MIN_FACE_SHARE) -> bool:
        """Most frames of every checked window show the face at `min_share` of the panel or more."""
        for name in ("open", "peak"):
            frames = self._window(name)
            if not frames:
                continue
            good = sum(1 for f in frames if f.face_share is not None and f.face_share >= min_share)
            if good * 2 <= len(frames):
                return False
        return bool(self.frames)

    def caption_ok(self) -> bool:
        """No cut word anywhere and a phrase in every checked window."""
        if any(frame.cut for frame in self.frames):
            return False
        return all(
            any(frame.phrase for frame in self._window(name))
            for name in ("open", "peak")
            if self._window(name)
        )

    def passed(self) -> bool:
        return not self.errors and self.length_ok and self.face_ok() and self.caption_ok()


def check_times(duration_s: float, peak_s: float | None) -> list[tuple[Literal["open", "peak"], float]]:
    """The frames to read: the opening and around the peak, inside the clip. Pure."""
    times: list[tuple[Literal["open", "peak"], float]] = [("open", t) for t in OPEN_TIMES if t < duration_s]
    if peak_s is not None:
        times += [("peak", round(peak_s + d, 2)) for d in PEAK_OFFSETS if 0 <= peak_s + d < duration_s - 0.05]
    return times


# ── IO ────────────────────────────────────────────────────────


def _frame(path: Path, t_s: float, ffmpeg: str) -> tuple[int, int, bytes]:
    command = [ffmpeg, "-v", "error", "-ss", f"{t_s:.3f}", "-i", str(path), "-frames:v", "1"]
    command += ["-vf", f"scale={REF_WIDTH}:{REF_HEIGHT}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    result = tools.run(command, timeout_s=30, what="cover check frame")
    return REF_WIDTH, REF_HEIGHT, result.stdout


def _crop_rows(frame: tuple[int, int, bytes], top: int, bottom: int) -> tuple[int, int, bytes]:
    width, _height, data = frame
    return width, bottom - top, data[top * width * 3 : bottom * width * 3]


def _ocr_lines(engine: Any, frame: tuple[int, int, bytes]) -> list[str]:
    """Caption-sized text lines OCR'd from the caption band."""
    import numpy as np

    width, height, data = _crop_rows(frame, CAPTION_TOP, CAPTION_BOTTOM)
    image = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)[:, :, ::-1]  # RGB -> BGR
    result = engine(np.ascontiguousarray(image))
    texts = getattr(result, "txts", None) or ()
    boxes = getattr(result, "boxes", None)
    lines: list[str] = []
    for n, text in enumerate(texts):
        if boxes is not None and n < len(boxes):
            ys = [float(point[1]) for point in boxes[n]]
            if max(ys) - min(ys) < MIN_CAPTION_TEXT_H:
                continue
        lines.append(str(text))
    return lines


def _save_jpeg(frame: tuple[int, int, bytes], path: Path) -> None:
    from PIL import Image

    width, height, data = frame
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.frombytes("RGB", (width, height), data).save(path, "JPEG", quality=80)


def check_clip(row: Mapping[str, Any], engine: Any, *, out_dir: Path, ffmpeg: str = "ffmpeg") -> ClipCheck:
    """Check one rendered clip (`row`: id, local_path, duration_s, peak_s, facecam_h, transcript)."""
    clip_id = str(row["id"])
    path = Path(str(row.get("local_path") or ""))
    if not path.is_file():
        return ClipCheck(clip_id, (), False, None, None, ("file missing",))
    media = tools.probe(path)
    expected = float(row["duration_s"]) if row.get("duration_s") is not None else None
    end_card = settings.get_float("factory.end_card_s", 0.0)
    length_ok = expected is not None and any(
        abs(media.duration_s - expected - extra) <= LENGTH_TOLERANCE_S for extra in {0.0, end_card}
    )
    vocabulary = sorted(
        {normalise(w) for w, _s, _e in transcript_words(row.get("transcript")) if normalise(w)}
    )
    facecam_h = int(row.get("facecam_h") or 672)
    peak = float(row["peak_s"]) if row.get("peak_s") is not None else None
    frames: list[FrameCheck] = []
    raw = []
    for window, t in check_times(expected or media.duration_s, peak):
        frame = _frame(path, t, ffmpeg)
        raw.append((window, t, frame))
        _save_jpeg(frame, out_dir / clip_id / f"{window}_{t:05.2f}.jpg")
    try:
        faces = coverready.detect_faces([_crop_rows(frame, 0, facecam_h) for _w, _t, frame in raw])
    except coverready.FaceDetectorUnavailableError as exc:
        return ClipCheck(clip_id, (), length_ok, media.duration_s, expected, (str(exc),))
    for (window, t, frame), face in zip(raw, faces, strict=True):
        tokens = tuple(token for line in _ocr_lines(engine, frame) for token in line.split())
        frames.append(
            FrameCheck(
                window=window,
                t_s=t,
                face_share=None if face is None else round(coverready.face_share(face.h, facecam_h), 3),
                tokens=tokens,
                statuses=tuple(token_status(token, vocabulary) for token in tokens),
            )
        )
    return ClipCheck(clip_id, tuple(frames), length_ok, media.duration_s, expected)


_RECENT_SQL = """
select c.id::text as id, c.local_path, c.duration_s, c.peak_s, c.peak_source, c.facecam_h, c.transcript,
       c.layout_kind, c.created_at
from clips c
where c.facecam_h is not null and c.layout_kind = 'C' and c.local_path is not null
  and (%(since)s::timestamptz is null or c.created_at >= %(since)s::timestamptz)
order by c.created_at desc
limit %(limit)s
"""


def recent(limit: int, since: str | None) -> list[dict[str, Any]]:
    """The newest cover-ready layout C clips."""
    return [dict(row) for row in db.fetch_all(_RECENT_SQL, {"limit": limit, "since": since})]


def run(limit: int = 20, since: str | None = None) -> list[ClipCheck]:
    """Check the newest cover-ready clips; frames are kept under `$DATA_DIR/covers-check/`."""
    from rapidocr import RapidOCR

    engine = RapidOCR()
    out_dir = env().DATA_DIR / "covers-check"
    checks: list[ClipCheck] = []
    for row in recent(limit, since):
        try:
            checks.append(check_clip(row, engine, out_dir=out_dir))
        except tools.FfmpegError as exc:
            get_logger("covers").warning("cover_check_failed", clip_id=row["id"], error=str(exc)[:200])
            checks.append(ClipCheck(str(row["id"]), (), False, None, None, (str(exc)[:200],)))
    return checks


def summary_rows(checks: Sequence[ClipCheck]) -> list[dict[str, Any]]:
    """One printable row per clip. Pure."""
    rows = []
    for check in checks:
        shares = [f.face_share for f in check.frames if f.face_share is not None]
        rows.append(
            {
                "clip_id": check.clip_id,
                "passed": check.passed(),
                "face_ok": check.face_ok(),
                "caption_ok": check.caption_ok(),
                "length_ok": check.length_ok,
                "min_face_share": min(shares) if shares else None,
                "cut_words": sorted(
                    {t for f in check.frames for t, s in zip(f.tokens, f.statuses, strict=True) if s == "cut"}
                ),
                "phrases": [" ".join(f.tokens) for f in check.frames if f.phrase],
                "errors": list(check.errors),
            }
        )
    return rows

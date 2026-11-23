"""Face detection for Render v2 (D119, development-plan/M19 R2): where the streamer's face is inside the
facecam box, so the cam panel can be framed around it, and the frames that are checked.

The detector is OpenCV's YuNet (`cv2.FaceDetectorYN`, opencv-python-headless 4.14, model
`assets/models/face_detection_yunet_2023mar.onnx` from opencv_zoo, MIT licence in
`assets/models/LICENSE-yunet.txt`; docs/14). It runs on the CPU, needs no network and takes tens of
milliseconds per frame. `FakeFaceDetector` stands in for it in tests.

`FaceDetectRules` is the setting `facecam.detect`; R2 uses `check_frames` and `detector_score`, R3 the rest.
"""

import functools
import statistics
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from sixstars.factory import tools
from sixstars.factory.assets import assets_dir
from sixstars.factory.layout import Box
from sixstars.factory.window import ClipWindow

YUNET_MODEL = "models/face_detection_yunet_2023mar.onnx"
# YuNet finds faces down to about 10 px but does better when the short side of the image is at least this
# long: smaller crops (a 248x246 facecam box) are enlarged before detection.
DETECT_MIN_SIDE = 320
NMS_THRESHOLD = 0.3
TOP_K = 50
FRAME_TIMEOUT_S = 30.0

Image = np.ndarray[Any, np.dtype[np.uint8]]


@dataclass(frozen=True, slots=True)
class Face:
    """One detected face: its box (pixels of the image it was found in) and the detector's score."""

    box: Box
    score: float


class FaceDetector(Protocol):
    def detect(self, image: Image) -> list[Face]:
        """Faces in a BGR image (H x W x 3, uint8), largest first."""
        ...


@dataclass(frozen=True, slots=True)
class FaceDetectRules:
    """The setting `facecam.detect` (docs/16)."""

    min_confidence: float = 0.75
    check_frames: int = 5
    min_face_hits: int = 3
    min_face_share: float = 0.25
    detector_score: float = 0.6
    cache_scope: str = "source"
    creator_prior_days: float = 7.0

    def __post_init__(self) -> None:
        if not 1 <= self.check_frames <= 30 or not 1 <= self.min_face_hits <= self.check_frames:
            raise ValueError("need 1 <= min_face_hits <= check_frames <= 30")
        if not 0 < self.detector_score < 1 or not 0 <= self.min_confidence <= 1:
            raise ValueError("scores must be within 0..1")
        if not 0 < self.min_face_share < 1 or self.creator_prior_days < 0:
            raise ValueError("min_face_share must be within 0..1 and creator_prior_days not negative")
        if self.cache_scope not in ("source", "creator"):
            raise ValueError("cache_scope must be source or creator")

    @classmethod
    def from_setting(cls, raw: object) -> "FaceDetectRules":
        """Missing keys keep their default; a missing or invalid setting gives the defaults. Pure."""
        if not isinstance(raw, Mapping):
            return cls()
        base = cls()
        values: dict[str, object] = {}
        for name in cls.__dataclass_fields__:
            fallback = getattr(base, name)
            value = raw.get(name, fallback)
            if isinstance(fallback, str):
                values[name] = value if isinstance(value, str) else fallback
            elif isinstance(fallback, int):
                ok = (
                    isinstance(value, int | float)
                    and not isinstance(value, bool)
                    and float(value).is_integer()
                )
                values[name] = int(value) if ok else fallback
            else:
                ok = isinstance(value, int | float) and not isinstance(value, bool)
                values[name] = float(value) if ok else fallback
        try:
            return cls(**values)  # type: ignore[arg-type]
        except ValueError:
            return base


class YuNetDetector:
    """`cv2.FaceDetectorYN` with the bundled 2023mar model. One instance per process (`default_detector`);
    calls are serialised because the OpenCV object keeps the input size between calls."""

    def __init__(self, model: Path, score_threshold: float) -> None:
        import cv2  # imported here: a service that never detects faces does not load OpenCV

        if not model.is_file():
            raise FileNotFoundError(f"face model missing: {model}")
        self._cv2 = cv2
        self._net = cv2.FaceDetectorYN.create(
            str(model), "", (DETECT_MIN_SIDE, DETECT_MIN_SIDE), score_threshold, NMS_THRESHOLD, TOP_K
        )
        self._lock = threading.Lock()

    def detect(self, image: Image) -> list[Face]:
        height, width = image.shape[:2]
        if height < 2 or width < 2:
            return []
        scale = max(1.0, DETECT_MIN_SIDE / min(height, width))
        work: Any = image
        if scale > 1.0:
            size = (round(width * scale), round(height * scale))
            work = self._cv2.resize(image, size, interpolation=self._cv2.INTER_LINEAR)
        with self._lock:
            self._net.setInputSize((work.shape[1], work.shape[0]))
            _, rows = self._net.detect(work)
        found: list[Face] = []
        for row in [] if rows is None else rows:
            x, y, w, h = (float(v) / scale for v in row[:4])
            box = clip_box(x, y, w, h, width, height)
            if box is not None:
                found.append(Face(box, float(row[14])))
        return sorted(found, key=lambda face: face.box.w * face.box.h, reverse=True)


@functools.cache
def default_detector(score_threshold: float) -> YuNetDetector:
    """The process-wide YuNet detector for a score threshold."""
    return YuNetDetector(assets_dir() / YUNET_MODEL, score_threshold)


@dataclass
class FakeFaceDetector:
    """Test double: returns `faces` for every image, or `by_call[n]` for the n-th call, and records the
    sizes of the images it was given."""

    faces: Sequence[Face] = ()
    by_call: Sequence[Sequence[Face]] | None = None
    seen: list[tuple[int, int]] = field(default_factory=list)

    def detect(self, image: Image) -> list[Face]:
        self.seen.append((int(image.shape[1]), int(image.shape[0])))
        if self.by_call is not None:
            index = len(self.seen) - 1
            return list(self.by_call[index]) if index < len(self.by_call) else []
        return list(self.faces)


def clip_box(x: float, y: float, w: float, h: float, width: int, height: int) -> Box | None:
    """A float box clipped to a width x height image as an integer `Box`; None when nothing is left. Pure."""
    x0, y0 = max(0, int(round(x))), max(0, int(round(y)))
    x1, y1 = min(width, int(round(x + w))), min(height, int(round(y + h)))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return Box(x0, y0, x1 - x0, y1 - y0)


def sample_times(window: ClipWindow, count: int) -> list[float]:
    """`count` file times spread over the window (the middle of each of `count` equal parts). Pure."""
    step = window.duration_s / count
    return [window.start_s + step * (i + 0.5) for i in range(count)]


def median_face(per_frame: Sequence[Sequence[Face]], min_score: float) -> tuple[Box | None, int]:
    """The median box of the largest face with a score of at least `min_score` in each frame, and the
    number of frames that had one. Pure."""
    best = [next((face.box for face in faces if face.score >= min_score), None) for faces in per_frame]
    boxes = [box for box in best if box is not None]
    if not boxes:
        return None, 0
    x, y = statistics.median_low(b.x for b in boxes), statistics.median_low(b.y for b in boxes)
    w, h = statistics.median_low(b.w for b in boxes), statistics.median_low(b.h for b in boxes)
    return Box(x, y, w, h), len(boxes)


def frame_command(source: Path, time_s: float, crop: Box, *, ffmpeg: str) -> list[str]:
    """ffmpeg arguments that write one frame at `time_s`, cropped to `crop`, as raw BGR to stdout. Pure."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{time_s:.3f}", "-i", str(source),
        "-frames:v", "1", "-vf", f"crop={crop.w}:{crop.h}:{crop.x}:{crop.y}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]  # fmt: skip


def grab_frames(source: Path, times_s: Sequence[float], crop: Box, *, ffmpeg: str = "ffmpeg") -> list[Image]:
    """The `crop` of the source at each time as BGR images (a frame ffmpeg cannot read is left out)."""
    frames: list[Image] = []
    size = crop.w * crop.h * 3
    for time_s in times_s:
        try:
            result = tools.run(
                frame_command(source, time_s, crop, ffmpeg=ffmpeg),
                timeout_s=FRAME_TIMEOUT_S,
                what="face frame",
            )
        except tools.FfmpegError:
            continue
        if len(result.stdout) >= size:
            frames.append(np.frombuffer(result.stdout[:size], dtype=np.uint8).reshape(crop.h, crop.w, 3))
    return frames


@dataclass(frozen=True, slots=True)
class CamFaceCheck:
    """The face check of one clip's facecam: the median face box in source pixels (None: no face), how
    many frames had a face out of how many were read, and how long it took."""

    face_box: Box | None
    hits: int
    frames: int
    elapsed_ms: int

    def as_json(self) -> dict[str, object]:
        face = self.face_box
        return {
            "face_box": None if face is None else {"x": face.x, "y": face.y, "w": face.w, "h": face.h},
            "hits": self.hits,
            "frames": self.frames,
            "ms": self.elapsed_ms,
        }


def find_cam_face(
    source: Path,
    window: ClipWindow,
    cam_box: Box,
    rules: FaceDetectRules,
    *,
    detector: FaceDetector | None = None,
    ffmpeg: str = "ffmpeg",
    grab: Callable[..., list[Image]] | None = None,
) -> CamFaceCheck:
    """Look for the streamer's face inside `cam_box` in `rules.check_frames` frames spread over the clip
    window; the median face box (source pixels) of the frames that had one."""
    started = time.monotonic()
    detector = detector or default_detector(rules.detector_score)
    grab = grab or grab_frames
    frames = grab(source, sample_times(window, rules.check_frames), cam_box, ffmpeg=ffmpeg)
    face, hits = median_face([detector.detect(frame) for frame in frames], rules.detector_score)
    if face is not None:
        face = Box(cam_box.x + face.x, cam_box.y + face.y, face.w, face.h)
    return CamFaceCheck(face, hits, len(frames), int((time.monotonic() - started) * 1000))

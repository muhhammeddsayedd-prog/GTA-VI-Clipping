"""Stage 2 of the cover engine (docs/29a section 7): score the mined frames.

Per facecam frame (MediaPipe Face Landmarker with blendshapes): whether a face is found, its size (the face
box height as a share of the facecam frame's height, the same measure as C1's `face_target`), eyes open,
expression (the strongest of jaw open, eyes wide, brows up and smile, plus a bonus when two are high),
sharpness (Laplacian variance of the face box, ranked within the clip) and exposure (face box luma and
clipped pixels). Per gameplay frame: detail (edge density), brightness and change (difference from the
frame 0.5 s earlier), each ranked within the clip.

`frame_score` combines them with `cover.weights`. Hard rejects: no face, eyes closed, a face cut by the
frame edge, motion blur (sharpness in the clip's bottom 20%). The pure functions take plain numbers; the
`*_features` functions do the image work with OpenCV and MediaPipe (imported lazily).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

# docs/29a section 7, stage 2 (structural constants of the scorer, not tuning settings).
EYES_OPEN_MIN = 0.35
SHARPNESS_REJECT_PCT = 0.20
EXPRESSION_HIGH = 0.4
EXPRESSION_BONUS = 0.15
EXPOSURE_GOOD = (90.0, 190.0)
EXPOSURE_ZERO = (30.0, 250.0)
CLIPPED_LIMIT = 0.10
GAMEPLAY_DARK = 60.0
GAMEPLAY_BLACK = 20.0
FACE_AREA_TARGET = 0.40
EDGE_MARGIN_PX = 2
# Layout proposal (docs/29a section 7): face-led for weak gameplay or a talking moment, moment-led for
# strong gameplay with a small face.
FACE_LED_GAMEPLAY = 0.3
MOMENT_LED_GAMEPLAY = 0.7
MOMENT_LED_FACE_AREA = 0.25
# The clip writer's moment types that are about what was said rather than shown.
FACE_LED_MOMENTS = frozenset({"emotional"})
# Rendered frames (TikTok's cover_timestamp, YouTube's auto-pick).
PHRASE_BONUS = 0.3
CUT_PENALTY = 0.5

Layout = Literal["split", "face_led", "moment_led"]


@dataclass(frozen=True, slots=True)
class FaceFeatures:
    """What the landmarker and the pixels say about one facecam frame (box in frame pixels)."""

    found: bool
    area: float = 0.0  # face box height / frame height
    eyes_open: float = 0.0
    expression: float = 0.0
    sharpness: float = 0.0  # raw Laplacian variance
    luma: float = 0.0
    clipped: float = 0.0
    edge_cut: bool = False
    box: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class GameFeatures:
    """One gameplay frame: edge density (0-1), mean luma (0-255), change from 0.5 s earlier (0-1)."""

    detail: float
    luma: float
    change: float


@dataclass(frozen=True, slots=True)
class FrameScore:
    """A scored frame pair (clip time in ms)."""

    t_ms: int
    face: FaceFeatures
    game: GameFeatures
    expression: float = 0.0
    face_area: float = 0.0
    sharpness: float = 0.0  # percentile within the clip
    exposure: float = 0.0
    gameplay: float = 0.0
    score: float = 0.0
    rejected: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "t_ms": self.t_ms,
            "score": round(self.score, 4),
            "expression": round(self.expression, 3),
            "face_area": round(self.face_area, 3),
            "sharpness": round(self.sharpness, 3),
            "exposure": round(self.exposure, 3),
            "gameplay": round(self.gameplay, 3),
            "eyes_open": round(self.face.eyes_open, 3),
            "face_box": list(self.face.box) if self.face.box else None,
            "rejected": self.rejected,
        }


# ── Pure maths ────────────────────────────────────────────────


def _avg(values: Mapping[str, float], *names: str) -> float:
    return sum(values.get(name, 0.0) for name in names) / len(names)


def expression_score(blendshapes: Mapping[str, float]) -> float:
    """The strongest of jaw open, eyes wide, brows up and smile; +0.15 when two or more are high. Pure."""
    parts = [
        blendshapes.get("jawOpen", 0.0),
        _avg(blendshapes, "eyeWideLeft", "eyeWideRight"),
        blendshapes.get("browInnerUp", 0.0),
        _avg(blendshapes, "mouthSmileLeft", "mouthSmileRight"),
    ]
    bonus = EXPRESSION_BONUS if sum(1 for p in parts if p >= EXPRESSION_HIGH) >= 2 else 0.0
    return min(1.0, max(parts) + bonus)


def eyes_open_score(blendshapes: Mapping[str, float]) -> float:
    """1 minus the mean eye blink. Pure."""
    return max(0.0, 1.0 - _avg(blendshapes, "eyeBlinkLeft", "eyeBlinkRight"))


def exposure_score(luma: float, clipped: float) -> float:
    """1 inside 90-190 mean luma, falling to 0 at 30 and 250, scaled down by the share of clipped pixels
    (0 at 10%). Pure."""
    low_good, high_good = EXPOSURE_GOOD
    low_zero, high_zero = EXPOSURE_ZERO
    if low_good <= luma <= high_good:
        base = 1.0
    elif luma < low_good:
        base = max(0.0, (luma - low_zero) / (low_good - low_zero))
    else:
        base = max(0.0, (high_zero - luma) / (high_zero - high_good))
    return base * max(0.0, 1.0 - clipped / CLIPPED_LIMIT)


def percentile_ranks(values: Sequence[float]) -> list[float]:
    """Each value's rank within the list as 0..1 (ties share the lower rank; one value is 1.0). Pure."""
    n = len(values)
    if n <= 1:
        return [1.0] * n
    ordered = sorted(values)
    return [ordered.index(v) / (n - 1) for v in values]


def brightness_factor(luma: float) -> float:
    """1 at or above luma 60, 0 at 20 and below. Pure."""
    if luma >= GAMEPLAY_DARK:
        return 1.0
    return max(0.0, (luma - GAMEPLAY_BLACK) / (GAMEPLAY_DARK - GAMEPLAY_BLACK))


def score_frames(
    items: Sequence[tuple[int, FaceFeatures, GameFeatures]], weights: Mapping[str, float]
) -> list[FrameScore]:
    """Score every frame of a clip (ranks are within the clip) and mark the hard rejects. Pure."""
    sharp = percentile_ranks([face.sharpness for _t, face, _g in items])
    detail = percentile_ranks([game.detail for _t, _f, game in items])
    change = percentile_ranks([game.change for _t, _f, game in items])
    scored: list[FrameScore] = []
    for n, (t_ms, face, game) in enumerate(items):
        gameplay = brightness_factor(game.luma) * (0.5 * detail[n] + 0.5 * change[n])
        area = min(face.area / FACE_AREA_TARGET, 1.0) if face.found else 0.0
        exposure = exposure_score(face.luma, face.clipped) if face.found else 0.0
        expression = face.expression if face.found else 0.0
        total = (
            weights.get("expression", 0.0) * expression
            + weights.get("face_area", 0.0) * area
            + weights.get("sharpness", 0.0) * sharp[n]
            + weights.get("exposure", 0.0) * exposure
            + weights.get("gameplay", 0.0) * gameplay
        )
        rejected = None
        if not face.found:
            rejected = "no_face"
        elif face.eyes_open < EYES_OPEN_MIN:
            rejected = "eyes_closed"
        elif face.edge_cut:
            rejected = "face_cut"
        elif len(items) >= 5 and sharp[n] < SHARPNESS_REJECT_PCT:
            rejected = "blur"
        scored.append(
            FrameScore(
                t_ms=t_ms,
                face=face,
                game=game,
                expression=expression,
                face_area=face.area if face.found else 0.0,
                sharpness=sharp[n],
                exposure=exposure,
                gameplay=gameplay,
                score=total,
                rejected=rejected,
            )
        )
    return scored


def top_frames(scored: Sequence[FrameScore], count: int = 5, min_gap_ms: int = 500) -> list[FrameScore]:
    """The best `count` frames that are not rejected, at least `min_gap_ms` apart. Pure."""
    chosen: list[FrameScore] = []
    for frame in sorted(scored, key=lambda f: (-f.score, f.t_ms)):
        if frame.rejected is not None:
            continue
        if all(abs(frame.t_ms - other.t_ms) >= min_gap_ms for other in chosen):
            chosen.append(frame)
        if len(chosen) == count:
            break
    return chosen


def propose_layout(gameplay: float, face_area: float, moment_type: str | None) -> Layout:
    """The layout family the frame suits (docs/29a section 7). Pure."""
    if gameplay < FACE_LED_GAMEPLAY or (moment_type or "") in FACE_LED_MOMENTS:
        return "face_led"
    if gameplay > MOMENT_LED_GAMEPLAY and face_area < MOMENT_LED_FACE_AREA:
        return "moment_led"
    return "split"


def rendered_score(base: float, *, phrase: bool, cut: bool) -> float:
    """A rendered frame's score: its clean frame's score, +0.3 for a whole caption phrase, -0.5 for a cut
    word. Pure."""
    return base + (PHRASE_BONUS if phrase else 0.0) - (CUT_PENALTY if cut else 0.0)


def nearest(scored: Sequence[FrameScore], t_ms: int) -> FrameScore | None:
    """The scored frame closest in time. Pure."""
    return min(scored, key=lambda f: abs(f.t_ms - t_ms), default=None)


# ── Image work (OpenCV, MediaPipe) ────────────────────────────


class Landmarker:
    """A warm MediaPipe Face Landmarker with blendshapes (one per job)."""

    def __init__(self, model_path: str) -> None:
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path),
            output_face_blendshapes=True,
            num_faces=1,
            running_mode=vision.RunningMode.IMAGE,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)

    def close(self) -> None:
        self._landmarker.close()

    def face(self, rgb: Any) -> FaceFeatures:
        """Features of the main face in an RGB numpy image (H, W, 3)."""
        import cv2
        import mediapipe as mp
        import numpy as np

        height, width = rgb.shape[:2]
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        result = self._landmarker.detect(image)
        if not result.face_landmarks or not result.face_blendshapes:
            return FaceFeatures(found=False)
        points = result.face_landmarks[0]
        xs = [p.x * width for p in points]
        ys = [p.y * height for p in points]
        x0, x1 = max(0, int(min(xs))), min(width, int(max(xs)) + 1)
        y0, y1 = max(0, int(min(ys))), min(height, int(max(ys)) + 1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return FaceFeatures(found=False)
        edge_cut = (
            min(xs) < EDGE_MARGIN_PX
            or min(ys) < EDGE_MARGIN_PX
            or max(xs) > width - EDGE_MARGIN_PX
            or max(ys) > height - EDGE_MARGIN_PX
        )
        shapes = {c.category_name: float(c.score) for c in result.face_blendshapes[0]}
        gray = cv2.cvtColor(np.ascontiguousarray(rgb[y0:y1, x0:x1]), cv2.COLOR_RGB2GRAY)
        clipped = float(np.mean((gray <= 5) | (gray >= 250)))
        return FaceFeatures(
            found=True,
            area=(y1 - y0) / height,
            eyes_open=eyes_open_score(shapes),
            expression=expression_score(shapes),
            sharpness=float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            luma=float(gray.mean()),
            clipped=clipped,
            edge_cut=edge_cut,
            box=(x0, y0, x1 - x0, y1 - y0),
        )


def game_features(rgb: Any, previous: Any | None) -> GameFeatures:
    """Detail, brightness and change of a gameplay frame (both images RGB numpy, any size)."""
    import cv2
    import numpy as np

    small = cv2.resize(rgb, (320, max(2, round(rgb.shape[0] * 320 / rgb.shape[1]))))
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    detail = float(np.mean(cv2.Canny(gray, 80, 160) > 0))
    change = 0.0
    if previous is not None:
        prev_small = cv2.resize(previous, (gray.shape[1], gray.shape[0]))
        prev_gray = cv2.cvtColor(prev_small, cv2.COLOR_RGB2GRAY)
        change = float(np.mean(cv2.absdiff(gray, prev_gray))) / 255.0
    return GameFeatures(detail=detail, luma=float(gray.mean()), change=change)


def with_rejection(frame: FrameScore, reason: str) -> FrameScore:
    """A copy marked rejected. Pure."""
    return replace(frame, rejected=reason)

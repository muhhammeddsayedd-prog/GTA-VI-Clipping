"""Cover-ready video (cover engine stage 0, docs/29a section 7, scoring_config `cover.video_ready`).

YouTube picks our Shorts thumbnails from the video's own frames, and TikTok's `cover_timestamp` points at a
rendered frame, so the frames a platform is likely to pick must already look like a cover:

- layout C draws a taller facecam panel (`facecam_h`, 768 of 1920 px = 40% by default, instead of 672);
- the facecam crop is placed on the streamer's face (found with MediaPipe on a few frames) so the face is
  centred and fills `face_target` of the panel's height (layouts A and C);
- in the first `open_s` seconds and `peak_pad_s` around the clip's peak, chunked captions show as one
  complete phrase (`phrase_words`, `phrase_chars`), never a word or two and never a cut word;
- when the gameplay is very dark at the start (mean luma under `dark_luma`), it is lifted with a gamma of
  `dark_gamma` for the first `dark_s` seconds, fading back over the last `dark_fade_s`.

Nothing here changes the clip's length, cut points or audio. Every number is a key of the flag's JSON.
The face detector and the luma measurement are optional: when either fails, the render goes on without
that part.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from sixstars.factory import tools
from sixstars.factory.anchor import CHAT_LAG_S
from sixstars.factory.layout import C_FACECAM_H, Box, FaceFraming, GameLift
from sixstars.logging import get_logger
from sixstars.scoring import config as scoring_config

FLAG = "cover.video_ready"
# MediaPipe's BlazeFace short-range model, baked into the image (docker/Dockerfile).
FACE_MODEL_ENV = "SIXSTARS_FACE_MODEL"
FACE_MODEL_DEFAULT = "/models/mediapipe/blaze_face_short_range.tflite"
FACE_MIN_SCORE = 0.5
FRAME_TIMEOUT_S = 30.0
# A peak needs at least this many chat lines in its best second to count as a chat peak.
CHAT_PEAK_MIN_LINES = 3

Span = tuple[float, float]


@dataclass(frozen=True, slots=True)
class CoverVideoRules:
    """The `cover.video_ready` flag's numbers (defaults from docs/29a section 7, stage 0)."""

    facecam_h: int = 768
    face_target: float = 0.40
    face_min_crop_px: int = 180
    face_samples: int = 5
    open_s: float = 2.0
    peak_pad_s: float = 1.0
    phrase_words: int = 6
    phrase_chars: int = 26
    dark_luma: float = 50.0
    dark_gamma: float = 1.15
    dark_s: float = 1.5
    dark_fade_s: float = 0.5

    @classmethod
    def from_flag(cls, values: Mapping[str, Any]) -> "CoverVideoRules":
        """Rules from the flag's JSON; a missing or non-numeric key keeps its default. Pure."""
        base = cls()
        number = scoring_config.number
        return cls(
            facecam_h=int(min(max(number(values, "facecam_h", base.facecam_h), C_FACECAM_H), 960)),
            face_target=min(max(number(values, "face_target", base.face_target), 0.2), 0.8),
            face_min_crop_px=int(max(number(values, "face_min_crop_px", base.face_min_crop_px), 64)),
            face_samples=int(min(max(number(values, "face_samples", base.face_samples), 1), 12)),
            open_s=max(number(values, "open_s", base.open_s), 0.0),
            peak_pad_s=max(number(values, "peak_pad_s", base.peak_pad_s), 0.0),
            phrase_words=int(max(number(values, "phrase_words", base.phrase_words), 2)),
            phrase_chars=int(max(number(values, "phrase_chars", base.phrase_chars), 8)),
            dark_luma=number(values, "dark_luma", base.dark_luma),
            dark_gamma=min(max(number(values, "dark_gamma", base.dark_gamma), 1.0), 1.6),
            dark_s=max(number(values, "dark_s", base.dark_s), 0.0),
            dark_fade_s=max(number(values, "dark_fade_s", base.dark_fade_s), 0.0),
        )


def load_rules() -> CoverVideoRules | None:
    """The rules when `cover.video_ready` is on, else None (today's render)."""
    values = scoring_config.flag(FLAG)
    return None if values is None else CoverVideoRules.from_flag(values)


@dataclass(frozen=True, slots=True)
class CoverVideo:
    """What the renderer does differently for one clip (times in clip time)."""

    facecam_h: int
    framing: FaceFraming | None = None
    phrase_windows: tuple[Span, ...] = ()
    phrase_words: int = 6
    phrase_chars: int = 26
    lift: GameLift | None = None
    peak_s: float | None = None
    peak_source: str = ""

    def to_json(self) -> dict[str, Any]:
        """For the variants recipe. Pure."""
        return {
            "facecam_h": self.facecam_h,
            "framing": None
            if self.framing is None
            else {
                "face": [self.framing.face.x, self.framing.face.y, self.framing.face.w, self.framing.face.h],
                "target_share": self.framing.target_share,
                "min_crop_px": self.framing.min_crop_px,
            },
            "phrase_windows": [list(span) for span in self.phrase_windows],
            "phrase_words": self.phrase_words,
            "phrase_chars": self.phrase_chars,
            "lift": None
            if self.lift is None
            else {"gamma": self.lift.gamma, "until_s": self.lift.until_s, "fade_s": self.lift.fade_s},
            "peak_s": self.peak_s,
            "peak_source": self.peak_source,
        }

    @classmethod
    def from_json(cls, value: object) -> "CoverVideo | None":
        """The inverse of `to_json`; None for a missing or broken value. Pure."""
        if not isinstance(value, Mapping):
            return None
        try:
            framing = None
            raw_framing = value.get("framing")
            if isinstance(raw_framing, Mapping):
                x, y, w, h = (int(v) for v in raw_framing["face"])
                framing = FaceFraming(
                    Box(x, y, w, h), float(raw_framing["target_share"]), int(raw_framing["min_crop_px"])
                )
            raw_lift = value.get("lift")
            lift = (
                GameLift(float(raw_lift["gamma"]), float(raw_lift["until_s"]), float(raw_lift["fade_s"]))
                if isinstance(raw_lift, Mapping)
                else None
            )
            peak = value.get("peak_s")
            return cls(
                facecam_h=int(value["facecam_h"]),
                framing=framing,
                phrase_windows=tuple((float(s), float(e)) for s, e in value.get("phrase_windows") or []),
                phrase_words=int(value.get("phrase_words", 6)),
                phrase_chars=int(value.get("phrase_chars", 26)),
                lift=lift,
                peak_s=None if peak is None else float(peak),
                peak_source=str(value.get("peak_source") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ── Pure maths ────────────────────────────────────────────────


def _inside(t: float | None, duration_s: float) -> bool:
    return t is not None and 0.0 <= t < duration_s


def chat_peak_s(chat_times: Sequence[float], duration_s: float) -> float | None:
    """The clip-time second with the most chat lines, moved back by the chat lag (people type after the
    moment). None when no second has `CHAT_PEAK_MIN_LINES` lines. Pure."""
    counts: dict[int, int] = {}
    for t in chat_times:
        second = int(t - CHAT_LAG_S)
        if 0 <= second < duration_s:
            counts[second] = counts.get(second, 0) + 1
    if not counts:
        return None
    second, count = max(counts.items(), key=lambda item: (item[1], -item[0]))
    return float(second) + 0.5 if count >= CHAT_PEAK_MIN_LINES else None


def choose_peak(
    duration_s: float,
    *,
    anchor_s: float | None = None,
    payoff_s: float | None = None,
    chat_times: Sequence[float] = (),
) -> tuple[float | None, str]:
    """The clip's peak in clip time and where it came from: the cut anchor (loudness, chat and speech
    together) when it is inside the clip, else the writer's payoff, else the busiest chat second. Pure."""
    if _inside(anchor_s, duration_s):
        assert anchor_s is not None
        return round(anchor_s, 2), "anchor"
    if _inside(payoff_s, duration_s):
        assert payoff_s is not None
        return round(payoff_s, 2), "payoff"
    chat = chat_peak_s(chat_times, duration_s)
    if chat is not None:
        return chat, "chat"
    return None, ""


def phrase_windows(duration_s: float, peak_s: float | None, open_s: float, pad_s: float) -> tuple[Span, ...]:
    """The spans where captions must show a whole phrase: the opening and around the peak, merged when
    they touch and clipped to the clip. Pure."""
    spans: list[Span] = []
    if open_s > 0:
        spans.append((0.0, min(open_s, duration_s)))
    if peak_s is not None and 0 <= peak_s < duration_s and pad_s > 0:
        spans.append((max(0.0, peak_s - pad_s), min(duration_s, peak_s + pad_s)))
    merged: list[Span] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def in_windows(t: float, windows: Sequence[Span]) -> bool:
    """Is `t` inside one of `windows` (start inclusive, end exclusive)? Pure."""
    return any(start <= t < end for start, end in windows)


def face_share(face_h: float, crop_h: float) -> float:
    """The face's height as a share of the crop's height. Pure."""
    return 0.0 if crop_h <= 0 else face_h / crop_h


def lift_for(mean_luma: float | None, rules: CoverVideoRules, duration_s: float) -> GameLift | None:
    """The opening lift when the gameplay's mean luma (0–255) is under `dark_luma`. Pure."""
    if mean_luma is None or mean_luma >= rules.dark_luma or rules.dark_s <= 0 or rules.dark_gamma <= 1.0:
        return None
    until = min(rules.dark_s, duration_s)
    return GameLift(rules.dark_gamma, until, min(rules.dark_fade_s, until))


def median_box(boxes: Sequence[Box]) -> Box | None:
    """The per-coordinate median of `boxes` (None when empty). Pure."""
    if not boxes:
        return None
    x = int(median(b.x for b in boxes))
    y = int(median(b.y for b in boxes))
    w = int(median(b.w for b in boxes))
    h = int(median(b.h for b in boxes))
    return Box(x, y, max(w, 2), max(h, 2))


def sample_times(duration_s: float, peak_s: float | None, count: int) -> list[float]:
    """Clip times to look for the face: spread over the clip, plus the opening and the peak. Pure."""
    times = {round(duration_s * (n + 0.5) / count, 2) for n in range(max(count, 1))}
    times.add(round(min(1.0, duration_s / 2), 2))
    if peak_s is not None and 0 <= peak_s < duration_s:
        times.add(round(peak_s, 2))
    return sorted(t for t in times if 0 <= t < duration_s)


# ── Measurements (ffmpeg, MediaPipe) ──────────────────────────


class FaceDetectorUnavailableError(RuntimeError):
    """MediaPipe or its model is missing."""


def _frame_rgb(source: Path, at_s: float, box: Box, ffmpeg: str) -> tuple[int, int, bytes]:
    """One source frame cropped to `box`, as raw RGB24."""
    crop = f"crop={box.w}:{box.h}:{box.x}:{box.y}"
    command = [ffmpeg, "-v", "error", "-ss", f"{at_s:.3f}", "-i", str(source), "-frames:v", "1"]
    command += ["-vf", crop, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    result = tools.run(command, timeout_s=FRAME_TIMEOUT_S, what="cover frame")
    return box.w, box.h, result.stdout


def detect_faces(frames: Sequence[tuple[int, int, bytes]]) -> list[Box | None]:
    """The most confident face per RGB frame (frame pixels), or None. Raises `FaceDetectorUnavailableError`
    when MediaPipe or its model is missing."""
    import os

    model = os.environ.get(FACE_MODEL_ENV, FACE_MODEL_DEFAULT)
    if not Path(model).is_file():
        raise FaceDetectorUnavailableError(f"face model not found: {model}")
    try:
        import mediapipe as mp
        import numpy as np
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions
    except ImportError as exc:
        raise FaceDetectorUnavailableError(f"mediapipe not installed: {exc}") from exc
    options = vision.FaceDetectorOptions(
        base_options=BaseOptions(model_asset_path=model), min_detection_confidence=FACE_MIN_SCORE
    )
    found: list[Box | None] = []
    with vision.FaceDetector.create_from_options(options) as detector:
        for width, height, data in frames:
            if len(data) != width * height * 3:
                found.append(None)
                continue
            array = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=array)
            result = detector.detect(image)
            best = max(result.detections, key=lambda d: d.categories[0].score, default=None)
            if best is None:
                found.append(None)
                continue
            b = best.bounding_box
            x, y = max(0, int(b.origin_x)), max(0, int(b.origin_y))
            w, h = min(int(b.width), width - x), min(int(b.height), height - y)
            found.append(Box(x, y, w, h) if w > 1 and h > 1 else None)
    return found


def find_face(
    source: Path, clip_start_s: float, facecam: Box, times: Sequence[float], *, ffmpeg: str = "ffmpeg"
) -> Box | None:
    """The streamer's face in source pixels: the median of the faces found in the facecam box at `times`
    (clip time), or None when it is found in fewer than half of them."""
    frames = [_frame_rgb(source, clip_start_s + t, facecam, ffmpeg) for t in times]
    faces = [face for face in detect_faces(frames) if face is not None]
    if len(faces) * 2 < len(times):
        return None
    local = median_box(faces)
    if local is None:
        return None
    return Box(facecam.x + local.x, facecam.y + local.y, local.w, local.h)


def mean_luma(
    source: Path, start_s: float, duration_s: float, box: Box | None, *, ffmpeg: str
) -> float | None:
    """Mean luma (0–255) of the source over a span, inside `box` when given; None when unreadable."""
    chain = ([f"crop={box.w}:{box.h}:{box.x}:{box.y}"] if box is not None else []) + [
        "scale=160:-2",
        "signalstats",
        "metadata=print:key=lavfi.signalstats.YAVG:file=-",
    ]
    command = [ffmpeg, "-v", "error", "-ss", f"{start_s:.3f}", "-t", f"{duration_s:.3f}", "-i", str(source)]
    command += ["-an", "-vf", ",".join(chain), "-f", "null", "-"]
    result = tools.run(command, timeout_s=FRAME_TIMEOUT_S, what="cover luma")
    values: list[float] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if "YAVG=" in line:
            try:
                values.append(float(line.split("YAVG=", 1)[1]))
            except ValueError:
                continue
    return sum(values) / len(values) if values else None


@dataclass(frozen=True, slots=True)
class PlanInput:
    """What `plan` needs from the pipeline (clip time unless said otherwise)."""

    source: Path
    clip_start_s: float  # file time
    duration_s: float
    facecam: Box | None
    gameplay: Box | None  # the gameplay crop in source pixels (None: the whole frame)
    anchor_s: float | None = None
    payoff_s: float | None = None
    chat_times: tuple[float, ...] = ()
    known_face: Box | None = None


def plan(rules: CoverVideoRules, item: PlanInput, *, ffmpeg: str = "ffmpeg") -> CoverVideo:
    """The cover-ready treatment of one clip. Never raises: a failed measurement drops only its part."""
    log = get_logger("factory")
    peak, source = choose_peak(
        item.duration_s, anchor_s=item.anchor_s, payoff_s=item.payoff_s, chat_times=item.chat_times
    )
    face = item.known_face
    if face is None and item.facecam is not None:
        try:
            face = find_face(
                item.source,
                item.clip_start_s,
                item.facecam,
                sample_times(item.duration_s, peak, rules.face_samples),
                ffmpeg=ffmpeg,
            )
        except (FaceDetectorUnavailableError, tools.FfmpegError) as exc:
            log.warning("cover_face_unavailable", error=str(exc)[:200])
    luma: float | None = None
    try:
        luma = mean_luma(
            item.source, item.clip_start_s, min(rules.dark_s, item.duration_s), item.gameplay, ffmpeg=ffmpeg
        )
    except tools.FfmpegError as exc:
        log.warning("cover_luma_unavailable", error=str(exc)[:200])
    framing = (
        FaceFraming(face, rules.face_target, rules.face_min_crop_px)
        if face is not None and item.facecam is not None
        else None
    )
    cover = CoverVideo(
        facecam_h=rules.facecam_h,
        framing=framing,
        phrase_windows=phrase_windows(item.duration_s, peak, rules.open_s, rules.peak_pad_s),
        phrase_words=rules.phrase_words,
        phrase_chars=rules.phrase_chars,
        lift=lift_for(luma, rules, item.duration_s),
        peak_s=peak,
        peak_source=source,
    )
    log.info(
        "cover_video_plan",
        peak_s=peak,
        peak_source=source,
        face=None if face is None else [face.x, face.y, face.w, face.h],
        gameplay_luma=None if luma is None else round(luma, 1),
        lift=cover.lift is not None,
        windows=[list(span) for span in cover.phrase_windows],
    )
    return cover


def shifted(cover: CoverVideo, rules: CoverVideoRules, offset_s: float, duration_s: float) -> CoverVideo:
    """The same treatment for another cut of the clip whose start is `offset_s` earlier (a story cut):
    the face is kept, the peak moves, the windows are rebuilt; the lift is dropped (it was measured for
    the other opening). Pure."""
    peak = None if cover.peak_s is None else cover.peak_s + offset_s
    if peak is not None and not 0 <= peak < duration_s:
        peak = None
    return CoverVideo(
        facecam_h=cover.facecam_h,
        framing=cover.framing,
        phrase_windows=phrase_windows(duration_s, peak, rules.open_s, rules.peak_pad_s),
        phrase_words=cover.phrase_words,
        phrase_chars=cover.phrase_chars,
        lift=None,
        peak_s=peak,
        peak_source=cover.peak_source if peak is not None else "",
    )

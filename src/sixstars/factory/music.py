"""Music detection on the final clip window (docs/09 section 8) and the duck spans that follow from it.

PANNs audio tagging (Cnn14) runs on 1-second frames of the window: a frame is music when the "Music" class
is above 0.5 or one of the vocal-music classes is above 0.4. `music_ratio` is the share of music frames.
The result is stored on the clip row (`clips.music_ratio`) for the gate, and the per-frame flags drive the
ducking in the render. The frame rule is pure and has unit tests; the model is loaded lazily, once per
process.
"""

import contextlib
import io
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from sixstars.factory import tools
from sixstars.factory.bleep import Span
from sixstars.factory.window import ClipWindow
from sixstars.logging import get_logger

# docs/09 section 8 (fixed by the spec, not settings).
FRAME_S = 1.0
SAMPLE_RATE = 32000
MUSIC_CLASS = "Music"
MUSIC_THRESHOLD = 0.5
VOCAL_CLASSES = ("Song", "Singing", "Rapping", "Pop music", "Hip hop music")
VOCAL_THRESHOLD = 0.4
# A last frame shorter than this is too short to judge and is left out.
MIN_FRAME_S = 0.5
DETECTOR_NAME = "panns-cnn14"

FloatArray = np.ndarray[Any, np.dtype[np.float32]]


class MusicDetectorUnavailableError(RuntimeError):
    """The audio tagging model cannot be loaded on this machine."""


@dataclass(frozen=True, slots=True)
class MusicAnalysis:
    """Music flags of one clip window: one flag per `frame_s` seconds from the window start."""

    frames: tuple[bool, ...]
    frame_s: float = FRAME_S
    detector: str = DETECTOR_NAME

    @property
    def ratio(self) -> float:
        """Music frames divided by all frames (0 for an empty window)."""
        return sum(self.frames) / len(self.frames) if self.frames else 0.0

    def spans(self, duration_s: float) -> list[Span]:
        """The music frames as merged spans in clip time, clamped to `duration_s`."""
        spans: list[Span] = []
        for index, flagged in enumerate(self.frames):
            start = index * self.frame_s
            if not flagged or start >= duration_s:
                continue
            end = min(start + self.frame_s, duration_s)
            if index == len(self.frames) - 1:
                end = duration_s
            if spans and abs(spans[-1].end_s - start) < 1e-6:
                spans[-1] = Span(spans[-1].start_s, end)
            else:
                spans.append(Span(start, end))
        return spans

    def as_json(self) -> dict[str, object]:
        """A JSON form for events and the gate: the ratio, the frame length and the flags as 0/1."""
        return {
            "ratio": round(self.ratio, 4),
            "frame_s": self.frame_s,
            "frames": [int(flag) for flag in self.frames],
            "detector": self.detector,
        }


def frame_flags(probabilities: FloatArray, labels: Sequence[str]) -> list[bool]:
    """Applies the docs/09 rule to class probabilities of shape (frames, classes)."""
    if probabilities.ndim != 2 or probabilities.shape[1] != len(labels):
        raise ValueError("probabilities must have one column per label")
    index = {label: i for i, label in enumerate(labels)}
    if MUSIC_CLASS not in index:
        raise ValueError(f"label {MUSIC_CLASS!r} is missing")
    music = probabilities[:, index[MUSIC_CLASS]] > MUSIC_THRESHOLD
    vocal_columns = [index[name] for name in VOCAL_CLASSES if name in index]
    if vocal_columns:
        music = music | (probabilities[:, vocal_columns] > VOCAL_THRESHOLD).any(axis=1)
    return [bool(flag) for flag in music]


def split_frames(samples: FloatArray, sample_rate: int = SAMPLE_RATE) -> FloatArray:
    """Cuts mono samples into whole frames of `FRAME_S`; a last part of at least `MIN_FRAME_S` is padded
    with silence, a shorter one is dropped. Returns shape (frames, samples per frame)."""
    size = int(sample_rate * FRAME_S)
    whole = samples.size // size
    rest = samples.size - whole * size
    parts = [samples[: whole * size].reshape(whole, size)]
    if rest >= int(sample_rate * MIN_FRAME_S):
        last = np.zeros((1, size), dtype=np.float32)
        last[0, :rest] = samples[whole * size :]
        parts.append(last)
    return np.concatenate(parts, axis=0).astype(np.float32, copy=False)


def duck_spans(analysis: MusicAnalysis | None, max_music_ratio: float, duration_s: float) -> list[Span]:
    """Spans to duck (docs/09 section 6): the music frames of a clip that is under the music limit.

    A clip with no music, or with more music than its gate profile allows (the gate rejects it anyway),
    is not ducked.
    """
    if analysis is None or not any(analysis.frames) or analysis.ratio > max_music_ratio:
        return []
    return analysis.spans(duration_s)


class MusicDetector(Protocol):
    """Anything that can flag the music frames of mono 32 kHz audio."""

    def flags(self, frames: FloatArray) -> list[bool]:
        """One flag per row of `frames` (shape (frames, samples per frame))."""
        ...


def _load_panns(checkpoint: Path) -> tuple[Any, list[str]]:
    """Import PANNs and load Cnn14 on the CPU. Returns the tagger and the AudioSet label names."""
    import panns_inference  # type: ignore[import-untyped, unused-ignore]

    tagger = panns_inference.AudioTagging(checkpoint_path=str(checkpoint), device="cpu")
    return tagger, [str(label) for label in panns_inference.labels]


class PannsDetector:
    """PANNs Cnn14 audio tagging on the CPU. The checkpoint and the label file must be in `~/panns_data`
    (the Docker image bakes them in); the library is imported lazily because it is heavy."""

    BATCH = 16

    def __init__(self) -> None:
        self._model: Any | None = None
        self._labels: list[str] = []
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                data_dir = Path.home() / "panns_data"
                needed = [data_dir / "class_labels_indices.csv", data_dir / "Cnn14_mAP=0.431.pth"]
                missing = [path.name for path in needed if not path.is_file()]
                if missing:
                    # Importing the library without these files makes it shell out to wget.
                    raise MusicDetectorUnavailableError(f"missing in {data_dir}: {', '.join(missing)}")
                try:
                    # The library prints its checkpoint path; logs must stay one JSON object per line.
                    with contextlib.redirect_stdout(io.StringIO()):
                        model, labels = _load_panns(needed[1])
                    self._model, self._labels = model, labels
                except Exception as exc:
                    raise MusicDetectorUnavailableError(
                        f"PANNs failed to load: {type(exc).__name__}"
                    ) from exc
            return self._model

    def flags(self, frames: FloatArray) -> list[bool]:
        model = self._load()
        flags: list[bool] = []
        for start in range(0, frames.shape[0], self.BATCH):
            clipwise, _embedding = model.inference(frames[start : start + self.BATCH])
            flags += frame_flags(np.asarray(clipwise, dtype=np.float32), self._labels)
        return flags


_default_detector: PannsDetector | None = None


def default_detector() -> PannsDetector:
    """The process-wide PANNs detector (the model is loaded on first use)."""
    global _default_detector
    if _default_detector is None:
        _default_detector = PannsDetector()
    return _default_detector


def analyse(
    source: Path,
    window: ClipWindow,
    *,
    detector: MusicDetector | None = None,
    has_audio: bool = True,
    ffmpeg: str = "ffmpeg",
) -> MusicAnalysis:
    """Music flags for `window` of the source file. A source without audio has no music."""
    if not has_audio:
        return MusicAnalysis(frames=())
    raw = tools.read_pcm(
        source, start_s=window.start_s, duration_s=window.duration_s, sample_rate=SAMPLE_RATE, ffmpeg=ffmpeg
    )
    samples = np.frombuffer(raw, dtype=np.float32)
    frames = split_frames(samples)
    if frames.shape[0] == 0:
        return MusicAnalysis(frames=())
    flags = (detector or default_detector()).flags(frames)
    analysis = MusicAnalysis(frames=tuple(flags))
    get_logger("factory").info(
        "music_analysed", frames=len(flags), music_frames=sum(flags), music_ratio=round(analysis.ratio, 3)
    )
    return analysis

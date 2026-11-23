"""Pure timing maths for the factory: context padding, the writer's trim rules, frame snapping and the
end-card timing. Every time is in seconds relative to the source file.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClipWindow:
    """A span of the source file."""

    start_s: float
    end_s: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.start_s) and math.isfinite(self.end_s)):
            raise ValueError("window times must be finite")
        if self.start_s < 0 or self.end_s <= self.start_s:
            raise ValueError(f"invalid window {self.start_s}..{self.end_s}")

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def padded_window(
    window: ClipWindow, context_pad_s: float, file_duration_s: float | None = None
) -> ClipWindow:
    """The window plus `context_pad_s` either side, kept inside the file."""
    if context_pad_s < 0:
        raise ValueError("context_pad_s must not be negative")
    start = max(0.0, window.start_s - context_pad_s)
    end = window.end_s + context_pad_s
    if file_duration_s is not None:
        end = min(end, file_duration_s)
    return ClipWindow(start, end)


def resolve_trim(
    proposed: ClipWindow,
    *,
    writer_start_s: float | None,
    writer_end_s: float | None,
    spike_floor_s: float,
    min_len_s: float,
    max_len_s: float,
    file_duration_s: float,
) -> ClipWindow:
    """Applies the clip writer's in/out points under the rules of docs/05 section 6 and prompt P2.

    The start may move anywhere inside the file but never later than `spike_floor_s`; the end stays inside
    the file; the length is forced into `[min_len_s, max_len_s]` (a short clip is extended at the end first,
    then at the start; a long clip loses its tail). A file shorter than `min_len_s` yields the whole file.
    """
    if min_len_s <= 0 or max_len_s < min_len_s:
        raise ValueError("need 0 < min_len_s <= max_len_s")
    if file_duration_s <= 0:
        raise ValueError("file_duration_s must be positive")
    floor = min(max(spike_floor_s, 0.0), file_duration_s)
    start = proposed.start_s if writer_start_s is None else writer_start_s
    end = proposed.end_s if writer_end_s is None else writer_end_s
    start = min(max(start, 0.0), floor)
    end = min(end, file_duration_s)
    if end - start > max_len_s:
        end = start + max_len_s
    if end - start < min_len_s:
        end = min(start + min_len_s, file_duration_s)
        start = max(0.0, min(start, end - min_len_s))
    return ClipWindow(start, end)


def frame_count(duration_s: float, fps: int) -> int:
    """Whole frames in a duration (nearest frame, at least one)."""
    return max(1, round(duration_s * fps))


def snap_duration(duration_s: float, fps: int) -> float:
    """The duration rounded to a whole number of frames, so video, audio and the end card line up."""
    return frame_count(duration_s, fps) / fps


def total_duration_s(clip_duration_s: float, end_card_s: float, fps: int) -> float:
    """Length of the finished file: the clip plus the end card, both frame-snapped."""
    end_card = snap_duration(end_card_s, fps) if end_card_s > 0 else 0.0
    return snap_duration(clip_duration_s, fps) + end_card

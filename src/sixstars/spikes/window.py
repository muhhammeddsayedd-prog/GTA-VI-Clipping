"""Pure rules that turn a detected spike into a clip window: pre-roll before the spike, duration limits and
merging of overlapping spikes. Implemented in M04.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sixstars.spikes.config import SpikeConfig


@dataclass(frozen=True, slots=True)
class ClipWindow:
    """A clip window on the same clock as the chat (`start`..`end`), the latest allowed start
    (`spike_floor`) and the padded range to download and transcribe for Claude."""

    start: float
    end: float
    spike_floor: float
    pad_start: float
    pad_end: float


def clip_window(
    first_t: float,
    end_t: float | None,
    cfg: SpikeConfig,
    *,
    chat_offset_s: float = 0.0,
    earliest: float | None = None,
) -> ClipWindow:
    """The clip window of a spike whose first candidate bucket starts at `first_t`.

    `end_t` is the first time after the peak where the score fell below `end_z`, or None when the spike
    was cut at its maximum length. The clip starts `lead_s` (plus the platform's chat offset) before the
    spike, ends `tail_s` after the score fell, and its length is clamped to `[min_len_s, max_len_s]`; a
    window that is too long keeps its start. `earliest` (the start of the stream) bounds the start.
    """
    start = first_t - cfg.lead_s - chat_offset_s
    if earliest is not None:
        start = max(start, earliest)
    raw_end = start + cfg.max_len_s if end_t is None else end_t + cfg.tail_s
    end = start + min(max(raw_end - start, cfg.min_len_s), cfg.max_len_s)
    pad_start = start - cfg.context_pad_s
    if earliest is not None:
        pad_start = max(pad_start, earliest)
    return ClipWindow(start, end, first_t, pad_start, end + cfg.context_pad_s)


def merge_by_peak[T](
    spikes: Sequence[T],
    merge_gap_s: float,
    *,
    peak_t: Callable[[T], float],
    score: Callable[[T], float],
) -> list[tuple[T, list[T]]]:
    """Merge spikes whose peaks are closer than `merge_gap_s`: the higher score is kept, the others are
    returned next to it as absorbed. Input order does not matter; the result is in time order."""
    merged: list[tuple[T, list[T]]] = []
    for spike in sorted(spikes, key=peak_t):
        if merged and peak_t(spike) - peak_t(merged[-1][0]) < merge_gap_s:
            kept, absorbed = merged[-1]
            if score(spike) > score(kept):
                merged[-1] = (spike, [*absorbed, kept])
            else:
                absorbed.append(spike)
        else:
            merged.append((spike, []))
    return merged


def in_cooldown(peak_t: float, last_emitted_peak_t: float | None, merge_gap_s: float) -> bool:
    """Live mode cannot take back a spike it already emitted, so a later spike whose peak is closer than
    `merge_gap_s` to the last emitted peak is dropped (it counts as merged into the earlier one)."""
    return last_emitted_peak_t is not None and peak_t - last_emitted_peak_t < merge_gap_s

"""Pure time maths of the replay pass after a live stream: which parts of the video the live lane already
clipped, and which replay spikes fall on them. Implemented in M08.

`live_sessions.clipped_ranges` holds wall-clock ranges. A replay works in seconds from the start of the
video, and the video starts when the stream started, so `offset = wall - live_sessions.started_at`.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Range:
    """A range of the video in seconds from its start."""

    start_s: float
    end_s: float


def parse_wall(value: object) -> datetime | None:
    """An ISO 8601 string or a datetime as an aware UTC datetime; None when it is neither."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def clipped_offsets(clipped_ranges: Iterable[Mapping[str, Any]], started_at: datetime | None) -> list[Range]:
    """`live_sessions.clipped_ranges` as ranges of the video, in time order.

    Entries without two readable times, or that end before they start, are left out. Without
    `started_at` nothing can be converted and the result is empty.
    """
    if started_at is None:
        return []
    origin = parse_wall(started_at)
    if origin is None:
        return []
    ranges: list[Range] = []
    for item in clipped_ranges:
        start, end = parse_wall(item.get("start_wall")), parse_wall(item.get("end_wall"))
        if start is None or end is None or end <= start:
            continue
        ranges.append(Range((start - origin).total_seconds(), (end - origin).total_seconds()))
    return sorted(ranges, key=lambda item: item.start_s)


def overlaps(start_s: float, end_s: float, ranges: Sequence[Range], margin_s: float = 0.0) -> bool:
    """True when `[start_s, end_s]` comes within `margin_s` of any of `ranges`."""
    return any(start_s < item.end_s + margin_s and item.start_s - margin_s < end_s for item in ranges)


def split_clipped[T](
    spikes: Iterable[T],
    ranges: Sequence[Range],
    margin_s: float,
    *,
    window: Callable[[T], tuple[float, float]],
) -> tuple[list[T], list[T]]:
    """Split spikes into `(kept, already clipped)`; `window(spike)` gives its clip window in video seconds."""
    kept: list[T] = []
    dropped: list[T] = []
    for spike in spikes:
        start_s, end_s = window(spike)
        (dropped if overlaps(start_s, end_s, ranges, margin_s) else kept).append(spike)
    return kept, dropped

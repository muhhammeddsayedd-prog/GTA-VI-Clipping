"""The hype montage (development-plan/M18 Phase E, reference R10): which footage goes in as B-roll and when.

The montage cuts the gameplay panel to other gameplay every other `montage_every_s` seconds while the
streamer's face and voice carry on. The B-roll comes from the footage around the moment that the source
file already holds (the padded window, widened for story cuts in Phase C), never from the clip itself:

1. `motion_command` measures how much the picture changes, twice a second, over the padded window
   (a small grey frame, the mean difference to the previous one);
2. `pick_broll` takes the busiest stretches outside the clip, not overlapping, skipping stills
   (menus, loading and black screens barely move);
3. `montage_timeline` places them: the clip opens on its own gameplay, and never cuts away within
   `PAYOFF_GUARD_S` of the payoff or in the last second.

`draw_seam_fade` draws the soft dark band over the seam between gameplay and face. All but the ffmpeg run
and the drawing are pure.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from sixstars.factory.window import ClipWindow

# Motion is sampled this often (frames per second) on a frame this small: plenty to rank stretches.
MOTION_FPS = 2
MOTION_SIZE = (96, 54)
# A stretch whose mean change is below this (0..255 luma) is a still: a menu, a loading or black screen.
STILL_MOTION = 1.5
# Never cut away this close to the payoff, nor in the first or the last second.
PAYOFF_GUARD_S = 1.5
OPENING_S = 1.5
CLOSING_S = 1.0
# Most B-roll cuts per clip, and the footage around the clip a montage needs at least.
MAX_CUTS = 6
MIN_ROOM_S = 9.0
FADE_MAX_ALPHA = 140

_FRAME = re.compile(r"pts_time:(?P<t>[0-9.]+)")
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=(?P<v>[0-9.]+)")


def motion_command(source: Path, window: ClipWindow, *, ffmpeg: str = "ffmpeg") -> list[str]:
    """ffmpeg arguments that print the picture change of `window` (file time) twice a second. Pure."""
    width, height = MOTION_SIZE
    chain = (
        f"fps={MOTION_FPS},scale={width}:{height},format=gray,tblend=all_mode=difference,signalstats,"
        "metadata=print:key=lavfi.signalstats.YAVG"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-ss",
        f"{window.start_s:.3f}",
        "-t",
        f"{window.duration_s:.3f}",
        "-i",
        str(source),
        "-an",
        "-vf",
        chain,
        "-f",
        "null",
        "-",
    ]


def parse_motion(log: str, offset_s: float) -> list[tuple[float, float]]:
    """(file time, change) pairs from the `metadata=print` output; `offset_s` is where the measured window
    starts in the file. Pure."""
    readings: list[tuple[float, float]] = []
    pending: float | None = None
    for line in log.splitlines():
        frame = _FRAME.search(line)
        if frame:
            pending = float(frame["t"])
            continue
        value = _YAVG.search(line)
        if value and pending is not None:
            readings.append((round(offset_s + pending, 3), float(value["v"])))
            pending = None
    return readings


def pick_broll(
    motion: Sequence[tuple[float, float]],
    clip: ClipWindow,
    padded: ClipWindow,
    *,
    length_s: float,
    count: int,
) -> list[ClipWindow]:
    """The `count` busiest stretches of `length_s` seconds in `padded` that do not touch `clip`, do not
    overlap each other and are not stills, busiest first (file time). Pure."""
    if count <= 0 or length_s <= 0:
        return []
    step = 1.0 / MOTION_FPS
    candidates: list[tuple[float, ClipWindow]] = []
    start = padded.start_s
    while start + length_s <= padded.end_s + 1e-6:
        span = ClipWindow(start, start + length_s)
        outside = span.end_s <= clip.start_s or span.start_s >= clip.end_s
        values = [value for t, value in motion if span.start_s <= t < span.end_s]
        if outside and values:
            mean = sum(values) / len(values)
            if mean >= STILL_MOTION:
                candidates.append((mean, span))
        start += step
    picked: list[ClipWindow] = []
    for _, span in sorted(candidates, key=lambda item: (-item[0], item[1].start_s)):
        if all(span.end_s <= other.start_s or span.start_s >= other.end_s for other in picked):
            picked.append(span)
            if len(picked) >= count:
                break
    return picked


def montage_timeline(duration_s: float, every_s: float, payoff_s: float | None) -> list[ClipWindow]:
    """Where the B-roll goes, in clip time: every other `every_s` slot starting with the second, shortened to
    keep clear of the payoff and of the opening and closing seconds; at most `MAX_CUTS`. Pure."""
    if every_s <= 0:
        return []
    slots: list[ClipWindow] = []
    start = every_s
    while start < duration_s and len(slots) < MAX_CUTS:
        begin, end = max(start, OPENING_S), min(start + every_s, duration_s - CLOSING_S)
        if payoff_s is not None and begin < payoff_s + PAYOFF_GUARD_S and end > payoff_s - PAYOFF_GUARD_S:
            end = min(end, payoff_s - PAYOFF_GUARD_S)
        # A slot cut to less than half its length (or to nothing, before the payoff) is left out.
        if end - begin >= every_s / 2:
            slots.append(ClipWindow(begin, end))
        start += 2 * every_s
    return slots


@dataclass(frozen=True, slots=True)
class Fade:
    path: Path
    height: int


def draw_seam_fade(out_path: Path, *, width: int, height: int) -> Fade:
    """A black band, transparent at both edges and `FADE_MAX_ALPHA` in the middle, `width` x `height`."""
    half = max(1, height // 2)
    column = Image.new("RGBA", (1, height))
    for y in range(height):
        alpha = round(FADE_MAX_ALPHA * (1 - abs(y - half) / half))
        column.putpixel((0, y), (0, 0, 0, max(0, alpha)))
    image = column.resize((width, height), Image.Resampling.NEAREST)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    return Fade(out_path, height)

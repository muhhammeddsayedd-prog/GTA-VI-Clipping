"""Rolling on-disk buffer of recent stream segments, from which a clip window is cut seconds after a spike.

Layout of one session's directory (`<DATA_DIR>/live/<session id>/`):

    seg_<run>_<YYYYmmddTHHMMSS>.ts    segments; the time is the UTC second ffmpeg opened the file
    <key>.pin                          segment names a pending cut needs (the janitor leaves them alone)
    cuts/<spike id>.mp4                windows cut for the factory, deleted when they are old

**Index.** A segment's place on the wall clock comes from its name. Inside one recorder run the stream
timestamps continue from segment to segment, so the index is refined with them: each segment's first
timestamp is read once (`probe_start`), the run's anchor is the smallest `name time - timestamp` among the
segments near the window (the moment with the least delivery delay), and a segment's start is
`anchor + timestamp`. That removes the jitter of HLS delivery (data arrives in bursts of 2 to 6 seconds)
and the one-second resolution of the names. Segments that cannot be probed keep their name time.

**Cut.** `extract` joins the bytes of the segments that cover the window (they are MPEG-TS of one run, so
a byte join is a valid stream; the segment still being written is read up to what is on disk) and remuxes
them into one file without re-encoding. The file starts at a segment boundary, which is a keyframe, so the
cut is exact at the start; `Cut.file_start` says where on the wall clock the file begins and the caller
addresses the window inside it in seconds. A window never spans two recorder runs: there is a hole in the
recording between them. The run that covers most of the window is used and the window is clamped to it.

**Bounded disk.** `sweep` deletes segments that ended more than `live.buffer_s` ago and, oldest first,
whatever exceeds `live.max_buffer_bytes`; pinned segments are kept until their pin is removed.

`scan`, `spans`, `anchored_starts`, `plan_cut` and `expired` hold the arithmetic and are pure.
"""

import contextlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import IO, Final

from sixstars.logging import get_logger

SEGMENT_RE: Final = re.compile(r"^seg_(\d{4})_(\d{8}T\d{6})\.ts$")
NAME_TIME_FORMAT: Final = "%Y%m%dT%H%M%S"
CUTS_DIR: Final = "cuts"
PIN_SUFFIX: Final = ".pin"
PROBE_TIMEOUT_S: Final = 15.0
CUT_TIMEOUT_S: Final = 120.0
COPY_CHUNK: Final = 1 << 20
ANCHOR_TOLERANCE_S: Final = 30.0  # offsets further than this below the median are a timestamp jump
STDERR_TAIL_CHARS: Final = 600

_log = get_logger("live")


class BufferCutError(RuntimeError):
    """The window cannot be cut from the buffer (nothing recorded there, or ffmpeg failed)."""


@dataclass(frozen=True, slots=True)
class Segment:
    """One segment file. `start` is the unix time in its name."""

    path: Path
    run: int
    start: float
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True, slots=True)
class Span:
    """A segment with its place on the wall clock: `[start, end)` in unix seconds."""

    segment: Segment
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class CutPlan:
    """Which segments a window needs. `file_start` is the wall time at which the first one begins;
    `start` / `end` are the window clamped to what the run holds."""

    run: int
    segments: tuple[Segment, ...]
    file_start: float
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class Cut:
    """A window cut from the buffer. Positions inside `path` are `wall time - file_start`."""

    path: Path
    file_start: float
    duration_s: float
    start: float
    end: float
    segments: tuple[str, ...]
    size_bytes: int

    def offset(self, wall: float) -> float:
        """Seconds into the file of a wall-clock time, kept inside the file."""
        return min(max(wall - self.file_start, 0.0), self.duration_s)


# ── names and scanning ───────────────────────────────────────


def segment_pattern(run: int) -> str:
    """The strftime pattern ffmpeg's segment muxer fills in for recorder run `run`."""
    return f"seg_{run:04d}_{NAME_TIME_FORMAT}.ts"


def segment_name(run: int, start: datetime) -> str:
    """The name of the segment of run `run` opened at `start`."""
    return f"seg_{run:04d}_{start.astimezone(UTC).strftime(NAME_TIME_FORMAT)}.ts"


def parse_segment_name(name: str) -> tuple[int, float] | None:
    """`(run, unix start)` from a segment file name, or None when it is not one."""
    match = SEGMENT_RE.match(name)
    if match is None:
        return None
    try:
        start = datetime.strptime(match.group(2), NAME_TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return int(match.group(1)), start.timestamp()


def scan(directory: Path) -> list[Segment]:
    """The segment files of a buffer directory, oldest first. Files that vanish meanwhile are skipped."""
    segments: list[Segment] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for path in entries:
        parsed = parse_segment_name(path.name)
        if parsed is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        segments.append(Segment(path, parsed[0], parsed[1], stat.st_size, stat.st_mtime))
    return sorted(segments, key=lambda item: (item.run, item.start, item.name))


def next_run(directory: Path) -> int:
    """The number of the next recorder run in this directory (1 for an empty one)."""
    return max((segment.run for segment in scan(directory)), default=0) + 1


def dir_bytes(directory: Path) -> int:
    """Bytes of everything under `directory`."""
    total = 0
    with contextlib.suppress(OSError):
        for path in directory.rglob("*"):
            with contextlib.suppress(OSError):
                if path.is_file():
                    total += path.stat().st_size
    return total


# ── the index (pure) ─────────────────────────────────────────


def spans(
    segments: Sequence[Segment], starts: Mapping[str, float] | None = None, now: float | None = None
) -> list[Span]:
    """Each segment's wall-clock interval. A segment ends where the next one of its run starts; the last
    segment of a run ends when it was last written (its modification time, never later than `now`)."""
    starts = starts or {}
    ordered = sorted(segments, key=lambda item: (item.run, starts.get(item.name, item.start), item.name))
    result: list[Span] = []
    for index, segment in enumerate(ordered):
        start = starts.get(segment.name, segment.start)
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        if following is not None and following.run == segment.run:
            end = starts.get(following.name, following.start)
        else:
            end = max(segment.mtime, start)
            if now is not None:
                end = min(end, now)
        result.append(Span(segment, start, max(end, start)))
    return result


def anchored_starts(
    segments: Sequence[Segment], timestamps: Mapping[str, float], *, around: float, span_s: float
) -> dict[str, float]:
    """Wall-clock starts refined with the stream timestamps of one run.

    `timestamps` maps segment names to the first timestamp in the file. The anchor is the smallest
    `name time - timestamp` among the probed segments whose name time lies within `span_s` of `around`
    (all probed segments when none does); values far below the median are ignored, because they come from
    a jump in the stream's timestamps and not from fast delivery. Segments without a timestamp are left
    out of the result (the caller falls back to the name time).
    """
    probed = [segment for segment in segments if segment.name in timestamps]
    if not probed:
        return {}
    near = [segment for segment in probed if abs(segment.start - around) <= span_s] or probed
    offsets = [segment.start - timestamps[segment.name] for segment in near]
    middle = median(offsets)
    anchor = min(offset for offset in offsets if offset >= middle - ANCHOR_TOLERANCE_S)
    starts: dict[str, float] = {}
    for segment in probed:
        start = anchor + timestamps[segment.name]
        # A timestamp jump inside the run would move a segment far from its name: trust the name then.
        if abs(start - segment.start) <= ANCHOR_TOLERANCE_S:
            starts[segment.name] = start
    return starts


def plan_cut(spans_: Sequence[Span], start_wall: float, end_wall: float) -> CutPlan | None:
    """Choose the segments for the window `[start_wall, end_wall)`, or None when nothing covers it.

    Only one recorder run is used: the one that holds most of the window. The window is clamped to that
    run. The file begins at the start of the segment that contains the (clamped) window start.
    """
    if end_wall <= start_wall:
        return None
    by_run: dict[int, list[Span]] = {}
    for span in spans_:
        by_run.setdefault(span.segment.run, []).append(span)
    best: tuple[float, int] | None = None
    for run, items in by_run.items():
        covered = min(end_wall, max(item.end for item in items)) - max(
            start_wall, min(item.start for item in items)
        )
        if covered > 0 and (best is None or covered > best[0]):
            best = (covered, run)
    if best is None:
        return None
    items = sorted(by_run[best[1]], key=lambda item: item.start)
    start = max(start_wall, items[0].start)
    end = min(end_wall, items[-1].end)
    chosen = [item for item in items if item.end > start and item.start < end]
    if not chosen:
        return None
    return CutPlan(
        run=best[1],
        segments=tuple(item.segment for item in chosen),
        file_start=chosen[0].start,
        start=start,
        end=end,
    )


def expired(
    spans_: Sequence[Span], pinned: frozenset[str] | set[str], now: float, buffer_s: float, max_bytes: int
) -> list[Segment]:
    """The segments the janitor may delete: those that ended more than `buffer_s` ago, then the oldest
    ones until the rest fits in `max_bytes`. Pinned segments and the newest segment are never chosen."""
    ordered = sorted(spans_, key=lambda item: (item.start, item.segment.name))
    if not ordered:
        return []
    newest = ordered[-1].segment.name
    doomed: list[Segment] = []
    kept: list[Span] = []
    for span in ordered:
        free = span.segment.name not in pinned and span.segment.name != newest
        if free and span.end < now - buffer_s:
            doomed.append(span.segment)
        else:
            kept.append(span)
    total = sum(span.segment.size for span in kept)
    for span in kept:
        if total <= max_bytes:
            break
        if span.segment.name in pinned or span.segment.name == newest:
            continue
        doomed.append(span.segment)
        total -= span.segment.size
    return doomed


# ── pins ─────────────────────────────────────────────────────


def pin(directory: Path, key: str, names: Sequence[str]) -> Path:
    """Protect the named segments from the janitor until `unpin(directory, key)`."""
    path = directory / f"{key}{PIN_SUFFIX}"
    path.write_text("\n".join(names) + "\n", encoding="utf-8")
    return path


def unpin(directory: Path, key: str) -> None:
    with contextlib.suppress(OSError):
        (directory / f"{key}{PIN_SUFFIX}").unlink()


def pinned(directory: Path, now: float | None = None, max_age_s: float | None = None) -> set[str]:
    """Every segment name listed in a pin file. A pin older than `max_age_s` is removed and ignored, so
    a cut that crashed cannot hold segments for ever."""
    names: set[str] = set()
    try:
        files = [path for path in directory.iterdir() if path.name.endswith(PIN_SUFFIX)]
    except OSError:
        return names
    for path in files:
        try:
            if now is not None and max_age_s is not None and now - path.stat().st_mtime > max_age_s:
                path.unlink()
                continue
            names.update(
                line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
        except OSError:
            continue
    return names


# ── janitor ──────────────────────────────────────────────────


def sweep(directory: Path, *, now: float, buffer_s: float, max_bytes: int, pin_max_age_s: float) -> int:
    """Delete what the buffer no longer needs. Returns the number of segments deleted."""
    doomed = expired(spans(scan(directory)), pinned(directory, now, pin_max_age_s), now, buffer_s, max_bytes)
    deleted = 0
    for segment in doomed:
        try:
            segment.path.unlink()
            deleted += 1
        except OSError:
            continue  # still open on Windows, or already gone: the next sweep tries again
    return deleted


def sweep_cuts(directory: Path, *, now: float, keep_s: float) -> int:
    """Delete cut files older than `keep_s`. Returns how many were deleted."""
    deleted = 0
    try:
        files = list((directory / CUTS_DIR).iterdir())
    except OSError:
        return 0
    for path in files:
        try:
            if now - path.stat().st_mtime > keep_s:
                path.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted


def remove_segments(directory: Path) -> int:
    """Delete every segment and pin of a finished session (cuts stay until they are old)."""
    removed = 0
    for segment in scan(directory):
        with contextlib.suppress(OSError):
            segment.path.unlink()
            removed += 1
    with contextlib.suppress(OSError):
        for path in directory.iterdir():
            if path.name.endswith(PIN_SUFFIX):
                with contextlib.suppress(OSError):
                    path.unlink()
    return removed


def remove_if_empty(directory: Path) -> bool:
    """Remove a session directory that holds no file any more."""
    try:
        if any(path.is_file() for path in directory.rglob("*")):
            return False
        shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        return False
    return not directory.exists()


# ── probing and cutting ──────────────────────────────────────

_probe_cache: dict[tuple[str, int], float] = {}
PROBE_CACHE_MAX: Final = 4096


def probe_start(path: Path, ffprobe: str = "ffprobe") -> float | None:
    """The first timestamp (seconds) in a segment file, or None when ffprobe cannot tell."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [ffprobe, "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
        return float(done.stdout.decode("ascii", errors="replace").strip().splitlines()[0])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def _timestamps(segments: Sequence[Segment], probe: Callable[[Path], float | None]) -> dict[str, float]:
    """First timestamps of the given segments, read once per file (a segment's start never changes)."""
    result: dict[str, float] = {}
    for segment in segments:
        key = (str(segment.path), int(segment.start))
        value = _probe_cache.get(key)
        if value is None:
            probed = probe(segment.path)
            if probed is None:
                continue
            if len(_probe_cache) >= PROBE_CACHE_MAX:
                _probe_cache.clear()
            value = _probe_cache[key] = probed
        result[segment.name] = value
    return result


def index(
    directory: Path,
    *,
    around: float,
    span_s: float,
    now: float | None = None,
    probe: Callable[[Path], float | None] | None = None,
) -> list[Span]:
    """The wall-clock index of a buffer directory, refined with stream timestamps for the segments within
    `span_s` of `around` (the others keep their name time)."""
    segments = scan(directory)
    starts: dict[str, float] = {}
    for run in {segment.run for segment in segments}:
        near = [
            segment
            for segment in segments
            if segment.run == run and abs(segment.start - around) <= span_s and segment.size > 0
        ]
        if near:
            stamps = _timestamps(near, probe or probe_start)
            starts.update(anchored_starts(near, stamps, around=around, span_s=span_s))
    return spans(segments, starts, now)


def cut_path(directory: Path, key: str) -> Path:
    """Where the cut for `key` (a spike id) is written."""
    return directory / CUTS_DIR / f"{key}.mp4"


def _join_into(plan: CutPlan, out_path: Path, ffmpeg: str) -> None:
    """Feed the bytes of the plan's segments to ffmpeg and remux them (no re-encode) into `out_path`."""
    duration = plan.end - plan.file_start
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "mpegts",
        "-i",
        "pipe:0",
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # stderr goes to a file, not a pipe: a pipe nobody reads would block ffmpeg, and with it the feeding.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(  # noqa: S603 - fixed argument list, no shell
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors
        )
        assert process.stdin is not None
        deadline = time.monotonic() + CUT_TIMEOUT_S
        # A hung ffmpeg would block the write for ever: the timer kills it, which ends the write.
        watchdog = threading.Timer(CUT_TIMEOUT_S, process.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                _feed(plan, process.stdin, deadline)
            finally:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            try:
                process.wait(max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise BufferCutError("cutting the window timed out") from exc
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(5)
        errors.seek(0)
        stderr = errors.read().decode("utf-8", errors="replace")
    if process.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        raise BufferCutError(f"ffmpeg could not cut the window: {stderr[-STDERR_TAIL_CHARS:]}")


def _feed(plan: CutPlan, sink: IO[bytes], deadline: float) -> None:
    """Write the bytes of the plan's segments to ffmpeg's input. ffmpeg stops reading once it has the
    duration it was asked for; the closed pipe then ends the feeding and is not an error."""
    for segment in plan.segments:
        try:
            source = segment.path.open("rb")
        except OSError as exc:
            raise BufferCutError(f"a buffer segment could not be read: {segment.name}") from exc
        with source:
            while True:
                if time.monotonic() > deadline:
                    raise BufferCutError("cutting the window timed out")
                try:
                    chunk = source.read(COPY_CHUNK)
                except OSError as exc:
                    raise BufferCutError(f"a buffer segment could not be read: {segment.name}") from exc
                if not chunk:
                    break
                try:
                    sink.write(chunk)
                except OSError:  # BrokenPipeError, or EINVAL on Windows: ffmpeg has what it needs
                    return


def probe_duration(path: Path, ffprobe: str = "ffprobe") -> float:
    """Length of a media file in seconds."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
        return float(done.stdout.decode("ascii", errors="replace").strip().splitlines()[0])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        raise BufferCutError(f"ffprobe could not read {path.name}") from exc


def wait_seconds(end_wall: float, now: float, settle_s: float, max_wait_s: float) -> float:
    """How long to wait before cutting a window that ends at `end_wall`: until the data for its end has
    had `settle_s` to arrive, but never longer than `max_wait_s`. Pure."""
    return min(max(end_wall + settle_s - now, 0.0), max(max_wait_s, 0.0))


def extract(
    directory: Path,
    start_wall: float,
    end_wall: float,
    out_path: Path,
    *,
    anchor_span_s: float,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    clock: Callable[[], float] = time.time,
    pin_key: str | None = None,
) -> Cut:
    """Cut `[start_wall, end_wall)` (unix seconds) out of the buffer into `out_path`, without re-encoding.

    The caller waits first (`wait_seconds`) when the window ends in the future. Raises `BufferCutError` when
    the buffer holds nothing of the window.
    """
    now = clock()
    spans_ = index(
        directory,
        around=(start_wall + end_wall) / 2,
        span_s=anchor_span_s,
        now=now,
        probe=lambda path: probe_start(path, ffprobe),
    )
    plan = plan_cut(spans_, start_wall, end_wall)
    if plan is None:
        raise BufferCutError("the buffer holds nothing of this window")
    if pin_key is not None:
        pin(directory, pin_key, [segment.name for segment in plan.segments])
    try:
        _join_into(plan, out_path, ffmpeg)
    finally:
        if pin_key is not None:
            unpin(directory, pin_key)
    duration = probe_duration(out_path, ffprobe)
    cut = Cut(
        path=out_path,
        file_start=plan.file_start,
        duration_s=duration,
        start=plan.start,
        end=min(plan.end, plan.file_start + duration),
        segments=tuple(segment.name for segment in plan.segments),
        size_bytes=out_path.stat().st_size,
    )
    _log.info(
        "buffer_cut",
        run=plan.run,
        segments=len(plan.segments),
        duration_s=round(duration, 2),
        size_bytes=cut.size_bytes,
        lead_in_s=round(plan.start - plan.file_start, 2),
    )
    return cut

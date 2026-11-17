"""Downloads only the needed time sections of a video with yt-dlp, instead of the whole file. Implemented in
M08.

One call fetches one padded spike window: `yt-dlp --download-sections "*<a>-<b>"` at a capped height. By
default the streams are copied, not re-encoded: ffmpeg starts at the key frame before `a` and writes an
edit list, so the file decodes from exactly `a` (checked against a re-encoded cut of the same window) and
costs no CPU time. `exact_cuts=True` adds `--force-keyframes-at-cuts` (a re-encode) for sources where the
copy is not accurate.

Nothing here touches the database: the caller passes the access (proxy, cookies) and the limits.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sixstars.factory import tools
from sixstars.logging import get_logger
from sixstars.vod import ytdlp
from sixstars.vod.ytdlp import Access, Failure, Runner, YtDlpError

CONTAINER: Final = "mp4"
# A section shorter than this share of what was asked for counts as a failed download.
MIN_DURATION_SHARE: Final = 0.5
MIN_SECTION_S: Final = 1.0


@dataclass(frozen=True, slots=True)
class Section:
    """A time range of the original video, in seconds from its start."""

    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def spec(self) -> str:
        """The value of `--download-sections`."""
        return f"*{self.start_s:.3f}-{self.end_s:.3f}"


@dataclass(frozen=True, slots=True)
class Downloaded:
    """One section on disk. `start_s` is where the file starts in the original video."""

    path: Path
    start_s: float
    duration_s: float
    width: int
    height: int


def section_for(pad_start_s: float, pad_end_s: float, video_duration_s: float | None = None) -> Section:
    """The range to download for a padded window: never before 0, never after the end of the video,
    rounded to milliseconds. Raises `ValueError` when nothing is left."""
    start = max(0.0, pad_start_s)
    end = pad_end_s if video_duration_s is None or video_duration_s <= 0 else min(pad_end_s, video_duration_s)
    start, end = round(start, 3), round(end, 3)
    if end - start < MIN_SECTION_S:
        raise ValueError(f"section {pad_start_s:.1f}-{pad_end_s:.1f} s is empty or outside the video")
    return Section(start, end)


def format_selector(max_height: int) -> str:
    """Best video up to `max_height` plus best audio, H.264 and AAC first: they can be copied into an MP4
    and decode fast. Falls back to any codec, then to a single combined file, then to a taller format
    (a source that only has taller formats is still better than no clip)."""
    cap = f"[height<={max_height}]"
    return f"bv*{cap}[vcodec^=avc1]+ba[ext=m4a]/bv*{cap}[vcodec^=avc1]+ba/bv*{cap}+ba/b{cap}/bv*+ba/b"


def build_arguments(
    url: str,
    section: Section,
    out_dir: Path,
    name: str,
    *,
    max_height: int,
    exact_cuts: bool = False,
) -> list[str]:
    """The yt-dlp arguments (after the base command) that download one section to `out_dir/name.mp4`."""
    arguments = [
        "--download-sections",
        section.spec,
        "-f",
        format_selector(max_height),
        "--merge-output-format",
        CONTAINER,
        "--no-part",
        "--force-overwrites",
        "--output",
        str(out_dir / f"{name}.%(ext)s"),
    ]
    if exact_cuts:
        arguments.append("--force-keyframes-at-cuts")
    arguments.append(url)
    return arguments


def section_path(out_dir: Path, name: str) -> Path:
    """Where the finished section file is."""
    return out_dir / f"{name}.{CONTAINER}"


def remove_leftovers(out_dir: Path, name: str) -> None:
    """Delete everything yt-dlp wrote for `name` except the finished section file."""
    keep = section_path(out_dir, name)
    for path in out_dir.glob(f"{name}.*"):
        if path != keep:
            path.unlink(missing_ok=True)


def existing(out_dir: Path, name: str, section: Section, *, ffprobe: str = "ffprobe") -> Downloaded | None:
    """The section file of an earlier attempt when it is complete, else None (a broken file is deleted)."""
    path = section_path(out_dir, name)
    if not path.is_file():
        return None
    try:
        media = tools.probe(path, ffprobe)
    except tools.FfmpegError:
        path.unlink(missing_ok=True)
        return None
    if not media.has_video or media.duration_s < section.duration_s * MIN_DURATION_SHARE:
        path.unlink(missing_ok=True)
        return None
    return Downloaded(path, section.start_s, media.duration_s, media.width, media.height)


def download_section(
    url: str,
    section: Section,
    out_dir: Path,
    name: str,
    *,
    access: Access,
    max_height: int,
    timeout_s: float,
    exact_cuts: bool = False,
    runner: Runner | None = None,
    ffprobe: str = "ffprobe",
) -> Downloaded:
    """Download one section and check the file. Returns at once when a complete file is already there.

    Raises `ytdlp.DownloadBlockedError` (bot check or rate limit), `ytdlp.VideoUnavailableError`, or
    `ytdlp.YtDlpError` with kind `forbidden`, `timeout` or `error`. Partial files are removed on failure.
    """
    log = get_logger("vod")
    out_dir.mkdir(parents=True, exist_ok=True)
    ready = existing(out_dir, name, section, ffprobe=ffprobe)
    if ready is not None:
        log.info("section_reused", name=name, start_s=section.start_s, duration_s=ready.duration_s)
        return ready
    arguments = build_arguments(url, section, out_dir, name, max_height=max_height, exact_cuts=exact_cuts)
    try:
        ytdlp.run(arguments, access=access, timeout_s=timeout_s, what="section", runner=runner)
    except YtDlpError:
        remove_leftovers(out_dir, name)
        section_path(out_dir, name).unlink(missing_ok=True)
        raise
    remove_leftovers(out_dir, name)
    done = existing(out_dir, name, section, ffprobe=ffprobe)
    if done is None:
        raise YtDlpError(Failure("error", f"yt-dlp finished but {name}.{CONTAINER} is missing or too short"))
    log.info(
        "section_downloaded",
        name=name,
        start_s=section.start_s,
        duration_s=round(done.duration_s, 2),
        height=done.height,
        size_bytes=done.path.stat().st_size,
    )
    return done


def summarise_failures(kinds: Sequence[str]) -> str:
    """`"forbidden x2, timeout x1"` for a list of failure kinds."""
    counts: dict[str, int] = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    return ", ".join(f"{kind} x{count}" for kind, count in counts.items())

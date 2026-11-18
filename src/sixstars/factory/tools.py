"""Runs ffmpeg and ffprobe for the factory: every call has a timeout, captures stderr and logs its
duration. Commands are argument lists, never shell strings.
"""

import json
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sixstars.logging import get_logger

PROBE_TIMEOUT_S = 30.0
FRAME_TIMEOUT_S = 60.0
AUDIO_TIMEOUT_S = 180.0
RENDER_TIMEOUT_S = 900.0
NVENC_TEST_TIMEOUT_S = 20.0
STDERR_TAIL_CHARS = 1500


class FfmpegError(RuntimeError):
    """ffmpeg or ffprobe failed, timed out or is not installed."""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """What ffprobe says about a media file."""

    width: int
    height: int
    duration_s: float
    has_video: bool
    has_audio: bool
    video_codec: str | None = None
    audio_codec: str | None = None
    fps: float = 0.0
    sample_rate: int = 0


def run(command: Sequence[str], *, timeout_s: float, what: str) -> subprocess.CompletedProcess[bytes]:
    """Run one tool to the end. Raises `FfmpegError` on a non-zero exit, a timeout or a missing binary."""
    log = get_logger("factory")
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(command), capture_output=True, check=False, timeout=timeout_s, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired as exc:
        log.error("ffmpeg_timeout", what=what, timeout_s=timeout_s)
        raise FfmpegError(f"{what} timed out after {timeout_s:g} s") from exc
    except OSError as exc:
        raise FfmpegError(f"{what} could not start: {type(exc).__name__}") from exc
    duration_ms = int((time.monotonic() - started) * 1000)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]
        log.error("ffmpeg_failed", what=what, exit_code=result.returncode, duration_ms=duration_ms)
        raise FfmpegError(f"{what} failed with exit code {result.returncode}: {tail}")
    log.info("ffmpeg_done", what=what, duration_ms=duration_ms)
    return result


def _rate(value: object) -> float:
    if not isinstance(value, str) or "/" not in value:
        return 0.0
    top, bottom = value.split("/", 1)
    try:
        return float(top) / float(bottom) if float(bottom) else 0.0
    except ValueError:
        return 0.0


def _number(value: object) -> float:
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def parse_probe(data: dict[str, object]) -> MediaInfo:
    """Build a `MediaInfo` from `ffprobe -show_streams -show_format -of json` output."""
    streams = data.get("streams")
    video: dict[str, object] | None = None
    audio: dict[str, object] | None = None
    for stream in streams if isinstance(streams, list) else []:
        if not isinstance(stream, dict):
            continue
        if stream.get("codec_type") == "video" and video is None:
            video = stream
        elif stream.get("codec_type") == "audio" and audio is None:
            audio = stream
    fmt = data.get("format")
    duration = _number(fmt.get("duration")) if isinstance(fmt, dict) else 0.0
    if duration <= 0 and video is not None:
        duration = _number(video.get("duration"))
    return MediaInfo(
        width=int(_number(video.get("width"))) if video else 0,
        height=int(_number(video.get("height"))) if video else 0,
        duration_s=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        video_codec=str(video.get("codec_name")) if video else None,
        audio_codec=str(audio.get("codec_name")) if audio else None,
        fps=_rate(video.get("avg_frame_rate")) if video else 0.0,
        sample_rate=int(_number(audio.get("sample_rate"))) if audio else 0,
    )


def probe(path: Path, ffprobe: str = "ffprobe") -> MediaInfo:
    """Read the size, duration and streams of a media file."""
    result = run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        timeout_s=PROBE_TIMEOUT_S,
        what="ffprobe",
    )
    try:
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise FfmpegError("ffprobe printed no JSON") from exc
    if not isinstance(data, dict):
        raise FfmpegError("ffprobe printed no JSON object")
    return parse_probe(data)


def _seek_args(start_s: float, duration_s: float | None) -> list[str]:
    args = ["-ss", f"{max(0.0, start_s):.3f}"]
    if duration_s is not None:
        args += ["-t", f"{duration_s:.3f}"]
    return args


def grab_frame(source: Path, time_s: float, ffmpeg: str = "ffmpeg") -> bytes:
    """One frame of `source` at `time_s` as JPEG bytes (full source resolution)."""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *_seek_args(time_s, None)]
    command += ["-i", str(source), "-frames:v", "1", "-q:v", "3", "-f", "image2pipe", "-c:v", "mjpeg", "-"]
    result = run(command, timeout_s=FRAME_TIMEOUT_S, what="frame grab")
    if not result.stdout:
        raise FfmpegError(f"no frame decoded at {time_s:.2f} s")
    return result.stdout


def build_thumb_command(
    source: str, destination: Path, *, time_s: float, width: int, qscale: int, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """ffmpeg arguments that write one JPEG frame of `source` (a path or an https URL) at `time_s`,
    scaled to `width` pixels (height kept in proportion, even). `-ss` comes before `-i`, so a remote file
    is read from the seek point on, not from the start."""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *_seek_args(time_s, None)]
    command += ["-i", source, "-frames:v", "1", "-vf", f"scale={max(2, int(width))}:-2"]
    command += ["-q:v", str(min(31, max(2, int(qscale)))), "-f", "image2", "-c:v", "mjpeg", str(destination)]
    return command


def grab_thumb(
    source: str, destination: Path, *, time_s: float, width: int, qscale: int, ffmpeg: str = "ffmpeg"
) -> Path:
    """Write one scaled JPEG frame of `source` to `destination` (see `build_thumb_command`)."""
    command = build_thumb_command(
        source, destination, time_s=time_s, width=width, qscale=qscale, ffmpeg=ffmpeg
    )
    run(command, timeout_s=FRAME_TIMEOUT_S, what="thumbnail")
    if not destination.is_file() or destination.stat().st_size == 0:
        raise FfmpegError(f"no frame decoded at {time_s:.2f} s")
    return destination


def extract_audio(
    source: Path,
    destination: Path,
    *,
    start_s: float,
    duration_s: float,
    sample_rate: int,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Write `[start_s, start_s + duration_s]` of the audio of `source` as a mono 16-bit WAV file."""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    command += [*_seek_args(start_s, duration_s), "-i", str(source), "-vn", "-ac", "1"]
    command += ["-ar", str(sample_rate), "-c:a", "pcm_s16le", str(destination)]
    run(command, timeout_s=AUDIO_TIMEOUT_S, what="audio extract")
    return destination


def read_pcm(
    source: Path, *, start_s: float, duration_s: float, sample_rate: int, ffmpeg: str = "ffmpeg"
) -> bytes:
    """`[start_s, start_s + duration_s]` of the audio as raw mono 32-bit float samples."""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    command += [*_seek_args(start_s, duration_s), "-i", str(source), "-vn", "-ac", "1"]
    command += ["-ar", str(sample_rate), "-f", "f32le", "-"]
    return run(command, timeout_s=AUDIO_TIMEOUT_S, what="audio decode").stdout


_nvenc: dict[str, bool] = {}
_nvenc_lock = threading.Lock()


def nvenc_available(ffmpeg: str = "ffmpeg") -> bool:
    """True when this ffmpeg can really encode with `h264_nvenc` (checked once per process).

    The encoder being compiled in is not enough: without an NVIDIA GPU and driver it fails at start, so a
    tiny test encode decides.
    """
    with _nvenc_lock:
        if ffmpeg not in _nvenc:
            _nvenc[ffmpeg] = _test_nvenc(ffmpeg)
        return _nvenc[ffmpeg]


def _test_nvenc(ffmpeg: str) -> bool:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi"]
    command += ["-i", "color=c=black:s=256x256:r=30:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"]
    try:
        result = subprocess.run(
            command, capture_output=True, check=False, timeout=NVENC_TEST_TIMEOUT_S, stdin=subprocess.DEVNULL
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    get_logger("factory").info("nvenc_checked", available=result.returncode == 0)
    return result.returncode == 0

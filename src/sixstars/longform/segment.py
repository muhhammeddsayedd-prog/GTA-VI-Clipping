"""Render one full-frame 16:9 segment (M20 L3): the stream as broadcast, scaled and padded (never cropped),
with the segment's ASS overlay and two-pass loudnorm audio. Every segment gets the same codec, size, frame
rate and sample rate, so the episode joins them without re-encoding.
"""

from dataclasses import dataclass
from pathlib import Path, PurePath

from sixstars.factory.audio import LoudnormMeasurement, build_audio_filters, parse_loudnorm_report
from sixstars.factory.bleep import Span
from sixstars.factory.escape import normalize_filter_path, render_filter
from sixstars.factory.presets import audio_encoder_args, container_args
from sixstars.factory.tools import run
from sixstars.longform.config import RenderSpec

AUDIO_OUT = "aout"
VIDEO_OUT = "vout"


@dataclass(frozen=True)
class SegmentJob:
    """What one segment render needs. `start_s`/`duration_s` are in the fetched file."""

    source: Path
    start_s: float
    duration_s: float
    ass_path: Path
    fonts_dir: PurePath | None
    out_path: Path
    render: RenderSpec
    target_lufs: float
    has_audio: bool = True
    bleeps: tuple[Span, ...] = ()


def video_chain(job: SegmentJob) -> str:
    """Scale to fit, pad to the canvas, burn the overlay. Never a crop."""
    w, h, fps = job.render.width, job.render.height, job.render.fps
    ass_options = [("filename", normalize_filter_path(str(job.ass_path)))]
    if job.fonts_dir is not None:
        ass_options.append(("fontsdir", normalize_filter_path(str(job.fonts_dir))))
    steps = [
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        f"fps={fps}",
        render_filter("ass", ass_options),
        "format=yuv420p",
    ]
    return f"[0:v]{','.join(steps)}[{VIDEO_OUT}]"


def audio_chains(job: SegmentJob, *, measured: LoudnormMeasurement | None, report: bool) -> list[str]:
    return build_audio_filters(
        source="0:a" if job.has_audio else None,
        duration_s=job.duration_s,
        end_card_s=0.0,
        target_lufs=job.target_lufs,
        bleeps=job.bleeps,
        measured=measured,
        report=report,
        out=AUDIO_OUT,
    )


def input_args(job: SegmentJob) -> list[str]:
    return ["-ss", f"{job.start_s:.3f}", "-t", f"{job.duration_s:.3f}", "-i", str(job.source)]


def video_encoder_args(render: RenderSpec) -> list[str]:
    """x264 with a fixed GOP (2 s) so segments join cleanly."""
    return [
        "-c:v",
        "libx264",
        "-preset",
        render.preset,
        "-crf",
        str(render.crf),
        "-maxrate",
        render.maxrate,
        "-bufsize",
        render.bufsize,
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(render.fps),
        "-g",
        str(render.fps * 2),
    ]


def measure_command(job: SegmentJob, ffmpeg: str = "ffmpeg") -> list[str]:
    """First loudnorm pass: audio only, JSON report on stderr."""
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        *input_args(job),
        "-filter_complex",
        ";".join(audio_chains(job, measured=None, report=True)),
        "-map",
        f"[{AUDIO_OUT}]",
        "-vn",
        "-f",
        "null",
        "-",
    ]


def render_command(
    job: SegmentJob, measured: LoudnormMeasurement | None, ffmpeg: str = "ffmpeg"
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        *input_args(job),
        "-filter_complex",
        ";".join([video_chain(job), *audio_chains(job, measured=measured, report=False)]),
        "-map",
        f"[{VIDEO_OUT}]",
        "-map",
        f"[{AUDIO_OUT}]",
        *video_encoder_args(job.render),
        *audio_encoder_args(),
        *container_args(),
        str(job.out_path),
    ]


def render_segment(job: SegmentJob, *, timeout_s: float, ffmpeg: str = "ffmpeg") -> Path:
    """Measure loudness, then render. Writes to a temporary name and renames, so a re-run never sees a
    half-written segment."""
    measured = None
    if job.has_audio:
        report = run(measure_command(job, ffmpeg), timeout_s=timeout_s, what="longform loudness")
        measured = parse_loudnorm_report(report.stderr.decode("utf-8", "replace"))
    tmp = job.out_path.with_name(job.out_path.stem + ".part.mp4")
    run(
        render_command(SegmentJob(**{**job.__dict__, "out_path": tmp}), measured, ffmpeg),
        timeout_s=timeout_s,
        what="longform segment",
    )
    tmp.replace(job.out_path)
    return job.out_path

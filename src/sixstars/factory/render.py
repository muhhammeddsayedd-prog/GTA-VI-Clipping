"""Builds and runs the ffmpeg filter graph that renders the final clip file. Implemented in M06.

This module holds the pure part: the `-filter_complex` graph and the ffmpeg argument lists for normal and
express renders. Arguments are lists, never shell strings.
"""

from dataclasses import dataclass, field
from pathlib import PurePath

from sixstars.factory.audio import LoudnormMeasurement, SfxPlan, build_audio_filters
from sixstars.factory.bleep import Span
from sixstars.factory.layout import Geometry, Layout, LayoutA, layout_filters
from sixstars.factory.overlays import (
    ass_filter,
    end_card_filters,
    progress_filters,
    seam_filter,
    watermark_filter,
)
from sixstars.factory.params import FactoryParams, FontFiles
from sixstars.factory.presets import RenderMode, audio_encoder_args, container_args, video_encoder_args
from sixstars.factory.window import ClipWindow, snap_duration, total_duration_s

VIDEO_OUT = "vout"
AUDIO_OUT = "aout"


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """The source video file and what ffprobe says about it."""

    path: PurePath
    width: int
    height: int
    has_audio: bool = True

    def __post_init__(self) -> None:
        if self.width < 2 or self.height < 2:
            raise ValueError("source dimensions must be at least 2x2")


@dataclass(frozen=True, slots=True)
class RenderSpec:
    """Everything one render needs.

    `window` is the final clip span inside the source file. `ass_path` is the file written from
    `subtitles.build_ass`. `bleeps`, `duck_spans` and `sfx_times_s` are in clip time. `hook_shown` delays
    the watermark until the hook has gone. `loudnorm` carries first-pass measurements for a normal render.
    """

    source: SourceInfo
    window: ClipWindow
    layout: Layout
    ass_path: PurePath
    output_path: PurePath
    series_no: int
    params: FactoryParams
    mode: RenderMode = "normal"
    fonts: FontFiles = field(default_factory=FontFiles)
    bleeps: tuple[Span, ...] = ()
    duck_spans: tuple[Span, ...] = ()
    sfx_path: PurePath | None = None
    sfx_times_s: tuple[float, ...] = ()
    hook_shown: bool = True
    zoom: bool = True
    loudnorm: LoudnormMeasurement | None = None
    nvenc_available: bool = False

    @property
    def clip_duration_s(self) -> float:
        """Clip length without the end card, snapped to whole frames."""
        return snap_duration(self.window.duration_s, self.params.fps)

    @property
    def end_card_s(self) -> float:
        """End card length snapped to whole frames (0 when the end card is off)."""
        return snap_duration(self.params.end_card_s, self.params.fps) if self.params.end_card_s > 0 else 0.0

    @property
    def output_duration_s(self) -> float:
        """Length of the finished file."""
        return total_duration_s(self.window.duration_s, self.params.end_card_s, self.params.fps)


def _seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _path_arg(path: PurePath) -> str:
    """A path as a command argument; a leading dash is guarded so it is never read as an option."""
    text = str(path)
    return f"./{text}" if text.startswith("-") else text


def build_video_filters(spec: RenderSpec) -> list[str]:
    """Video chains: layout, seam, subtitles and overlays, progress bar, then the end card."""
    params = spec.params
    geometry = Geometry.for_canvas(params.width, params.height)
    duration = spec.clip_duration_s
    chains = layout_filters(
        spec.layout,
        source_w=spec.source.width,
        source_h=spec.source.height,
        geometry=geometry,
        fps=params.fps,
        duration_s=duration,
        zoom=spec.zoom,
        out="v_layout",
    )
    text_chain: list[str] = []
    if isinstance(spec.layout, LayoutA):
        text_chain.append(seam_filter(geometry, params.accent_color))
    text_chain.append(ass_filter(spec.ass_path, spec.fonts.fonts_dir))
    watermark = watermark_filter(
        params.watermark,
        geometry=geometry,
        font_name=params.font,
        fonts=spec.fonts,
        start_s=min(params.hook_seconds, duration) if spec.hook_shown else 0.0,
    )
    if watermark is not None:
        text_chain.append(watermark)
    chains.append(f"[v_layout]{','.join(text_chain)}[v_text]")
    chains += progress_filters(
        geometry=geometry,
        accent_color=params.accent_color,
        fps=params.fps,
        duration_s=duration,
        source="v_text",
        out="v_progress",
    )
    finish = f"trim=duration={_seconds(duration)},setpts=PTS-STARTPTS,setsar=1,format=yuv420p"
    if spec.end_card_s <= 0:
        chains.append(f"[v_progress]{finish}[{VIDEO_OUT}]")
        return chains
    chains.append(f"[v_progress]{finish}[v_main]")
    chains += end_card_filters(
        geometry=geometry,
        fps=params.fps,
        end_card_s=spec.end_card_s,
        series_label=params.series_label,
        series_no=spec.series_no,
        accent_color=params.accent_color,
        font_name=params.font,
        fonts=spec.fonts,
        out="v_end",
    )
    chains.append(f"[v_main][v_end]concat=n=2:v=1:a=0[{VIDEO_OUT}]")
    return chains


def _audio_filters(spec: RenderSpec, *, report: bool, with_end_card: bool) -> list[str]:
    sfx = None
    if spec.sfx_path is not None and spec.sfx_times_s:
        sfx = SfxPlan(input_index=1, times_s=spec.sfx_times_s)
    return build_audio_filters(
        source="0:a" if spec.source.has_audio else None,
        duration_s=spec.clip_duration_s,
        end_card_s=spec.end_card_s if with_end_card else 0.0,
        target_lufs=spec.params.loudness_lufs,
        bleeps=spec.bleeps if spec.params.bleep else (),
        duck_spans=spec.duck_spans,
        duck_db=spec.params.music_duck_db,
        sfx=sfx,
        measured=None if report else spec.loudnorm,
        report=report,
        out=AUDIO_OUT,
    )


def build_audio_graph(spec: RenderSpec) -> list[str]:
    """Audio chains for the render: duck, bleep, keyword pops, loudnorm, silence under the end card."""
    return _audio_filters(spec, report=False, with_end_card=True)


def build_filter_complex(spec: RenderSpec) -> str:
    """The whole `-filter_complex` graph, ending in the links `[vout]` and `[aout]`."""
    return ";".join([*build_video_filters(spec), *build_audio_graph(spec)])


def _input_args(spec: RenderSpec) -> list[str]:
    args = [
        "-ss",
        _seconds(spec.window.start_s),
        "-t",
        _seconds(spec.clip_duration_s),
        "-i",
        _path_arg(spec.source.path),
    ]
    if spec.sfx_path is not None and spec.sfx_times_s:
        args += ["-i", _path_arg(spec.sfx_path)]
    return args


def build_render_command(spec: RenderSpec, *, ffmpeg: str = "ffmpeg") -> list[str]:
    """The ffmpeg argument list for the render (normal or express, per `spec.mode`)."""
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        *_input_args(spec),
        "-filter_complex",
        build_filter_complex(spec),
        "-map",
        f"[{VIDEO_OUT}]",
        "-map",
        f"[{AUDIO_OUT}]",
        *video_encoder_args(spec.params, spec.mode, nvenc_available=spec.nvenc_available),
        *audio_encoder_args(),
        *container_args(),
        _path_arg(spec.output_path),
    ]


def build_loudnorm_measure_command(spec: RenderSpec, *, ffmpeg: str = "ffmpeg") -> list[str]:
    """First pass of the two-pass loudness normalisation used by normal renders.

    Runs the audio chain only and prints loudnorm's JSON report on stderr; feed that to
    `audio.parse_loudnorm_report` and put the result in `RenderSpec.loudnorm` for the real render.
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        *_input_args(spec),
        "-filter_complex",
        ";".join(_audio_filters(spec, report=True, with_end_card=False)),
        "-map",
        f"[{AUDIO_OUT}]",
        "-vn",
        "-f",
        "null",
        "-",
    ]

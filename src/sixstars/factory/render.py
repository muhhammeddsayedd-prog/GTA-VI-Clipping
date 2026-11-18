"""Builds and runs the ffmpeg filter graph that renders the final clip file. Implemented in M06.

This module holds the pure part: the `-filter_complex` graph and the ffmpeg argument lists for normal and
express renders. Arguments are lists, never shell strings.
"""

from dataclasses import dataclass, field
from pathlib import PurePath

from sixstars.factory.audio import SFX_GAIN_DB, LoudnormMeasurement, SfxPlan, SfxTrack, build_audio_filters
from sixstars.factory.bleep import Span
from sixstars.factory.escape import normalize_filter_path, render_filter
from sixstars.factory.layout import (
    REF_HEIGHT,
    REF_WIDTH,
    Geometry,
    Layout,
    LayoutA,
    PunchIns,
    ZoomCut,
    layout_filters,
)
from sixstars.factory.overlays import (
    SEAM_TRACK_COLOR,
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
    logo_path: PurePath | None = None
    hook_shown: bool = True
    zoom: bool = True
    loudnorm: LoudnormMeasurement | None = None
    nvenc_available: bool = False
    # docs/27 edit effects (all off by default: the classic look).
    zoom_cut: ZoomCut | None = None
    punch_ins: PunchIns | None = None
    sfx_tracks: tuple[SfxTrack, ...] = ()
    sfx_gain_db: float = SFX_GAIN_DB
    compress: bool = False
    watermark_y: int | None = None
    # docs/27 title bar: a PNG drawn on the 1080x1920 reference canvas, centred, its top at `title_top`
    # (reference pixels), shown for the first `factory.hook_seconds`, or for the whole clip with
    # `title_whole_clip` (the persistent title of the docs/27 section 5 styles).
    title_png: PurePath | None = None
    title_top: int = 0
    title_whole_clip: bool = False

    @property
    def _active_tracks(self) -> tuple[SfxTrack, ...]:
        return tuple(track for track in self.sfx_tracks if track.times_s)

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
    """Video chains: layout, seam, progress bar, subtitles and overlays, then the end card."""
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
        zoom_cut=spec.zoom_cut,
        punch_ins=spec.punch_ins,
    )
    # The seam and the progress bar belong to the split frame: they are hidden while a punch-in fills it.
    shown = None
    if spec.punch_ins is not None and spec.punch_ins.spans:
        shown = f"not({spec.punch_ins.expression()})"
    # The progress bar goes under the text. In layout A it runs along the seam (the seam is its track), so
    # it never crosses the streamer's face; in layout B it sits at the bottom of the safe zone.
    bar_source, bar_y = "v_layout", None
    if isinstance(spec.layout, LayoutA):
        seam_y = geometry.seam_y(spec.layout.kind)
        seam = seam_filter(geometry, SEAM_TRACK_COLOR, enable=shown, y=seam_y)
        chains.append(f"[v_layout]{seam}[v_seam]")
        bar_source = "v_seam"
        bar_y = seam_y - max(0, (geometry.progress_h - geometry.a_seam_h) // 2)
    chains += progress_filters(
        geometry=geometry,
        accent_color=params.accent_color,
        fps=params.fps,
        duration_s=duration,
        source=bar_source,
        out="v_progress",
        y=bar_y,
        enable=shown,
    )
    text_source = "v_progress"
    if spec.title_png is not None and (params.hook_seconds > 0 or spec.title_whole_clip):
        chains += title_filters(spec, geometry, source="v_progress", out="v_titled")
        text_source = "v_titled"
    text_chain = [ass_filter(spec.ass_path, spec.fonts.fonts_dir)]
    watermark = watermark_filter(
        params.watermark,
        geometry=geometry,
        font_name=params.font,
        fonts=spec.fonts,
        start_s=min(params.hook_seconds, duration) if spec.hook_shown else 0.0,
        y=spec.watermark_y,
    )
    if watermark is not None:
        text_chain.append(watermark)
    finish = f"trim=duration={_seconds(duration)},setpts=PTS-STARTPTS,setsar=1,format=yuv420p"
    text_chain.append(finish)
    if spec.end_card_s <= 0:
        chains.append(f"[{text_source}]{','.join(text_chain)}[{VIDEO_OUT}]")
        return chains
    chains.append(f"[{text_source}]{','.join(text_chain)}[v_main]")
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
        logo_path=spec.logo_path,
    )
    chains.append(f"[v_main][v_end]concat=n=2:v=1:a=0[{VIDEO_OUT}]")
    return chains


def title_filters(spec: RenderSpec, geometry: Geometry, *, source: str, out: str) -> list[str]:
    """The title bar PNG read with `movie`, scaled from the reference canvas to the output, laid over
    `source` centred with its top at `spec.title_top` (one still frame, shown for the first
    `factory.hook_seconds` of the clip, then gone so the content has the frame; with
    `spec.title_whole_clip` shown for the whole clip)."""
    assert spec.title_png is not None
    hook_end = (
        spec.clip_duration_s if spec.title_whole_clip else min(spec.params.hook_seconds, spec.clip_duration_s)
    )
    scale = geometry.width / REF_WIDTH
    image = [
        render_filter("movie", [("filename", normalize_filter_path(str(spec.title_png)))]),
        "format=rgba",
    ]
    if geometry.width != REF_WIDTH:
        image.append(render_filter("scale", [("w", f"trunc(iw*{scale:.4f}/2)*2"), ("h", "-2")]))
    y = round(spec.title_top * geometry.height / REF_HEIGHT)
    overlay = render_filter(
        "overlay", [("x", "(W-w)/2"), ("y", str(y)), ("enable", f"lt(t,{_seconds(hook_end)})")]
    )
    return [f"{','.join(image)}[{out}_img]", f"[{source}][{out}_img]{overlay}[{out}]"]


def _sfx_plans(spec: RenderSpec) -> tuple[SfxPlan | None, list[SfxPlan]]:
    """The keyword pop (input 1 when present) and one plan per extra sound effect, in input order."""
    index = 1
    pop = None
    if spec.sfx_path is not None and spec.sfx_times_s:
        pop = SfxPlan(input_index=index, times_s=spec.sfx_times_s, gain_db=spec.sfx_gain_db)
        index += 1
    extra: list[SfxPlan] = []
    for track in spec._active_tracks:
        extra.append(SfxPlan(input_index=index, times_s=track.times_s, gain_db=spec.sfx_gain_db))
        index += 1
    return pop, extra


def _audio_filters(spec: RenderSpec, *, report: bool, with_end_card: bool) -> list[str]:
    sfx, extra = _sfx_plans(spec)
    return build_audio_filters(
        source="0:a" if spec.source.has_audio else None,
        duration_s=spec.clip_duration_s,
        end_card_s=spec.end_card_s if with_end_card else 0.0,
        target_lufs=spec.params.loudness_lufs,
        bleeps=spec.bleeps if spec.params.bleep else (),
        duck_spans=spec.duck_spans,
        duck_db=spec.params.music_duck_db,
        sfx=sfx,
        extra_sfx=extra,
        compress=spec.compress,
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
    for track in spec._active_tracks:
        args += ["-i", _path_arg(track.path)]
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


def build_envelope_command(spec: RenderSpec, *, ffmpeg: str = "ffmpeg") -> list[str]:
    """Measures the momentary loudness of the clip's source audio every 100 ms (`ebur128`), for placing the
    punch-ins; read the readings from stderr with `effects.parse_momentary`. Needs a source with audio."""
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-ss",
        _seconds(spec.window.start_s),
        "-t",
        _seconds(spec.clip_duration_s),
        "-i",
        _path_arg(spec.source.path),
        "-vn",
        "-af",
        "ebur128",
        "-f",
        "null",
        "-",
    ]

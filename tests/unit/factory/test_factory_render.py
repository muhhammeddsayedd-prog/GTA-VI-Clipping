"""ffmpeg command assembly and the whole filter graph (no ffmpeg needed: strings and argument lists only)."""

import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from sixstars.factory.audio import LoudnormMeasurement
from sixstars.factory.bleep import Span
from sixstars.factory.layout import Box, Layout, LayoutA, LayoutB
from sixstars.factory.params import FactoryParams, FontFiles
from sixstars.factory.presets import audio_encoder_args, container_args, uses_nvenc, video_encoder_args
from sixstars.factory.render import (
    RenderSpec,
    SourceInfo,
    build_filter_complex,
    build_loudnorm_measure_command,
    build_render_command,
    build_video_filters,
)
from sixstars.factory.window import ClipWindow

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "factory"
LAYOUT_A = LayoutA(facecam_box=Box(1440, 720, 480, 360))
SpecFactory = Callable[..., RenderSpec]


def assert_snapshot(name: str, actual: str) -> None:
    """Compares with a stored file; run with UPDATE_SNAPSHOTS=1 to (re)write it."""
    path = FIXTURES / name
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert path.is_file(), f"missing snapshot {path}; run with UPDATE_SNAPSHOTS=1"
    assert actual == path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.fixture
def spec_factory(params: FactoryParams) -> SpecFactory:
    def make(layout: Layout = LAYOUT_A, **overrides: object) -> RenderSpec:
        values: dict[str, object] = {
            "source": SourceInfo(PurePosixPath("/data/vod/spike 42.mp4"), 1920, 1080),
            "window": ClipWindow(12.0, 48.0),
            "layout": layout,
            "ass_path": PurePosixPath("/data/render/c1/clip.ass"),
            "output_path": PurePosixPath("/data/ready/c1.mp4"),
            "series_no": 128,
            "params": params,
            "fonts": FontFiles(
                fonts_dir=PurePosixPath("/app/assets/fonts"),
                font_file=PurePosixPath("/app/assets/fonts/Montserrat-Black.ttf"),
            ),
            "bleeps": (Span(4.85, 5.45),),
        }
        values.update(overrides)
        return RenderSpec(**values)  # type: ignore[arg-type]

    return make


def option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


@pytest.mark.parametrize(
    ("mode", "use_nvenc", "nvenc_available", "expected"),
    [
        ("normal", False, False, ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]),
        ("express", False, False, ["-c:v", "libx264", "-preset", "superfast", "-crf", "21"]),
        ("express", True, True, ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]),
        ("express", True, False, ["-c:v", "libx264", "-preset", "superfast", "-crf", "21"]),
        ("express", False, True, ["-c:v", "libx264", "-preset", "superfast", "-crf", "21"]),
        ("normal", True, True, ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]),
    ],
)
def test_video_encoder_args(
    params_factory: Callable[..., FactoryParams],
    mode: str,
    use_nvenc: bool,
    nvenc_available: bool,
    expected: list[str],
) -> None:
    params = params_factory(use_nvenc=use_nvenc)
    args = video_encoder_args(params, mode, nvenc_available=nvenc_available)  # type: ignore[arg-type]
    assert args == [*expected, "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "30"]
    assert uses_nvenc(params, mode, nvenc_available=nvenc_available) is (expected[1] == "h264_nvenc")  # type: ignore[arg-type]


def test_presets_come_from_params(params_factory: Callable[..., FactoryParams]) -> None:
    params = params_factory(preset_normal="medium", preset_express="ultrafast", crf=18, fps=60)
    assert video_encoder_args(params, "normal")[2:6] == ["-preset", "medium", "-crf", "18"]
    assert video_encoder_args(params, "express")[2:6] == ["-preset", "ultrafast", "-crf", "18"]
    assert video_encoder_args(params, "normal")[-2:] == ["-r", "60"]
    with pytest.raises(ValueError):
        video_encoder_args(params, "turbo")  # type: ignore[arg-type]


def test_audio_and_container_args() -> None:
    assert audio_encoder_args() == ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    assert container_args() == ["-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart"]


def test_render_command_normal(spec_factory: SpecFactory) -> None:
    spec = spec_factory()
    command = build_render_command(spec)
    graph = build_filter_complex(spec)
    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        "12",
        "-t",
        "36",
        "-i",
        "/data/vod/spike 42.mp4",
        "-filter_complex",
        graph,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        "/data/ready/c1.mp4",
    ]
    assert all(isinstance(arg, str) for arg in command)


def test_render_command_express_and_custom_binary(spec_factory: SpecFactory) -> None:
    command = build_render_command(spec_factory(mode="express"), ffmpeg="/usr/local/bin/ffmpeg")
    assert command[0] == "/usr/local/bin/ffmpeg"
    assert option(command, "-preset") == "superfast"


def test_render_command_keeps_paths_with_spaces_as_single_arguments(spec_factory: SpecFactory) -> None:
    spec = spec_factory(
        source=SourceInfo(PureWindowsPath("d:\\Personal Projects\\6Starts - GTA VI\\src 1.mp4"), 1920, 1080),
        ass_path=PureWindowsPath("d:\\Personal Projects\\6Starts - GTA VI\\render\\clip a.ass"),
        output_path=PureWindowsPath("d:\\Personal Projects\\6Starts - GTA VI\\ready\\c 1.mp4"),
        fonts=FontFiles(font_file=PureWindowsPath("C:\\Windows\\Fonts\\ariblk.ttf")),
    )
    command = build_render_command(spec)
    assert option(command, "-i") == "d:\\Personal Projects\\6Starts - GTA VI\\src 1.mp4"
    assert command[-1] == "d:\\Personal Projects\\6Starts - GTA VI\\ready\\c 1.mp4"
    graph = option(command, "-filter_complex")
    assert "ass=filename=d\\\\:/Personal Projects/6Starts - GTA VI/render/clip a.ass," in graph
    assert "fontfile=C\\\\:/Windows/Fonts/ariblk.ttf:" in graph
    assert "fontsdir" not in graph


def test_paths_starting_with_a_dash_are_not_read_as_options(spec_factory: SpecFactory) -> None:
    spec = spec_factory(
        source=SourceInfo(PurePosixPath("-evil.mp4"), 1920, 1080), output_path=PurePosixPath("-out.mp4")
    )
    command = build_render_command(spec)
    assert option(command, "-i") == "./-evil.mp4" and command[-1] == "./-out.mp4"


def test_durations_are_frame_snapped(spec_factory: SpecFactory) -> None:
    spec = spec_factory(window=ClipWindow(12.0, 48.016))
    assert spec.clip_duration_s == pytest.approx(36.0)
    assert spec.end_card_s == pytest.approx(1.5)
    assert spec.output_duration_s == pytest.approx(37.5)
    command = build_render_command(spec)
    assert option(command, "-ss") == "12" and option(command, "-t") == "36"


@pytest.mark.parametrize(
    ("layout", "name"),
    [(LAYOUT_A, "filter_layout_a.txt"), (LayoutB(), "filter_layout_b.txt")],
    ids=["A", "B"],
)
def test_filter_graph_snapshot(spec_factory: SpecFactory, layout: Layout, name: str) -> None:
    graph = build_filter_complex(spec_factory(layout))
    assert_snapshot(name, graph.replace(";", ";\n") + "\n")


def test_filter_graph_structure_layout_a(spec_factory: SpecFactory) -> None:
    chains = build_video_filters(spec_factory())
    text_chain = next(chain for chain in chains if chain.startswith("[v_layout]"))
    order = [text_chain.index(part) for part in ("drawbox=", "ass=filename=", "drawtext=text=@6starsclips")]
    assert order == sorted(order)
    assert "enable=gte(t\\,3)" in text_chain
    assert ":fontsdir=/app/assets/fonts," in text_chain
    assert any("overlay=x=-w+w*t/36:y=1500" in chain for chain in chains)
    assert "[v_progress]trim=duration=36,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v_main]" in chains
    assert chains[-1] == "[v_main][v_end]concat=n=2:v=1:a=0[vout]"
    assert any("6 Stars Moments #128" in chain for chain in chains)


def test_layout_b_has_no_seam(spec_factory: SpecFactory) -> None:
    graph = build_filter_complex(spec_factory(LayoutB()))
    assert "drawbox=x=0:y=1147" not in graph and "boxblur" in graph and "vstack" not in graph


def test_filter_graph_options(
    spec_factory: SpecFactory, params_factory: Callable[..., FactoryParams]
) -> None:
    no_hook = build_filter_complex(spec_factory(hook_shown=False))
    assert "enable=gte" not in no_hook
    no_end_card = build_filter_complex(spec_factory(params=params_factory(end_card_s=0.0)))
    assert (
        "concat" not in no_end_card and no_end_card.count("[vout]") == 1 and no_end_card.count("[aout]") == 1
    )
    no_zoom = build_filter_complex(spec_factory(zoom=False))
    assert "eval=frame" not in no_zoom and "eval=frame" in build_filter_complex(spec_factory())
    no_watermark = build_filter_complex(spec_factory(params=params_factory(watermark="")))
    assert "x=1040-text_w" not in no_watermark
    silent_source = build_filter_complex(
        spec_factory(source=SourceInfo(PurePosixPath("/v.mp4"), 1920, 1080, has_audio=False))
    )
    assert "[0:a]" not in silent_source and "anullsrc" in silent_source


def test_bleep_follows_the_setting(
    spec_factory: SpecFactory, params_factory: Callable[..., FactoryParams]
) -> None:
    on = build_filter_complex(spec_factory())
    assert "volume=volume=0:enable=between(t\\,4.85\\,5.45)" in on and "sine=frequency=1000" in on
    off = build_filter_complex(spec_factory(params=params_factory(bleep=False)))
    assert "sine=" not in off and "between(t\\,4.85" not in off


def test_duck_and_loudness_come_from_params(
    spec_factory: SpecFactory, params_factory: Callable[..., FactoryParams]
) -> None:
    spec = spec_factory(
        params=params_factory(music_duck_db=-24, loudness_lufs=-16), duck_spans=(Span(0.0, 5.0),)
    )
    graph = build_filter_complex(spec)
    assert "volume=volume=-24dB:enable=between(t\\,0\\,5)" in graph
    assert "loudnorm=I=-16:TP=-1.5:LRA=11," in graph


def test_keyword_sfx_adds_a_second_input(spec_factory: SpecFactory) -> None:
    plain = build_render_command(spec_factory())
    assert plain.count("-i") == 1
    spec = spec_factory(sfx_path=PurePosixPath("/app/assets/sfx/pop.wav"), sfx_times_s=(0.6, 2.4))
    command = build_render_command(spec)
    assert command.count("-i") == 2 and "/app/assets/sfx/pop.wav" in command
    assert "[1:a]" in option(command, "-filter_complex")
    without_times = build_render_command(spec_factory(sfx_path=PurePosixPath("/app/assets/sfx/pop.wav")))
    assert without_times.count("-i") == 1


def test_two_pass_loudnorm(spec_factory: SpecFactory) -> None:
    spec = spec_factory()
    measure = build_loudnorm_measure_command(spec)
    assert measure[:3] == ["ffmpeg", "-hide_banner", "-nostdin"] and measure[-4:] == [
        "-vn",
        "-f",
        "null",
        "-",
    ]
    graph = option(measure, "-filter_complex")
    assert "print_format=json" in graph and "sine=frequency=1000" in graph
    assert "anullsrc" not in graph and "[vout]" not in graph and "ass=" not in graph
    measured = LoudnormMeasurement(-20.76, -17.75, 0.0, -30.76, 0.02)
    second = build_filter_complex(spec_factory(loudnorm=measured))
    assert (
        "measured_I=-20.76:measured_TP=-17.75:measured_LRA=0:measured_thresh=-30.76:offset=0.02:linear=true"
        in second
    )
    assert "print_format" not in second


def test_source_info_validation() -> None:
    with pytest.raises(ValueError):
        SourceInfo(PurePosixPath("/v.mp4"), 0, 1080)

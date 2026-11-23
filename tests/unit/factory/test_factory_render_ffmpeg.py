"""Real renders with ffmpeg: both layouts with subtitles, hook box, bleep and end card, checked with ffprobe,
frame pixels and the audio spectrum. Skipped when ffmpeg is not installed."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sixstars.factory.audio import parse_loudnorm_report
from sixstars.factory.bleep import ProfanityMatcher, Span, bleep_spans
from sixstars.factory.layout import Box, Layout, LayoutA, LayoutB
from sixstars.factory.lines import Line, clip_lines, group_lines, words_from_transcript
from sixstars.factory.params import FactoryParams, FontFiles
from sixstars.factory.render import (
    RenderSpec,
    SourceInfo,
    build_loudnorm_measure_command,
    build_render_command,
)
from sixstars.factory.subtitles import build_ass
from sixstars.factory.textfit import pillow_measurer
from sixstars.factory.window import ClipWindow

pytestmark = pytest.mark.ffmpeg

ACCENT_RGB = (255, 212, 0)
END_CARD_RGB = (13, 13, 18)
HOSTILE_TEXT = "it's 100%: [a], {b}; c\\d 'q' \"z\" %{pts} 😭"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-4000:]}"
    return result


def probe(ffprobe: str, path: Path) -> dict[str, object]:
    out = run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]).stdout
    data: dict[str, object] = json.loads(out)
    return data


def frame_at(ffmpeg: str, video: Path, time_s: float, out: Path) -> Image.Image:
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{time_s}", "-i", str(video),
         "-frames:v", "1", "-update", "1", str(out)])  # fmt: skip
    return Image.open(out).convert("RGB")


def pixels(
    image: Image.Image, box: tuple[int, int, int, int]
) -> "np.ndarray[tuple[int, ...], np.dtype[np.uint8]]":
    """The RGB values of a (left, top, right, bottom) region as an array of shape (h, w, 3)."""
    return np.asarray(image.crop(box), dtype=np.uint8)


def close_to(pixel: tuple[int, ...], expected: tuple[int, int, int], tolerance: int = 40) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], expected, strict=True))


def tone_power(ffmpeg: str, video: Path, start_s: float, length_s: float, frequency: float) -> float:
    """Spectral power of the audio near `frequency` inside a span of the rendered file."""
    raw = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            f"{start_s}",
            "-t",
            f"{length_s}",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ],  # fmt: skip
        capture_output=True,
        check=True,
    ).stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    assert samples.size > 4800, "no audio decoded"
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size))) ** 2
    freqs = np.fft.rfftfreq(samples.size, 1 / 48000)
    return float(spectrum[(freqs > frequency - 25) & (freqs < frequency + 25)].sum())


def integrated_loudness(ffmpeg: str, video: Path, length_s: float) -> float:
    stderr = run([ffmpeg, "-hide_banner", "-nostdin", "-t", f"{length_s}", "-i", str(video), "-vn",
                  "-af", "ebur128", "-f", "null", "-"]).stderr  # fmt: skip
    summary = stderr[stderr.rindex("Integrated loudness:") :]
    return float(summary.split("I:")[1].split("LUFS")[0])


RenderClip = Callable[..., tuple[RenderSpec, Path]]


@pytest.fixture
def render_clip(
    ffmpeg_tools: tuple[str, str],
    test_font: tuple[str, Path],
    synthetic_source: Path,
    transcript: dict[str, object],
    keywords: dict[int, list[str]],
    profanity_terms: frozenset[str],
    params_factory: Callable[..., FactoryParams],
    tmp_path: Path,
) -> RenderClip:
    """Builds the ASS file and renders one clip from the synthetic source; returns the spec and the MP4."""

    def render(
        layout: Layout,
        window: ClipWindow,
        *,
        workdir: Path | None = None,
        mode: str = "express",
        hook: str = "He did NOT expect that 😭",
        creator: str = "@some_streamer",
        live_badge: bool = False,
        two_pass: bool = False,
        **param_overrides: object,
    ) -> tuple[RenderSpec, Path]:
        font_name, font_file = test_font
        params = params_factory(**{"font": font_name, "watermark": "@6starsclipping", **param_overrides})
        workdir = workdir or tmp_path / "render dir"
        workdir.mkdir(parents=True, exist_ok=True)
        lines: list[Line] = clip_lines(group_lines(words_from_transcript(transcript)), window)
        words = [word for line in lines for word in line.words]
        matcher = ProfanityMatcher(profanity_terms)
        spans = tuple(bleep_spans(words, matcher.find([word.text for word in words]), window.duration_s))
        ass_path = workdir / "clip.ass"
        ass_path.write_text(
            build_ass(
                lines,
                layout=layout.kind,
                params=params,
                duration_s=window.duration_s,
                keywords=keywords,
                profanity=matcher,
                hook=hook,
                creator=creator,
                live_badge=live_badge,
                measure=pillow_measurer(font_file),
            ),
            encoding="utf-8",
        )
        output = workdir / "clip out.mp4"
        spec = RenderSpec(
            source=SourceInfo(synthetic_source, 1920, 1080),
            window=window,
            layout=layout,
            ass_path=ass_path,
            output_path=output,
            series_no=128,
            params=params,
            mode=mode,  # type: ignore[arg-type]
            fonts=FontFiles(font_file=font_file),
            bleeps=spans,
        )
        if two_pass:
            measured = parse_loudnorm_report(
                run(build_loudnorm_measure_command(spec, ffmpeg=ffmpeg_tools[0])).stderr
            )
            assert measured is not None, "loudnorm printed no report"
            spec = RenderSpec(
                **{**{f: getattr(spec, f) for f in spec.__dataclass_fields__}, "loudnorm": measured}
            )
        run(build_render_command(spec, ffmpeg=ffmpeg_tools[0]))
        return spec, output

    return render


def assert_streams(info: dict[str, object], expected_duration: float) -> None:
    streams = info["streams"]
    assert isinstance(streams, list)
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    assert len(streams) == 2
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p" and video["r_frame_rate"] == "30/1"
    assert audio["codec_name"] == "aac" and audio["sample_rate"] == "48000" and audio["channels"] == 2
    assert float(video["duration"]) == pytest.approx(expected_duration, abs=0.1)
    assert float(audio["duration"]) == pytest.approx(expected_duration, abs=0.1)
    fmt = info["format"]
    assert isinstance(fmt, dict)
    assert float(fmt["duration"]) == pytest.approx(expected_duration, abs=0.1)


def test_render_layout_a(render_clip: RenderClip, ffmpeg_tools: tuple[str, str], tmp_path: Path) -> None:
    ffmpeg, ffprobe = ffmpeg_tools
    spec, output = render_clip(
        LayoutA(facecam_box=Box(1440, 720, 480, 360)), ClipWindow(0.0, 10.0), live_badge=True
    )
    assert spec.bleeps == (Span(pytest.approx(4.85), pytest.approx(5.45)),)
    assert_streams(probe(ffprobe, output), expected_duration=11.5)

    # The 440 Hz source tone is replaced by the 1 kHz bleep inside the span and untouched outside it.
    inside_1k = tone_power(ffmpeg, output, 4.95, 0.4, 1000)
    inside_440 = tone_power(ffmpeg, output, 4.95, 0.4, 440)
    outside_1k = tone_power(ffmpeg, output, 2.0, 0.4, 1000)
    outside_440 = tone_power(ffmpeg, output, 2.0, 0.4, 440)
    assert inside_1k > 100 * inside_440
    assert outside_440 > 100 * outside_1k
    assert outside_440 > 100 * inside_440
    # The end card is silent.
    assert tone_power(ffmpeg, output, 10.4, 0.8, 440) < outside_440 / 1e6

    early = frame_at(ffmpeg, output, 1.0, tmp_path / "a_1s.png")
    # Layout A: the seam is the white track of the progress bar, and the accent bar grows along it. The
    # probes sit at the frame edges, clear of the caption drawn over the middle of the seam.
    white = (255, 255, 255)
    assert close_to(early.getpixel((20, 1149)), ACCENT_RGB), "progress bar missing at 10%"
    assert close_to(early.getpixel((1050, 1149)), white), "seam track missing"
    assert close_to(early.getpixel((540, 325)), white), "hook box missing (moved below the LIVE badge)"
    assert close_to(early.getpixel((60, 250)), (232, 33, 43), 50), "LIVE badge missing"
    assert not close_to(early.getpixel((50, 1503)), ACCENT_RGB), "progress bar drawn across the facecam"
    middle = frame_at(ffmpeg, output, 5.0, tmp_path / "a_5s.png")
    assert close_to(middle.getpixel((60, 1149)), ACCENT_RGB) and close_to(
        middle.getpixel((1050, 1149)), white
    )
    late = frame_at(ffmpeg, output, 9.8, tmp_path / "a_late.png")
    assert close_to(late.getpixel((1040, 1149)), ACCENT_RGB), "progress bar did not reach the end"
    assert not close_to(middle.getpixel((540, 325)), (255, 255, 255)), (
        "hook box still shown after hook_seconds"
    )
    card = frame_at(ffmpeg, output, 10.8, tmp_path / "a_end.png")
    assert close_to(card.getpixel((20, 20)), END_CARD_RGB, 12) and close_to(
        card.getpixel((1060, 1900)), END_CARD_RGB, 12
    )
    assert pixels(card, (0, 780, 1080, 1100)).max() > 200, "end card text missing"
    assert pixels(card, (0, 0, 1080, 700)).max() < 40, "end card has content outside the text block"


def test_render_layout_b_with_a_seek(
    render_clip: RenderClip, ffmpeg_tools: tuple[str, str], tmp_path: Path
) -> None:
    ffmpeg, ffprobe = ffmpeg_tools
    spec, output = render_clip(LayoutB(), ClipWindow(2.0, 8.0))
    assert spec.bleeps == (
        Span(pytest.approx(2.85), pytest.approx(3.45)),
    )  # 4.9 s in the source, 2.9 s in the clip
    assert_streams(probe(ffprobe, output), expected_duration=7.5)
    assert tone_power(ffmpeg, output, 2.95, 0.4, 1000) > 100 * tone_power(ffmpeg, output, 2.95, 0.4, 440)
    assert tone_power(ffmpeg, output, 0.5, 0.4, 440) > 100 * tone_power(ffmpeg, output, 0.5, 0.4, 1000)

    early = frame_at(ffmpeg, output, 1.0, tmp_path / "b_1s.png")
    assert close_to(early.getpixel((540, 275)), (255, 255, 255)), "hook box missing at y=260..420"
    # Blurred, darkened fill above the gameplay; sharp gameplay from y=285.
    assert pixels(early, (0, 40, 1080, 200)).max() < 200, "background is not darkened"
    late = frame_at(ffmpeg, output, 5.0, tmp_path / "b_5s.png")
    assert close_to(late.getpixel((800, 1503)), ACCENT_RGB) and not close_to(
        late.getpixel((1000, 1503)), ACCENT_RGB
    )
    # Captions: white glyphs with a black outline in the band above the progress bar, none below it.
    caption_band = pixels(late, (200, 1400, 880, 1495))
    assert (caption_band.min(axis=2) > 245).any(), "no white caption glyphs"
    assert (caption_band.max(axis=2) < 10).any(), "no black caption outline"
    card = frame_at(ffmpeg, output, 6.8, tmp_path / "b_end.png")
    assert close_to(card.getpixel((20, 20)), END_CARD_RGB, 12)


def test_render_with_hostile_text_and_paths(
    render_clip: RenderClip, ffmpeg_tools: tuple[str, str], tmp_path: Path
) -> None:
    workdir = tmp_path / "it's, a [dir]; 100% {x} #1"
    _spec, output = render_clip(
        LayoutA(facecam_box=Box(0, 0, 480, 360)),
        ClipWindow(4.0, 6.0),
        workdir=workdir,
        hook=HOSTILE_TEXT,
        creator=HOSTILE_TEXT,
        live_badge=True,
        series_label=HOSTILE_TEXT,
        watermark=HOSTILE_TEXT,
        hook_seconds=0.5,
        end_card_s=0.5,
    )
    assert output.parent == workdir and output.is_file()
    assert_streams(probe(ffmpeg_tools[1], output), expected_duration=2.5)


def test_two_pass_loudness_hits_the_target(render_clip: RenderClip, ffmpeg_tools: tuple[str, str]) -> None:
    spec, output = render_clip(
        LayoutA(facecam_box=Box(1440, 720, 480, 360)), ClipWindow(0.0, 10.0), mode="normal", two_pass=True
    )
    assert spec.loudnorm is not None
    assert integrated_loudness(ffmpeg_tools[0], output, 10.0) == pytest.approx(-14.0, abs=1.0)


@pytest.fixture(scope="module")
def static_source(ffmpeg_tools: tuple[str, str], tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 6-second 1920x1080 still test pattern (SMPTE bars under a fine grid) with a tone: every frame of the
    source is the same picture, so any change between rendered frames comes from the render."""
    path = tmp_path_factory.mktemp("static source") / "static 6s.mp4"
    run([ffmpeg_tools[0], "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "smptehdbars=size=1920x1080:rate=30:duration=6,drawgrid=w=37:h=29:t=2:c=white",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)])  # fmt: skip
    return path


def _gray(
    image: Image.Image, box: tuple[int, int, int, int]
) -> "np.ndarray[tuple[int, ...], np.dtype[np.float64]]":
    return np.asarray(image.convert("L").crop(box), dtype=np.float64)


@pytest.mark.parametrize(
    ("layout", "regions"),
    [
        # Layout C: the facecam panel (above the seam at y=669) and the gameplay panel (below it), clear of
        # the progress bar that runs along the seam.
        pytest.param(
            LayoutA(Box(1440, 720, 480, 360), cam_top=True), [(0, 0, 1080, 640), (0, 700, 1080, 1480)], id="C"
        ),
        # Layout B: the 4:5 gameplay from y=285, above the progress bar at y=1500.
        pytest.param(LayoutB(), [(0, 300, 1080, 1480)], id="B"),
    ],
)
def test_framing_is_static_for_the_whole_clip(
    layout: Layout,
    regions: list[tuple[int, int, int, int]],
    ffmpeg_tools: tuple[str, str],
    static_source: Path,
    params_factory: Callable[..., FactoryParams],
    tmp_path: Path,
) -> None:
    """Render v2 (D119, M19 R0): the frames at 0.5 s, 3 s and 5.5 s of a still source are the same picture,
    pixel-aligned within 1 px. Before R0 the gameplay grew to 108% over the last 40% of the clip."""
    ffmpeg = ffmpeg_tools[0]
    params = params_factory(watermark="", end_card_s=0)
    ass_path = tmp_path / "empty.ass"
    ass_path.write_text(build_ass([], layout=layout.kind, params=params, duration_s=6.0), encoding="utf-8")
    output = tmp_path / "static.mp4"
    spec = RenderSpec(
        source=SourceInfo(static_source, 1920, 1080),
        window=ClipWindow(0.0, 6.0),
        layout=layout,
        ass_path=ass_path,
        output_path=output,
        series_no=1,
        params=params,
        mode="express",
        scaler="lanczos",
    )
    run(build_render_command(spec, ffmpeg=ffmpeg))
    frames = [frame_at(ffmpeg, output, t, tmp_path / f"f{t}.png") for t in (0.5, 3.0, 5.5)]
    for box in regions:
        first = _gray(frames[0], box)
        assert first.std() > 20, "the region has no detail to compare"
        # Sensitivity: the same picture shifted by 2 px differs clearly, so a 1 px drift would show.
        shifted = np.abs(first[:, 2:] - first[:, :-2]).mean()
        assert shifted > 8, f"pattern too flat to detect a shift ({shifted:.1f})"
        for other in frames[1:]:
            diff = np.abs(_gray(other, box) - first).mean()
            assert diff < min(2.0, shifted / 4), f"framing changed in {box}: mean diff {diff:.2f}"


@pytest.fixture(scope="module")
def boxes_source(ffmpeg_tools: tuple[str, str], tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 3-second 1920x1080 source: blue gameplay, a red facecam at (1440, 720, 480, 360) and a green
    chat box at (0, 0, 400, 400)."""
    path = tmp_path_factory.mktemp("boxes source") / "boxes.mp4"
    video = (
        "color=c=0x0000FF:s=1920x1080:r=30:d=3,"
        "drawbox=x=1440:y=720:w=480:h=360:color=0xFF0000:t=fill,"
        "drawbox=x=0:y=0:w=400:h=400:color=0x00FF00:t=fill"
    )
    run([ffmpeg_tools[0], "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)])  # fmt: skip
    return path


def _share(region: "np.ndarray[tuple[int, ...], np.dtype[np.uint8]]", rgb: tuple[int, int, int]) -> float:
    """Share of the pixels of an (h, w, 3) region close to a colour."""
    close = np.all(np.abs(region.astype(int) - np.array(rgb)) <= 60, axis=2)
    return float(close.mean())


@pytest.mark.parametrize("kind", ["G", "G0"])
def test_render_v2_frames_have_the_spec_sizes_and_avoid_cam_and_chat(
    kind: str,
    ffmpeg_tools: tuple[str, str],
    boxes_source: Path,
    params_factory: Callable[..., FactoryParams],
    tmp_path: Path,
) -> None:
    """Render v2 R1: G puts the cam panel (1080x720) over 1080x1200 of gameplay that slid clear of the cam
    and the chat; G0 puts 1080x1350 of gameplay at y=285 over a dark blur. No seam, no progress bar."""
    from sixstars.factory.layout import Box as LBox
    from sixstars.factory.layout import LayoutG, LayoutG0, LayoutV2Rules

    ffmpeg, ffprobe = ffmpeg_tools
    params = params_factory(watermark="", end_card_s=0)
    chat = LBox(0, 0, 400, 400)
    layout = LayoutG(LBox(1440, 720, 480, 360), overlay_boxes=(chat,)) if kind == "G" else LayoutG0((chat,))
    ass_path = tmp_path / "empty.ass"
    ass_path.write_text(build_ass([], layout=layout.kind, params=params, duration_s=3.0), encoding="utf-8")
    output = tmp_path / f"{kind}.mp4"
    spec = RenderSpec(
        source=SourceInfo(boxes_source, 1920, 1080),
        window=ClipWindow(0.0, 3.0),
        layout=layout,
        ass_path=ass_path,
        output_path=output,
        series_no=1,
        params=params,
        mode="express",
        scaler="lanczos",
        layout_v2=LayoutV2Rules(enabled=True, scaler="lanczos"),
    )
    run(build_render_command(spec, ffmpeg=ffmpeg))
    assert_streams(probe(ffprobe, output), expected_duration=3.0)
    image = frame_at(ffmpeg, output, 1.5, tmp_path / f"{kind}.png")
    blue, red, green = (0, 0, 255), (255, 0, 0), (0, 255, 0)
    if kind == "G":
        assert _share(pixels(image, (0, 0, 1080, 716)), red) > 0.95, "cam panel is the facecam"
        game = pixels(image, (0, 724, 1080, 1920))
        assert _share(game, blue) > 0.97, "gameplay panel holds gameplay only"
        assert _share(game, red) < 0.001 and _share(game, green) < 0.001, "cam or chat leaked in"
    else:
        game = pixels(image, (0, 289, 1080, 1631))
        assert _share(game, blue) > 0.97 and _share(game, green) < 0.001
        top = pixels(image, (0, 0, 1080, 270))
        assert top.max() < 230 and _share(top, blue) < 0.5, "blurred, darkened band above"


def test_render_v2_cam_panel_is_framed_on_the_face_with_a_dark_fill(
    ffmpeg_tools: tuple[str, str], params_factory: Callable[..., FactoryParams], tmp_path: Path
) -> None:
    """Render v2 R2: a small square cam (Jerma's box) framed around the face: the sharp cam in the middle of
    the 1080x720 panel, a darker blurred copy of the cam on both sides, nothing from outside the cam box."""
    from sixstars.factory.layout import Box as LBox
    from sixstars.factory.layout import LayoutG, LayoutV2Rules, cam_window

    ffmpeg, ffprobe = ffmpeg_tools
    source = tmp_path / "jerma.mp4"
    video = "color=c=0x0000FF:s=1920x1080:r=30:d=2,drawbox=x=0:y=651:w=248:h=246:color=0xFF0000:t=fill"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi", "-i", video,
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2", "-c:v", "libx264",
         "-preset", "ultrafast", "-crf", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(source)])  # fmt: skip
    rules = LayoutV2Rules(enabled=True, scaler="lanczos")
    cam, face = LBox(0, 651, 248, 246), LBox(80, 700, 80, 100)
    framing = cam_window(face, cam, 1080, 720, rules, 1920, 1080)
    assert framing.fill
    params = params_factory(watermark="", end_card_s=0)
    ass_path = tmp_path / "empty.ass"
    ass_path.write_text(build_ass([], layout="G", params=params, duration_s=2.0), encoding="utf-8")
    output = tmp_path / "g_face.mp4"
    spec = RenderSpec(
        source=SourceInfo(source, 1920, 1080),
        window=ClipWindow(0.0, 2.0),
        layout=LayoutG(cam, face_box=face),
        ass_path=ass_path,
        output_path=output,
        series_no=1,
        params=params,
        mode="express",
        scaler="lanczos",
        layout_v2=rules,
    )
    run(build_render_command(spec, ffmpeg=ffmpeg))
    assert_streams(probe(ffprobe, output), expected_duration=2.0)
    image = frame_at(ffmpeg, output, 1.0, tmp_path / "g_face.png")
    p = framing.placed
    sharp = pixels(image, (p.x + 20, p.y + 20, p.x + p.w - 20, p.y + p.h - 20))
    assert _share(sharp, (255, 0, 0)) > 0.95, "the sharp cam fills its place"
    panel = pixels(image, (0, 0, 1080, 716))
    assert _share(panel, (0, 0, 255)) < 0.001, "no gameplay pixels in the cam panel"
    if p.x > 30:
        side = pixels(image, (4, 100, p.x - 10, 600))
        assert side[..., 0].mean() < 230 and side[..., 0].mean() > 80, "a darkened red fill beside the cam"

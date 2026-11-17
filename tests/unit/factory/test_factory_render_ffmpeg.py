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
    assert close_to(early.getpixel((20, 1149)), ACCENT_RGB), "seam line missing"
    assert close_to(early.getpixel((540, 325)), (255, 255, 255)), (
        "hook box missing (moved below the LIVE badge)"
    )
    assert close_to(early.getpixel((60, 250)), (232, 33, 43), 50), "LIVE badge missing"
    assert close_to(early.getpixel((50, 1503)), ACCENT_RGB), "progress bar missing at 10%"
    assert not close_to(early.getpixel((400, 1503)), ACCENT_RGB), "progress bar too long at 10%"
    middle = frame_at(ffmpeg, output, 5.0, tmp_path / "a_5s.png")
    assert close_to(middle.getpixel((500, 1503)), ACCENT_RGB) and not close_to(
        middle.getpixel((700, 1503)), ACCENT_RGB
    )
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

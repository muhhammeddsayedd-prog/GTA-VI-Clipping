"""Cover-ready video with real ffmpeg: the taller facecam, the face crop and the opening lift change the
picture only. Length and audio are identical to the plain render (docs/29a: C1 never changes clip length, cut
points or audio). Skipped when ffmpeg is not installed."""

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from sixstars.factory import coverready
from sixstars.factory.layout import Box, FaceFraming, GameLift, LayoutA
from sixstars.factory.params import FactoryParams, FontFiles
from sixstars.factory.render import RenderSpec, SourceInfo, build_render_command
from sixstars.factory.subtitles import build_ass
from sixstars.factory.window import ClipWindow

pytestmark = pytest.mark.ffmpeg

LAYOUT_C = LayoutA(facecam_box=Box(1440, 720, 480, 360), cam_top=True)


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")[-3000:]
    return result


def _render(spec: RenderSpec, ffmpeg: str) -> Path:
    Path(spec.ass_path).write_text(
        build_ass([], layout="C", params=spec.params, duration_s=spec.clip_duration_s), encoding="utf-8"
    )
    _run(build_render_command(spec, ffmpeg=ffmpeg))
    return Path(spec.output_path)


def _duration(ffprobe: str, path: Path) -> float:
    out = _run([ffprobe, "-v", "error", "-show_format", "-of", "json", str(path)]).stdout
    return float(json.loads(out)["format"]["duration"])


def _audio_hash(ffmpeg: str, path: Path) -> str:
    pcm = _run([ffmpeg, "-v", "error", "-i", str(path), "-vn", "-f", "s16le", "-ac", "1", "-"]).stdout
    return hashlib.sha256(pcm).hexdigest()


def test_cover_ready_render_keeps_length_and_audio(
    ffmpeg_tools: tuple[str, str],
    test_font: tuple[str, Path],
    synthetic_source: Path,
    params_factory: Callable[..., FactoryParams],
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe = ffmpeg_tools
    font_name, font_file = test_font
    params = params_factory(font=font_name, watermark="@6starsclipping")
    plain = RenderSpec(
        source=SourceInfo(synthetic_source, 1920, 1080),
        window=ClipWindow(1.0, 7.0),
        layout=LAYOUT_C,
        ass_path=tmp_path / "plain.ass",
        output_path=tmp_path / "plain.mp4",
        series_no=1,
        params=params,
        mode="express",
        fonts=FontFiles(font_file=font_file),
    )
    ready = replace(
        plain,
        ass_path=tmp_path / "ready.ass",
        output_path=tmp_path / "ready.mp4",
        facecam_ref_h=768,
        framing=FaceFraming(Box(1600, 800, 90, 100), 0.4, 120),
        lift=GameLift(1.3, 1.5, 0.5),
    )
    plain_file, ready_file = _render(plain, ffmpeg), _render(ready, ffmpeg)
    assert _duration(ffprobe, ready_file) == pytest.approx(_duration(ffprobe, plain_file), abs=0.02)
    assert _audio_hash(ffmpeg, ready_file) == _audio_hash(ffmpeg, plain_file)
    # The lift brightens the gameplay at the start and is gone after 1.5 s.
    early_plain = coverready.mean_luma(plain_file, 0.0, 0.5, Box(0, 1000, 1080, 600), ffmpeg=ffmpeg)
    early_ready = coverready.mean_luma(ready_file, 0.0, 0.5, Box(0, 1000, 1080, 600), ffmpeg=ffmpeg)
    assert early_plain is not None and early_ready is not None and early_ready > early_plain + 3


def test_mean_luma_reads_the_source(ffmpeg_tools: tuple[str, str], synthetic_source: Path) -> None:
    luma = coverready.mean_luma(synthetic_source, 0.0, 1.0, Box(0, 0, 960, 540), ffmpeg=ffmpeg_tools[0])
    assert luma is not None and 10 < luma < 245

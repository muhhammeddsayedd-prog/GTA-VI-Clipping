"""ffmpeg log parsing, frame positions and the P5 call of the gate; real ffmpeg runs on synthetic video."""

import shutil
import subprocess
from pathlib import Path

import pytest
from gate_helpers import clean_visual, make_settings

from sixstars import settings, usage
from sixstars.ai.schemas import VisualVerdict
from sixstars.clients.fakes import FakeAnthropicClient
from sixstars.gate import visual

BLACK_LOG = """
[blackdetect @ 000001] black_start:3 black_end:5.5 black_duration:2.5
frame=  300 fps=0.0 q=-0.0 size=N/A time=00:00:10.00 bitrate=N/A speed= 200x
[blackdetect @ 000001] black_start:8 black_end:10 black_duration:2
"""
FREEZE_LOG = """
[freezedetect @ 0000023] lavfi.freezedetect.freeze_start: 4.004
[freezedetect @ 0000023] lavfi.freezedetect.freeze_duration: 3.003
[freezedetect @ 0000023] lavfi.freezedetect.freeze_end: 7.007
[freezedetect @ 0000023] lavfi.freezedetect.freeze_start: 26.5
"""


def test_parse_blackdetect() -> None:
    assert visual.parse_blackdetect(BLACK_LOG) == [2.5, 2.0]
    assert visual.parse_blackdetect("nothing here") == []


def test_parse_freezedetect_counts_an_open_freeze_to_the_end() -> None:
    assert visual.parse_freezedetect(FREEZE_LOG, 30.0) == [3.003, 3.5]
    assert visual.parse_freezedetect("", 30.0) == []


def test_parse_freezedetect_keeps_two_detectors_apart() -> None:
    log = """
[Parsed_freezedetect_2 @ 0a] lavfi.freezedetect.freeze_start: 1
[Parsed_freezedetect_5 @ 0b] lavfi.freezedetect.freeze_start: 2
[Parsed_freezedetect_2 @ 0a] lavfi.freezedetect.freeze_duration: 4
[Parsed_freezedetect_2 @ 0a] lavfi.freezedetect.freeze_end: 5
"""
    assert sorted(visual.parse_freezedetect(log, 10.0)) == [4.0, 8.0]


def test_the_filter_uses_the_configured_limits() -> None:
    settings = make_settings(
        max_black_s=1.5, max_freeze_s=3, freeze_noise=0.01, band_freeze_noise=0.002, band_black_pixel=0.05
    )
    graph = visual.detect_filter(settings)
    assert "[a]blackdetect=d=1.5:pix_th=0.1,freezedetect=n=0.01:d=3[whole]" in graph
    band = "[b]crop=iw:ih*0.2708:0:ih*0.2500,blackdetect=d=1.5:pix_th=0.05,freezedetect=n=0.002:d=3[band]"
    assert band in graph


def test_frame_times() -> None:
    assert visual.frame_times(40.0) == [4.0, 14.0, 24.0, 34.0]


def test_findings_round_trip_through_json() -> None:
    findings = clean_visual(other_watermark=True, notes="@otherchannel")
    assert visual.findings_from_json(visual.findings_as_json(findings)) == findings
    assert visual.findings_from_json(None) is None
    assert visual.findings_from_json({"other_watermark": True}) is None
    assert visual.findings_from_json({**visual.findings_as_json(findings), "platform_logos": "no"}) is None


def test_check_frames_asks_p5_with_the_clip_id(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {"ai.model_fast": "claude-haiku-5-5", "ai.daily_budget_usd": 25, "factory.watermark": "@x"}
    values["env.target_game"] = "Grand Theft Auto V"
    monkeypatch.setattr(settings, "_all", lambda: values)
    monkeypatch.setattr(usage, "spent_today", lambda provider: 0.0)
    verdict = VisualVerdict(
        other_watermark=False,
        platform_logos=True,
        menu_or_loading=False,
        pause_or_brb_screen=False,
        empty_facecam=False,
        readable_subtitles=True,
        notes="TikTok logo",
    )
    client = FakeAnthropicClient({"p5_visual_check": verdict})
    findings = visual.check_frames([_jpeg()] * 4, lane="express", clip_id="clip-9", client=client)
    assert findings == clean_visual(platform_logos=True, notes="TikTok logo")
    arguments = client.calls_to("complete_json")[0]
    assert arguments["images"] == 4 and arguments["ref_id"] == "clip-9"


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (108, 192), (40, 90, 160)).save(buffer, format="JPEG")
    return buffer.getvalue()


# ── Real ffmpeg ──────────────────────────────────────────────


def _make(tmp_path: Path, name: str, video_filter: str, seconds: float = 8.0, audio: bool = True) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    path = tmp_path / name
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi"]
    command += ["-i", f"testsrc2=s=270x480:r=15:d={seconds}"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    command += ["-vf", video_filter, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    command += ["-c:a", "aac", "-shortest", str(path)] if audio else [str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    return path


@pytest.mark.ffmpeg
def test_a_moving_picture_has_no_black_or_frozen_stretch(tmp_path: Path) -> None:
    path = _make(tmp_path, "moving.mp4", "null")
    facts = visual.measure_file(path, 6.5, make_settings())
    assert facts is not None
    assert (facts.width, facts.height, facts.has_video, facts.has_audio) == (270, 480, True, True)
    assert facts.duration_s == pytest.approx(8.0, abs=0.2)
    assert facts.size_bytes == path.stat().st_size
    assert facts.longest_black_s == 0.0 and facts.longest_freeze_s == 0.0


@pytest.mark.ffmpeg
def test_black_frames_are_found_even_under_moving_overlays(tmp_path: Path) -> None:
    """Three black seconds of gameplay under a white box that keeps blinking, as subtitles do."""
    black = "drawbox=enable='between(t,2,5)':x=0:y=0:w=iw:h=ih:color=black:t=fill"
    overlay = "drawbox=enable='lt(mod(t,0.4),0.2)':x=20:y=400:w=120:h=40:color=white:t=fill"
    path = _make(tmp_path, "black.mp4", f"{black},{overlay}")
    facts = visual.measure_file(path, 6.5, make_settings())
    assert facts is not None and facts.longest_black_s == pytest.approx(3.0, abs=0.25)
    under = visual.measure_file(path, 6.5, make_settings(max_black_s=4.0))
    assert under is not None and under.longest_black_s == 0.0


@pytest.mark.ffmpeg
def test_a_frozen_picture_is_found_even_under_moving_overlays(tmp_path: Path) -> None:
    frozen = "loop=loop=60:size=1:start=30"  # frame 30 repeated 60 times: four frozen seconds from 2 s
    overlay = "drawbox=enable='lt(mod(t,0.4),0.2)':x=20:y=400:w=120:h=40:color=white:t=fill"
    path = _make(tmp_path, "frozen.mp4", f"{frozen},{overlay}", seconds=10.0)
    facts = visual.measure_file(path, 9.0, make_settings())
    assert facts is not None and facts.longest_freeze_s is not None
    assert 3.5 < facts.longest_freeze_s < 5.0
    assert facts.longest_black_s == 0.0


@pytest.mark.ffmpeg
def test_a_freeze_in_the_end_card_is_not_counted(tmp_path: Path) -> None:
    path = _make(tmp_path, "endcard.mp4", "loop=loop=60:size=1:start=75", seconds=9.0)
    facts = visual.measure_file(path, 5.0, make_settings())
    assert facts is not None and facts.longest_freeze_s == 0.0


@pytest.mark.ffmpeg
def test_a_file_without_audio_and_a_missing_file(tmp_path: Path) -> None:
    silent = visual.measure_file(_make(tmp_path, "silent.mp4", "null", audio=False), 6.5, make_settings())
    assert silent is not None and silent.has_audio is False
    assert visual.measure_file(tmp_path / "nothing.mp4", 6.5, make_settings()) is None
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert visual.measure_file(empty, 6.5, make_settings()) is None


@pytest.mark.ffmpeg
def test_sample_frames_returns_four_jpegs(tmp_path: Path) -> None:
    frames = visual.sample_frames(_make(tmp_path, "frames.mp4", "null"), 6.5)
    assert len(frames) == 4 and all(frame[:2] == b"\xff\xd8" for frame in frames)
    assert len(set(frames)) == 4

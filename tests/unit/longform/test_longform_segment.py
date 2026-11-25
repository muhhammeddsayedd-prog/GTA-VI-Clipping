"""M20 L3: the 16:9 overlay (ASS) and the segment ffmpeg commands; one real render when ffmpeg exists."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sixstars.factory.bleep import ProfanityMatcher
from sixstars.factory.lines import Word
from sixstars.longform.config import RenderSpec
from sixstars.longform.segment import SegmentJob, measure_command, render_command, render_segment, video_chain
from sixstars.longform.subs16x9 import OverlayStyle, build_segment_ass, caption_chunks, creator_label, wrap

STYLE = OverlayStyle()
WORDS = [Word("no", 0.0, 0.3), Word("way", 0.35, 0.6), Word("damn", 0.65, 0.9), Word("bro", 2.0, 2.3)]


def test_captions_split_at_pauses_and_mask_profanity() -> None:
    chunks = caption_chunks(WORDS, STYLE, ProfanityMatcher(frozenset({"damn"})))
    assert [c[2] for c in chunks] == ["no way d***", "bro"]


def test_wrap_breaks_long_captions() -> None:
    assert "\\N" in wrap("one two three four five six seven eight nine ten eleven twelve", 20)


def test_ass_has_canvas_playres_badge_credit_and_mark() -> None:
    ass = build_segment_ass(
        words=WORDS,
        duration_s=20.0,
        position=7,
        creator_label="twitch.tv/someone",
        style=STYLE,
        width=1920,
        height=1080,
    )
    assert "PlayResX: 1920" in ass and "PlayResY: 1080" in ass
    assert "#7" in ass and "twitch.tv/someone" in ass and STYLE.mark_text in ass
    badge = next(line for line in ass.splitlines() if ",Badge," in line)
    assert badge.startswith("Dialogue: 1,0:00:00.00,0:00:02.00")


def test_creator_label() -> None:
    assert creator_label("twitch", "@Streamer") == "twitch.tv/Streamer"
    assert creator_label("youtube", "chan") == "youtube.com/@chan"
    assert creator_label("twitch", "") == ""


def job(tmp: Path, src: Path) -> SegmentJob:
    return SegmentJob(
        source=src,
        start_s=1.0,
        duration_s=3.0,
        ass_path=tmp / "s.ass",
        fonts_dir=None,
        out_path=tmp / "out.mp4",
        render=RenderSpec(),
        target_lufs=-14.0,
    )


def test_video_chain_pads_and_never_crops(tmp_path: Path) -> None:
    chain = video_chain(job(tmp_path, tmp_path / "in.mp4"))
    assert "force_original_aspect_ratio=decrease" in chain and "pad=1920:1080" in chain
    assert "crop" not in chain and "ass=" in chain


def test_commands_seek_and_map_both_streams(tmp_path: Path) -> None:
    j = job(tmp_path, tmp_path / "in.mp4")
    measure = measure_command(j)
    assert measure[-3:] == ["-f", "null", "-"] and "-ss" in measure
    cmd = render_command(j, None)
    assert cmd.count("-map") == 2 and "libx264" in cmd and cmd[-1].endswith("out.mp4")
    assert cmd[cmd.index("-g") + 1] == "60"


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_real_render_is_1920x1080_30fps_aac(tmp_path: Path) -> None:
    src = tmp_path / "src in space.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1280x720:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(src),
        ],
        check=True,
    )
    j = job(tmp_path, src)
    j.ass_path.write_text(
        build_segment_ass(words=WORDS, duration_s=3.0, position=3, creator_label="twitch.tv/x", style=STYLE),
        encoding="utf-8",
    )
    out = render_segment(j, timeout_s=120)
    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(out)],
            check=True,
            capture_output=True,
        ).stdout
    )
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (1920, 1080, "30/1")
    assert audio["codec_name"] == "aac" and audio["sample_rate"] == "48000"
    assert abs(float(probe["format"]["duration"]) - 3.0) < 0.2
    assert not out.with_name(out.stem + ".part.mp4").exists()

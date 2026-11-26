"""M20 L4: chapters, packaging text and the real assembly (cold open + segments + end card, concat)."""

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from sixstars.factory.lines import Word
from sixstars.longform import chapters as ch
from sixstars.longform.assemble import (
    cold_open_command,
    concat_command,
    concat_list,
    end_card_command,
    thumbnail_command,
)
from sixstars.longform.config import RenderSpec
from sixstars.longform.segment import SegmentJob, render_segment
from sixstars.longform.subs16x9 import OverlayStyle, build_segment_ass, build_title_ass

DAY = date(2026, 11, 25)
MOMENTS = [
    ch.Moment(3, "Alpha", "jumps the bridge", "https://twitch.tv/alpha", 30.0),
    ch.Moment(2, "Bravo", "police chase ends in a pool", "https://twitch.tv/bravo", 25.0),
    ch.Moment(1, "Charlie", "the heist goes wrong", "https://twitch.tv/charlie", 40.0),
]


def test_chapters_start_at_zero_and_include_the_cold_open() -> None:
    chs = ch.chapters(MOMENTS, cold_open_s=5.0)
    assert [c.start_s for c in chs] == [0.0, 35.0, 60.0]
    assert chs[0].label.startswith("#3 Alpha")
    assert ch.valid(chs, total_s=110.0)


def test_chapters_invalid_when_too_few_or_too_short() -> None:
    assert not ch.valid(ch.chapters(MOMENTS[:2], 5.0), 70.0)
    short = [
        ch.Moment(3, "a", "t", "", 5.0),
        ch.Moment(2, "b", "t", "", 5.0),
        ch.Moment(1, "c", "t", "", 5.0),
    ]
    assert not ch.valid(ch.chapters(short, 0.0), 15.0)


def test_stamp_formats() -> None:
    assert ch.stamp(0) == "00:00" and ch.stamp(65.9) == "01:05" and ch.stamp(3725) == "1:02:05"


def test_title_and_description() -> None:
    assert ch.title(30, DAY) == "TOP 30 Funniest GTA 6 Stream Moments (Nov 25)"
    text = ch.description(MOMENTS, ch.chapters(MOMENTS, 5.0), DAY, "Follow @6starsclips")
    assert (
        "00:00 #3 Alpha" in text
        and "https://twitch.tv/bravo" in text
        and text.endswith("Follow @6starsclips")
    )
    assert len(text) <= ch.DESCRIPTION_MAX


def test_concat_list_escapes_quotes(tmp_path: Path) -> None:
    listing = concat_list([tmp_path / "a b.mp4", tmp_path / "it's.mp4"])
    assert "file '" in listing and "it'\\''s.mp4" in listing


def test_commands_shape(tmp_path: Path) -> None:
    r = RenderSpec()
    cold = cold_open_command(tmp_path / "s.mp4", 5.0, tmp_path / "c.ass", None, r, tmp_path / "c.mp4")
    assert cold[cold.index("-sseof") + 1] == "-5.000"
    end = end_card_command(10.0, tmp_path / "e.ass", None, r, tmp_path / "e.mp4")
    assert any("anullsrc" in a for a in end) and any("color=c=" in a for a in end)
    joined = concat_command(tmp_path / "l.txt", tmp_path / "o.mp4")
    assert joined[joined.index("-c") + 1] == "copy"
    thumb = thumbnail_command(tmp_path / "s.mp4", 3.0, tmp_path / "t.ass", None, tmp_path / "t.jpg")
    assert thumb[thumb.index("-frames:v") + 1] == "1"


def _probe(path: Path) -> dict[str, object]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
    ).stdout
    result: dict[str, object] = json.loads(out)
    return result


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_real_episode_joins_without_reencoding(tmp_path: Path) -> None:
    src = tmp_path / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=1280x720:rate=30:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=12", "-shortest", "-c:v", "libx264", "-c:a", "aac",
         str(src)],
        check=True,
    )  # fmt: skip
    render, style = RenderSpec(preset="ultrafast"), OverlayStyle()
    segments = []
    for pos in (3, 2, 1):
        ass = tmp_path / f"s{pos}.ass"
        ass.write_text(
            build_segment_ass(words=[Word("hey", 0.1, 0.4)], duration_s=3.0, position=pos, creator_label="x",
                              style=style),
            encoding="utf-8",
        )  # fmt: skip
        out = tmp_path / f"seg{pos}.mp4"
        render_segment(
            SegmentJob(source=src, start_s=float(pos), duration_s=3.0, ass_path=ass, fonts_dir=None,
                       out_path=out, render=render, target_lufs=-14.0),
            timeout_s=120,
        )  # fmt: skip
        segments.append(out)
    title_ass = tmp_path / "title.ass"
    title_ass.write_text(
        build_title_ass("stay", duration_s=2.0, style=style, width=1920, height=1080), "utf-8"
    )
    cold, end = tmp_path / "cold.mp4", tmp_path / "end.mp4"
    subprocess.run(
        cold_open_command(segments[-1], 2.0, title_ass, None, render, cold), check=True, capture_output=True
    )
    subprocess.run(end_card_command(2.0, title_ass, None, render, end), check=True, capture_output=True)
    listing = tmp_path / "parts.txt"
    listing.write_text(concat_list([cold, *segments, end]), encoding="utf-8")
    final = tmp_path / "episode.mp4"
    subprocess.run(concat_command(listing, final), check=True, capture_output=True)
    info = _probe(final)
    fmt = info["format"]
    assert isinstance(fmt, dict)
    assert abs(float(fmt["duration"]) - 13.0) < 0.6  # 2 + 3*3 + 2
    streams = info["streams"]
    assert isinstance(streams, list)
    video = next(s for s in streams if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1920, 1080)
    thumb = tmp_path / "thumb.jpg"
    subprocess.run(
        thumbnail_command(segments[-1], 1.5, title_ass, None, thumb), check=True, capture_output=True
    )
    tinfo = _probe(thumb)
    tstreams = tinfo["streams"]
    assert isinstance(tstreams, list)
    assert (tstreams[0]["width"], tstreams[0]["height"]) == (1280, 720)

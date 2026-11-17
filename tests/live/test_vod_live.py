"""The VOD lane's network calls against the real services: a caption track, a short window download from
YouTube and from Twitch (no key needed), and a Twitch chat replay through the real TwitchDownloaderCLI.

Skipped unless `RUN_LIVE_TESTS=1`. No database, no AI call, nothing is uploaded. The video ids are public
GTA 5 streams; set `VOD_LIVE_YOUTUBE_ID` / `VOD_LIVE_TWITCH_VOD` when they are gone, and
`TWITCH_DOWNLOADER_CLI` to the binary for the chat test. Downloads are cut to a few seconds at 360p.

    RUN_LIVE_TESTS=1 uv run pytest tests/live/test_vod_live.py -v
"""

import os
import shutil
from pathlib import Path

import pytest

from sixstars.chat import twitch_replay
from sixstars.spikes.config import SpikeConfig
from sixstars.spikes.detector import analyse_replay
from sixstars.vod import captions, sections
from sixstars.vod.sections import Section
from sixstars.vod.ytdlp import Access

YOUTUBE_ID = os.environ.get("VOD_LIVE_YOUTUBE_ID", "1aC9ka7Kud4")
TWITCH_VOD = os.environ.get("VOD_LIVE_TWITCH_VOD", "2900689447")
SEED_SQL = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0002_seed_settings.sql"


@pytest.fixture
def ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")


def test_youtube_caption_track_has_word_times(tmp_path: Path) -> None:
    track = captions.fetch(
        f"https://www.youtube.com/watch?v={YOUTUBE_ID}",
        tmp_path,
        access=Access(),
        langs=[".*-orig", "en"],
        timeout_s=180,
    )
    assert track is not None, "this stream had automatic captions when the test was written"
    assert len(track.words) > 1000 and track.language == "en"
    assert track.end_s > 3600
    chunks = captions.chunk_words(track.words, 30)
    assert len(chunks) > 100 and all(chunk.text for chunk in chunks)


def test_youtube_window_download_is_only_the_window(tmp_path: Path, ffmpeg: None) -> None:
    done = sections.download_section(
        f"https://www.youtube.com/watch?v={YOUTUBE_ID}",
        Section(4500.0, 4512.0),
        tmp_path,
        "window",
        access=Access(),
        max_height=360,
        timeout_s=300,
    )
    assert done.path.is_file() and done.height <= 360
    assert 11.0 <= done.duration_s <= 13.5
    assert done.path.stat().st_size < 20 * 1024 * 1024


def test_twitch_window_download_needs_no_key(tmp_path: Path, ffmpeg: None) -> None:
    done = sections.download_section(
        f"https://www.twitch.tv/videos/{TWITCH_VOD}",
        Section(2820.0, 2832.0),
        tmp_path,
        "window",
        access=Access(),
        max_height=360,
        timeout_s=300,
    )
    assert done.path.is_file() and done.height <= 360
    assert 10.0 <= done.duration_s <= 16.0


def test_twitch_chat_replay_with_the_real_cli_gives_spikes(tmp_path: Path) -> None:
    cli = os.environ.get("TWITCH_DOWNLOADER_CLI", "")
    if not cli or not Path(cli).is_file():
        pytest.skip("TWITCH_DOWNLOADER_CLI does not name the binary")
    import json
    import re

    replay = twitch_replay.download(TWITCH_VOD, tmp_path / "chat.json", cli_path=Path(cli))
    assert replay.message_count > 1000 and replay.duration_s and replay.duration_s > 3600
    text = SEED_SQL.read_text(encoding="utf-8")
    values = {
        key: json.loads(raw.replace("''", "'"))
        for key, raw in re.findall(r"^\s*\('([\w.]+)', '(.*)'::jsonb, '", text, re.MULTILINE)
    }
    scan = analyse_replay(replay.messages, config=SpikeConfig.from_mapping(values), end_guard_s=300)
    assert scan.spikes, "a busy 12-hour stream has chat spikes"
    assert all(0 <= spike.pad_start < spike.pad_end <= replay.duration_s + 60 for spike in scan.spikes)

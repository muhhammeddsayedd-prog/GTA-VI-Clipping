"""Helpers for the gate and pre-check integration tests: rendered-looking test files made with ffmpeg and
builders for creators, clips in `gating` and their `clip.rendered` events."""

import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from posting_support import clean_posting_tables
from psycopg.types.json import Jsonb

from sixstars import db, events, settings
from sixstars.clients.fakes import (
    FakeAnthropicClient,
    FakeUploadPostClient,
    FakeYouTubeClient,
)
from sixstars.gate import gate
from sixstars.storage import FakeClipStorage

CLIP_S = 21.0
END_CARD_S = 1.5
NOW = datetime(2026, 11, 20, 12, 3, tzinfo=UTC)

P5_CLEAN = {
    "other_watermark": False,
    "platform_logos": False,
    "menu_or_loading": False,
    "pause_or_brb_screen": False,
    "empty_facecam": False,
    "readable_subtitles": True,
    "notes": "",
}
SAFE = dict.fromkeys(
    (
        "politics",
        "gambling",
        "sponsored",
        "real_violence",
        "sexual",
        "personal_info",
        "hate",
        "copyrighted_media",
    ),
    False,
)
WORDS = [
    "no",
    "way",
    "he",
    "actually",
    "landed",
    "that",
    "jump",
    "over",
    "the",
    "whole",
    "bridge",
    "that",
    "was",
    "insane",
    "chat",
    "look",
    "at",
    "this",
]


def _ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    return ffmpeg


def make_video(path: Path, video_filter: str = "null", seconds: float = CLIP_S + END_CARD_S) -> Path:
    """A 1080x1920 test pattern with a tone, shaped like a rendered clip (clip part plus end card)."""
    command = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi"]
    command += ["-i", f"testsrc2=s=1080x1920:r=10:d={seconds}", "-f", "lavfi"]
    command += ["-i", f"sine=frequency=440:duration={seconds}", "-vf", video_filter]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    subprocess.run([*command, str(path)], check=True, capture_output=True, timeout=180)
    return path


@pytest.fixture(scope="session")
def good_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_video(tmp_path_factory.mktemp("gate") / "good.mp4")


@pytest.fixture(scope="session")
def black_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Six seconds of black picture from 5 s (the limit is 4 s since migration 0017)."""
    black = "drawbox=enable='between(t,5,11)':x=0:y=0:w=iw:h=ih:color=black:t=fill"
    return make_video(tmp_path_factory.mktemp("gate") / "black.mp4", black)


@pytest.fixture(scope="session")
def frozen_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """About seven frozen seconds from 5 s (the limit is 4 s since migration 0017)."""
    return make_video(tmp_path_factory.mktemp("gate") / "frozen.mp4", "loop=loop=70:size=1:start=50")


def add_creator(tier: str = "preferred", handle: str = "SomeStreamer") -> str:
    row = db.fetch_one(
        "insert into creators (platform, channel_id, handle, tier) values ('youtube', %s, %s, %s) "
        "returning id::text as id",
        (f"chan-{handle}", handle, tier),
    )
    assert row is not None
    return str(row["id"])


def add_clip(creator_id: str, video: Path | None, **overrides: Any) -> str:
    """A clip as the factory leaves it in `gating`. `overrides` are column values; `music_frames` adds the
    `clip.rendered` event with those per-second flags."""
    frames = overrides.pop("music_frames", None)
    step = CLIP_S / (len(WORDS) + 1)
    values: dict[str, Any] = {
        "creator_id": creator_id,
        "source_platform": "youtube",
        "source_id": "video-1",
        "source_url": "https://example.test/v",
        "source_type": "vod",
        "lane": "hourly",
        "start_s": 1000.0,
        "end_s": 1000.0 + CLIP_S,
        "duration_s": CLIP_S,
        "transcript": Jsonb(
            {
                "language": "en",
                "words": [
                    {"w": word, "s": round(i * step + 0.2, 3), "e": round(i * step + 0.6, 3)}
                    for i, word in enumerate(WORDS)
                ],
            }
        ),
        "hook": "He did NOT expect that",
        "title": "Streamer jumps the whole bridge",
        "caption": "Nobody expected this jump",
        "hashtags": ["gta", "stunt"],
        "topic": "streamer-jumps-whole-bridge",
        "spoiler": False,
        "safety_flags": Jsonb(SAFE),
        "score": 9,
        "music_ratio": 0.0,
        "profanity_count": 0,
        "local_path": None if video is None else str(video),
        "file_size_bytes": None if video is None else video.stat().st_size,
        "status": "gating",
    }
    values.update(overrides)
    values.setdefault("gate_profile", "express" if values["lane"] == "spike" else "hourly")
    for key in ("safety_flags", "transcript"):
        if isinstance(values[key], dict):
            values[key] = Jsonb(values[key])
    columns = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    row = db.fetch_one(
        f"insert into clips ({columns}) values ({marks}) returning id::text as id", tuple(values.values())
    )
    assert row is not None
    clip_id = str(row["id"])
    if frames is not None:
        events.emit(
            "clip.rendered",
            entity_type="clip",
            entity_id=clip_id,
            data={"music": {"ratio": values["music_ratio"], "frame_s": 1.0, "frames": list(frames)}},
        )
    return clip_id


def distinct(n: int) -> dict[str, Any]:
    """Column values that make a clip share no video, topic or words with the default clip."""
    words = [{"w": f"word{n}x{i}", "s": float(i), "e": i + 0.4} for i in range(12)]
    return {
        "source_id": f"video-d{n}",
        "topic": f"topic{n}-thing{n}-other{n}",
        "transcript": {"words": words},
    }


def add_spike(creator_id: str, when: datetime | None = None) -> str:
    row = db.fetch_one(
        "insert into spikes (mode, creator_id, spike_wall, tier, status) "
        "values ('live', %s, %s, 'S', 'clipped') returning id::text as id",
        (creator_id, when or datetime.now(UTC) - timedelta(minutes=2)),
    )
    assert row is not None
    return str(row["id"])


def clip(clip_id: str) -> dict[str, Any]:
    row = db.fetch_one("select * from clips where id = %s", (clip_id,))
    assert row is not None
    return row


def clip_events(clip_id: str, prefix: str = "") -> list[dict[str, Any]]:
    return db.fetch_all(
        "select type, level, message, data from events where entity_id = %s and type like %s order by id",
        (clip_id, prefix + "%"),
    )


def count(table: str, where: str = "true") -> int:
    row = db.fetch_one(f"select count(*) as n from {table} where {where}")
    assert row is not None
    return int(row["n"])


class Kit:
    """The fakes one gate run uses, and the `GateDeps` built from them."""

    def __init__(self, p5: Any = None, oauth: bool = False, score: Any = None) -> None:
        replies = {"p5_visual_check": p5 if p5 is not None else P5_CLEAN}
        if score is not None:
            replies["p9_visual_score"] = score
        self.ai = FakeAnthropicClient(replies)
        self.youtube = FakeYouTubeClient(oauth=oauth)
        self.storage = FakeClipStorage()
        self.upload_post = FakeUploadPostClient()
        self.deps = gate.GateDeps(
            ai_client=self.ai,
            youtube=self.youtube,
            storage=self.storage,
            upload_post=lambda: self.upload_post,
        )

    @property
    def p5_calls(self) -> int:
        return len(self.ai.calls_to("complete_json"))


@pytest.fixture
def tables() -> Iterator[None]:
    """Empty clip tables before and after; the gate's module-level collaborators are reset.

    The visual score (`ai.visual_score`, on since migration 0016) is switched off here: these tests are
    about the gate's rules and run on the stored score. `test_gate_visual_score.py` switches it on."""
    clean_posting_tables()
    gate.configure(None)
    settings.set("ai.visual_score", False)
    yield
    gate.configure(None)
    clean_posting_tables()

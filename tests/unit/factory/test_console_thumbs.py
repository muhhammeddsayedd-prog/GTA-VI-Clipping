"""Clip thumbnails (console media): a real ffmpeg frame grab into fake Storage, and the never-raise wrapper
the render calls. The database writes are captured, not run."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sixstars import storage
from sixstars.factory import thumbs

WHEN = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
PARAMS = thumbs.ThumbParams(frame_s=1.5, width_px=540, qscale=5)


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []
    monkeypatch.setattr(thumbs.db, "execute", lambda sql, params=None: seen.append((sql, params)) or 1)
    return seen


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(thumbs.events, "emit", lambda type, **kw: seen.append({"type": type, **kw}) or 1)
    monkeypatch.setattr(thumbs.ThumbParams, "load", classmethod(lambda cls: PARAMS))
    return seen


@pytest.fixture
def vertical_clip(ffmpeg_tools: tuple[str, str], tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg_tools[0], "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi"]
        + [
            "-i",
            "testsrc=size=1080x1920:rate=30:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_make_thumb_grabs_uploads_and_records(
    vertical_clip: Path, ffmpeg_tools: tuple[str, str], writes: list[tuple[str, Any]]
) -> None:
    fake = storage.FakeClipStorage()
    key = thumbs.make_thumb(
        "clip-1",
        str(vertical_clip),
        created_at=WHEN,
        duration_s=3.0,
        storage=fake,
        params=PARAMS,
        ffmpeg=ffmpeg_tools[0],
    )
    assert key == storage.thumb_key("clip-1", WHEN)
    jpeg = fake.objects[key]
    assert jpeg[:2] == b"\xff\xd8"  # a JPEG
    probe = subprocess.run(
        [ffmpeg_tools[1], "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", "-"],
        input=jpeg,
        capture_output=True,
        check=True,
    )
    assert probe.stdout.decode().strip() == "540,960"
    assert writes == [("update clips set thumb_path = %s where id = %s", (key, "clip-1"))]


def test_try_make_thumb_never_raises(
    tmp_path: Path, emitted: list[dict[str, Any]], writes: list[tuple[str, Any]]
) -> None:
    fake = storage.FakeClipStorage()
    missing = tmp_path / "gone.mp4"
    result = thumbs.try_make_thumb("clip-2", str(missing), created_at=WHEN, duration_s=10.0, storage=fake)
    assert result is None and fake.objects == {} and writes == []
    assert [event["type"] for event in emitted] == ["clip.thumb_failed"]
    assert emitted[0]["entity_id"] == "clip-2" and emitted[0]["level"] == "warn"


def test_try_make_thumb_survives_an_upload_error(
    vertical_clip: Path,
    ffmpeg_tools: tuple[str, str],
    emitted: list[dict[str, Any]],
    writes: list[tuple[str, Any]],
) -> None:
    fake = storage.FakeClipStorage()
    fake.error = RuntimeError("storage down")
    result = thumbs.try_make_thumb(
        "clip-3", str(vertical_clip), created_at=WHEN, duration_s=3.0, storage=fake, ffmpeg=ffmpeg_tools[0]
    )
    assert result is None and writes == []
    assert emitted[0]["message"] == "RuntimeError: storage down"


def test_try_make_thumb_skips_without_storage(emitted: list[dict[str, Any]]) -> None:
    unconfigured = storage.ClipStorage()
    assert (
        thumbs.try_make_thumb("clip-4", "x.mp4", created_at=WHEN, duration_s=3.0, storage=unconfigured)
        is None
    )
    assert emitted == []

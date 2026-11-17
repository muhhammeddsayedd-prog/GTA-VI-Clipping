"""Live check of the recorder and the rolling buffer against a real stream (read-only, no database):

- a live YouTube stream found by the discovery search is recorded with the live lane's own commands;
- its segments are indexed by wall clock and a window cut from the buffer has the requested length;
- a finished video is refused as "not live" instead of being recorded from its start.

Run with `RUN_LIVE_TESTS=1 uv run pytest tests/live/test_live_recording.py -v` (about one minute). Set
`LIVE_STREAM_URL` to record a given stream (a Twitch channel URL works too) instead of searching.
"""

import os
import time
from pathlib import Path

import pytest

from sixstars.discovery.ytsearch import YtDlpSearch
from sixstars.live import buffer, commands
from sixstars.live.recorder import Recorder, stream_url

RECORD_S = 40.0
SEGMENT_S = 10.0
FINISHED_VIDEO = "https://www.youtube.com/watch?v=3ZGMLyZzrPI"  # a past GTA stream (M03 notes)


def live_url() -> str:
    given = os.environ.get("LIVE_STREAM_URL", "")
    if given:
        return given
    rows = [row for row in YtDlpSearch().search("GTA 5", live=True, limit=20) if row.is_live]
    if not rows:
        pytest.skip("the search found no live stream")
    best = max(rows, key=lambda row: row.viewers or 0)
    return f"https://www.youtube.com/watch?v={best.video_id}"


def recorder_for(url: str, directory: Path) -> Recorder:
    platform = commands.platform_of(url)
    login = commands.twitch_login(url) if platform == "twitch" else ""
    return Recorder(
        stream_url(platform, login, "", url),
        platform,
        directory,
        segment_s=SEGMENT_S,
        max_height=720,
        start_timeout_s=60,
        stall_timeout_s=45,
    )


def test_a_live_stream_is_recorded_indexed_and_cut(tmp_path: Path) -> None:
    recorder = recorder_for(live_url(), tmp_path / "session")
    started = time.time()
    recorder.start()
    try:
        failure = None
        while time.time() - started < RECORD_S and failure is None:
            failure = recorder.poll().failure
            time.sleep(1.0)
        assert failure is None, failure
        segments = buffer.scan(recorder.directory)
        assert len(segments) >= 3 and all(segment.run == 1 for segment in segments)
        # names are UTC wall-clock times of this run, whatever the machine's time zone is
        assert all(started - 5 <= segment.start <= time.time() + 1 for segment in segments)
        now = time.time()
        cut = buffer.extract(
            recorder.directory,
            now - 24,
            now - 6,
            buffer.cut_path(recorder.directory, "check"),
            anchor_span_s=300,
        )
        assert abs((cut.end - cut.start) - 18.0) < 1.5  # the window that was asked for
        assert cut.duration_s >= cut.end - cut.file_start - 1.5  # plus the lead-in to the segment start
        assert cut.file_start <= cut.start <= cut.file_start + SEGMENT_S + 6
    finally:
        recorder.stop(8)


def test_a_finished_video_is_not_recorded_as_if_it_were_live(tmp_path: Path) -> None:
    recorder = recorder_for(FINISHED_VIDEO, tmp_path / "session")
    recorder.start()
    try:
        failure = None
        deadline = time.time() + 70
        while failure is None and time.time() < deadline:
            failure = recorder.poll().failure
            time.sleep(1.0)
        assert failure is not None and failure.kind in ("not_live", "ended"), failure
        assert buffer.scan(recorder.directory) == []
    finally:
        recorder.stop(5)

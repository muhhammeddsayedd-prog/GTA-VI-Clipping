"""Live checks of the cheap YouTube discovery route (read-only, no Apify, no database):

- the `sp` filter values of `discovery.ytsearch` still mean "live now" and "recent videos" on YouTube;
- a flat yt-dlp listing still carries the fields discovery reads;
- `videos.list` still returns the fields `sources.candidate_from_api` reads, at 1 quota unit per call.

Run with `RUN_LIVE_TESTS=1 uv run pytest tests/live/test_discovery_live.py -v`.
"""

from datetime import UTC, datetime

import pytest

from sixstars.clients.youtube import YouTubeClient
from sixstars.discovery import sources
from sixstars.discovery.ytsearch import YtDlpSearch

TERM = "GTA 5"


def test_live_filter_returns_streams_that_are_live_now() -> None:
    rows = YtDlpSearch().search(TERM, live=True, limit=20)
    assert len(rows) >= 5
    assert all(row.is_live for row in rows)
    assert all(row.duration_s is None for row in rows)
    assert sum(1 for row in rows if row.viewers is not None) >= len(rows) // 2
    assert all(row.channel_id for row in rows)


def test_recent_filter_returns_finished_videos() -> None:
    rows = YtDlpSearch().search(TERM, within_hours=48, sort="views", limit=20)
    assert len(rows) >= 5
    finished = [row for row in rows if not row.is_live]
    assert len(finished) >= len(rows) // 2
    assert all(row.duration_s for row in finished)


def test_videos_list_enriches_live_and_finished_videos() -> None:
    client = YouTubeClient()
    if not client.is_configured():
        pytest.skip("YOUTUBE_API_KEY is not set")
    search = YtDlpSearch()
    live = search.search(TERM, live=True, limit=3)
    recent = search.search(TERM, within_hours=48, limit=3)
    ids = [row.video_id for row in live + recent]
    items = client.videos_list(ids, parts=sources.VIDEO_PARTS)
    assert len(items) >= len(ids) - 1
    now = datetime.now(UTC)
    candidates = [sources.candidate_from_api(item, None, now) for item in items]
    kept = [candidate for candidate in candidates if candidate is not None]
    streams = [candidate for candidate in kept if candidate.source_type == "live"]
    videos = [candidate for candidate in kept if candidate.source_type == "vod"]
    assert streams and videos
    assert all(stream.started_at is not None and stream.channel_id.startswith("UC") for stream in streams)
    assert any(stream.viewers is not None for stream in streams)
    assert all(video.published_at is not None and video.duration_s is not None for video in videos)
    assert any(video.views is not None and video.likes is not None for video in videos)

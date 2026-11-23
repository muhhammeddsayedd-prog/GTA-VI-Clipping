"""The staging server's situation against Postgres with fake clients (fix-blocked-platform-queue): YouTube
refuses every download, 24 YouTube videos wait out the block in status `new`, Twitch works. Discovery must
keep queueing Twitch, queue nothing new of YouTube, bound what is kept waiting, probe when nothing is
waiting, and hand live slots to the platform the live lane can record from."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from sixstars import db, settings
from sixstars.clients.fakes import (
    FIXED_TIME,
    FakeAnthropicClient,
    FakeApifyClient,
    FakeTwitchClient,
    FakeYouTubeClient,
    fake_video,
)
from sixstars.clock import DATE_TIME, fmt_local
from sixstars.discovery import config as discovery_config
from sixstars.discovery.run import DiscoveryClients, DiscoverySummary, run_discovery
from sixstars.discovery.ytsearch import FakeYtDlpSearch, FlatVideo
from sixstars.live import health as live_health
from sixstars.vod import health as vod_health

NOW = FIXED_TIME + timedelta(hours=5)
MIGRATION = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0013_blocked_platforms.sql"
P6 = "p6_permission_read"
BOT_CHECK = (
    "ERROR: [youtube] ytwait00001: Sign in to confirm you’re not a bot. Use --cookies-from-browser or "
    "--cookies for the authentication."
)
BLOCKED_REASON = f"download_blocked: {BOT_CHECK}"


@pytest.fixture(autouse=True)
def clean_discovery(database: None, test_database_url: str) -> Iterator[None]:
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("truncate creators cascade")
    settings.set("env.target_game", "Grand Theft Auto V")
    settings.set("env.search_terms", ["GTA 5"])
    settings.set("live.max_slots", 2)
    yield
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("truncate creators cascade")


def live_item(video_id: str, channel: str, viewers: int) -> dict[str, Any]:
    return {
        "id": video_id,
        "snippet": {
            "publishedAt": "2026-11-17T14:00:00Z",
            "channelId": channel,
            "channelTitle": f"Name {channel}",
            "title": "GTA 5 live",
            "liveBroadcastContent": "live",
            "defaultAudioLanguage": "en",
        },
        "contentDetails": {"duration": "P0D"},
        "statistics": {"viewCount": "9000", "likeCount": "300"},
        "liveStreamingDetails": {
            "actualStartTime": "2026-11-17T14:05:00Z",
            "concurrentViewers": str(viewers),
        },
    }


def vod_item(video_id: str, channel: str, views: int) -> dict[str, Any]:
    return {
        "id": video_id,
        "snippet": {
            "publishedAt": "2026-11-17T08:00:00Z",
            "channelId": channel,
            "channelTitle": f"Name {channel}",
            "title": "GTA 5 stream",
            "liveBroadcastContent": "none",
            "defaultAudioLanguage": "en",
        },
        "contentDetails": {"duration": "PT3H"},
        "statistics": {"viewCount": str(views), "likeCount": str(views // 40), "commentCount": "200"},
        "liveStreamingDetails": {
            "actualStartTime": "2026-11-17T08:00:00Z",
            "actualEndTime": "2026-11-17T11:00:00Z",
        },
    }


class World:
    """3 Twitch streams and 2 Twitch archives; 2 live YouTube streams and 2 finished YouTube streams."""

    def __init__(self) -> None:
        self.twitch = FakeTwitchClient()
        self.twitch.videos = [replace(fake_video(i), stream_id=f"ended{i}") for i in range(2)]
        self.youtube = FakeYouTubeClient()
        self.search = FakeYtDlpSearch()
        self.search.default_live = [
            FlatVideo("ytlive00001", "GTA 5 live", "UCyt1", "Name UCyt1", "is_live", 5000, None, None),
            FlatVideo("ytlive00002", "GTA 5 live", "UCyt2", "Name UCyt2", "is_live", 1500, None, None),
        ]
        self.search.default_recent = [
            FlatVideo("ytvod000001", "GTA 5 stream", "UCyt3", "Name UCyt3", "was_live", None, 200_000, 10800),
            FlatVideo("ytvod000002", "GTA 5 stream", "UCyt4", "Name UCyt4", "was_live", None, 90_000, 10800),
        ]
        self.youtube.videos = {
            "ytlive00001": live_item("ytlive00001", "UCyt1", 5000),
            "ytlive00002": live_item("ytlive00002", "UCyt2", 1500),
            "ytvod000001": vod_item("ytvod000001", "UCyt3", 200_000),
            "ytvod000002": vod_item("ytvod000002", "UCyt4", 90_000),
        }
        self.clients = DiscoveryClients(
            self.twitch,
            self.youtube,
            FakeApifyClient(),
            self.search,
            FakeAnthropicClient({P6: {"tier": "auto", "evidence": None}}),
        )

    def run(self, at: datetime = NOW) -> DiscoverySummary:
        settings.invalidate()
        return run_discovery(self.clients, now=at)


def count(table: str, where: str = "true") -> int:
    row = db.fetch_one(f"select count(*) as n from {table} where {where}")
    assert row is not None
    return int(row["n"])


def videos(where: str = "true") -> set[tuple[str, str]]:
    rows = db.fetch_all(f"select platform, source_id from videos where {where}")
    return {(row["platform"], row["source_id"]) for row in rows}


def waiting_youtube_videos(number: int) -> None:
    """`number` YouTube videos the VOD lane put back to `new` after the bot check, each with the job that
    tries it again; `ytwait00001` is the oldest."""
    creator = db.fetch_one(
        "insert into creators (platform, channel_id, handle) values ('youtube', 'UCwaiting', 'waiting') "
        "returning id::text as id"
    )
    assert creator is not None
    for index in range(1, number + 1):
        source_id = f"ytwait{index:05d}"
        row = db.fetch_one(
            """
            insert into videos (creator_id, platform, source_id, url, title, status, skip_reason, created_at)
            values (%s, 'youtube', %s, %s, 'GTA 5 stream', 'new', %s, %s)
            returning id::text as id
            """,
            (
                creator["id"],
                source_id,
                f"https://www.youtube.com/watch?v={source_id}",
                BLOCKED_REASON,
                NOW - timedelta(hours=10) + timedelta(minutes=index),
            ),
        )
        assert row is not None
        db.execute(
            "insert into jobs (queue, type, payload, priority, run_after, dedupe_key) "
            "values ('normal', 'vod.process', %s, 30, %s, %s)",
            (Jsonb({"video_id": row["id"]}), NOW + timedelta(minutes=20), f"vod:{row['id']}:again"),
        )


def youtube_downloads_blocked(at: datetime = NOW - timedelta(minutes=10)) -> None:
    """What the VOD lane records when a download is classified as blocked."""
    vod_health.record_blocked("youtube", at, BOT_CHECK, "no proxy, no cookies")


# ── the staging situation ────────────────────────────────────


def test_a_backlog_of_blocked_youtube_videos_no_longer_starves_twitch() -> None:
    world = World()
    waiting_youtube_videos(24)
    youtube_downloads_blocked()
    assert settings.get("discovery.max_vod_backlog") == 24  # the seeded cap, as full as on staging

    summary = world.run()

    # Twitch is queued; of YouTube nothing new, although its videos rank and are stored as candidates.
    assert summary.videos_queued == 2
    assert videos("created_at > now() - interval '1 hour'") == {("twitch", "video0"), ("twitch", "video1")}
    assert (
        count("jobs", "type = 'vod.process' and status = 'queued' and payload->>'platform' = 'twitch'") == 2
    )
    assert count("candidates", "platform = 'youtube' and source_type = 'vod'") == 2
    assert count("candidates", "platform = 'youtube' and source_type = 'vod' and picked") == 0
    assert count("creators", "platform = 'youtube' and channel_id in ('UCyt3', 'UCyt4')") == 2
    assert summary.skipped == {"platform_blocked": 2}
    assert summary.vod_max == 12  # the widened run, not cut by the blocked videos

    # Only `discovery.max_blocked_waiting` (12) stay waiting, the newest; the rest is closed with a reason.
    assert count("videos", "status = 'new' and skip_reason like 'download_blocked%'") == 12
    skipped = db.fetch_all("select source_id, skip_reason from videos where status = 'skipped'")
    assert {row["source_id"] for row in skipped} == {f"ytwait{index:05d}" for index in range(1, 13)}
    assert all(row["skip_reason"].startswith("gave up waiting") for row in skipped)
    assert count("jobs", "status = 'cancelled'") == 12
    assert count("jobs", "status = 'queued' and dedupe_key like '%:again'") == 12  # the probe goes on
    assert count("events", "type = 'video.skipped'") == 12

    # Said once in the summary, and to nobody's phone.
    assert summary.notes == [
        "12 video(s) that waited out a download block were skipped (more than 12 waiting per platform)",
        f"youtube downloads are blocked since {fmt_local(NOW - timedelta(minutes=10), DATE_TIME)}: "
        "2 video(s) not queued, 12 waiting for the VOD lane's retry",
    ]
    assert count("notify_outbox") == 0
    assert summary.errors == []

    # The next pass changes nothing more: no new skips, no YouTube, the same single note.
    again = world.run(NOW + timedelta(minutes=10))
    assert again.videos_queued == 0 and again.skipped == {"platform_blocked": 2}
    assert count("videos", "status = 'skipped'") == 12 and len(again.notes) == 1
    assert count("videos", "platform = 'youtube' and source_id like 'ytvod%'") == 0


def test_when_the_block_lifts_youtube_is_queued_again_and_nothing_waiting_is_lost() -> None:
    world = World()
    waiting_youtube_videos(5)
    youtube_downloads_blocked()
    blocked_pass = world.run()
    assert blocked_pass.videos_queued == 2 and count("videos", "status = 'skipped'") == 0

    vod_health.record_ok("youtube")  # the VOD lane downloaded a window: the block is over
    lifted = world.run(NOW + timedelta(minutes=10))

    assert settings.get(vod_health.STATE_KEY) == {}
    assert videos("source_id like 'ytvod%'") == {("youtube", "ytvod000001"), ("youtube", "ytvod000002")}
    assert lifted.videos_queued == 2 and lifted.skipped == {} and lifted.notes == []
    assert count("videos", "status = 'new' and skip_reason like 'download_blocked%'") == 5  # still draining


def test_with_nothing_waiting_one_video_of_the_blocked_platform_is_queued_as_the_probe() -> None:
    world = World()
    youtube_downloads_blocked()

    summary = world.run()

    assert videos("platform = 'youtube'") == {("youtube", "ytvod000001")}  # the best one, and only one
    assert summary.videos_queued == 3 and summary.skipped == {"platform_blocked": 1}
    assert "1 queued as a probe" in summary.notes[0]

    # While that probe is open the platform gets nothing more.
    later = world.run(NOW + timedelta(minutes=10))
    assert later.videos_queued == 0 and videos("platform = 'youtube'") == {("youtube", "ytvod000001")}


def test_a_block_nobody_refreshed_stops_holding_the_platform_back() -> None:
    world = World()
    youtube_downloads_blocked(NOW - timedelta(hours=2, minutes=1))  # discovery.blocked_platform_stale_s

    summary = world.run()

    assert summary.videos_queued == 4 and summary.skipped == {} and summary.notes == []


# ── live slots ───────────────────────────────────────────────


def live_blocked(*platforms: str) -> None:
    settings.set(
        live_health.STATE_KEY,
        {
            platform: {
                "blocks": 3,
                "since": (NOW - timedelta(hours=1)).isoformat(),
                "skip_until": (NOW + timedelta(minutes=20)).isoformat(),
                "notified": True,
                "access": None,
            }
            for platform in platforms
        },
    )


def sessions() -> list[tuple[str, str]]:
    rows = db.fetch_all("select platform, source_id from live_sessions order by source_id")
    return [(row["platform"], row["source_id"]) for row in rows]


def test_live_slots_go_to_the_platform_the_live_lane_can_record_from() -> None:
    world = World()
    live_blocked("twitch")  # without the block the two slots go to Twitch (10,000 and 9,900 viewers)

    summary = world.run()

    assert sessions() == [("youtube", "ytlive00001"), ("youtube", "ytlive00002")]
    assert summary.sessions_assigned == 2
    assert summary.skipped == {"live_platform_blocked": 3}
    assert summary.notes == ["twitch live recording is blocked: 3 stream(s) got no slot"]
    assert count("notify_outbox") == 0


def test_when_every_live_platform_is_blocked_no_slot_is_assigned() -> None:
    world = World()
    live_blocked("twitch", "youtube")

    summary = world.run()

    assert sessions() == [] and summary.sessions_assigned == 0
    assert summary.notes == [
        "no live slot assigned: every platform with streams is blocked (twitch, youtube)"
    ]
    assert summary.videos_queued == 4  # the VOD lane has its own record and is not blocked


def test_after_the_back_off_one_stream_of_the_platform_probes() -> None:
    world = World()
    live_blocked("twitch")

    summary = world.run(NOW + timedelta(minutes=21))

    assert sessions() == [("twitch", "stream0"), ("youtube", "ytlive00001")]
    assert summary.sessions_assigned == 2


# ── settings ─────────────────────────────────────────────────


def test_the_platform_weight_setting_changes_the_stored_scores() -> None:
    world = World()
    plain = world.run()
    before = {row["url"]: row["score"] for row in plain.top_videos}
    assert all("platform_weight" not in row["parts"] for row in plain.top_videos)  # {}: no preference

    settings.set("discovery.platform_weight", {"twitch": 0.5, "youtube": "x"})
    weighted = world.run(NOW + timedelta(minutes=10))

    twitch = [row for row in weighted.top_videos if row["platform"] == "twitch"]
    youtube = [row for row in weighted.top_live if row["platform"] == "youtube"]
    assert len(twitch) == 2 and all(row["parts"]["platform_weight"] == 0.5 for row in twitch)
    assert all(row["score"] == pytest.approx(before[row["url"]] * 0.5, abs=0.5) for row in twitch)
    assert youtube and all("platform_weight" not in row["parts"] for row in youtube)  # not a number: ignored
    stored = db.fetch_all(
        "select trend_score from candidates where platform = 'twitch' and source_type = 'vod' "
        "and checked_at = %s",
        (NOW + timedelta(minutes=10),),
    )
    assert sorted(float(row["trend_score"]) for row in stored) == sorted(row["score"] for row in twitch)


def test_code_defaults_match_migration_0013_and_it_can_be_applied_twice(test_database_url: str) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for key, value in discovery_config.BLOCKED_DEFAULTS.items():
        assert f"('{key}'," in sql, key
        assert settings.get(key) == value, key
    assert settings.get(vod_health.STATE_KEY) is None
    settings.set("discovery.max_blocked_waiting", 3)
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
        conn.execute(MIGRATION.read_bytes())
    settings.invalidate()
    assert settings.get("discovery.max_blocked_waiting") == 3  # an operator's value is kept
    assert settings.get("discovery.max_vod_backlog") == 24  # the seeded default is left as it is
    docs = (Path(__file__).resolve().parents[2] / "docs" / "16-settings-reference.md").read_text("utf-8")
    for key in (*discovery_config.BLOCKED_DEFAULTS, vod_health.STATE_KEY):
        assert f"| `{key}` |" in docs, key

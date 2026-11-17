"""Source hardening against Postgres with fake clients: discovery under the staging server's condition
(every yt-dlp search refused), the Data API search budget, the source-health notifications, the RSS
rotation, the second look at known live streams, the language rule and the doctor command."""

from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from sixstars import db, settings, usage
from sixstars.cli import app
from sixstars.clients._http import ApiError
from sixstars.clients.fakes import (
    FIXED_TIME,
    FakeAnthropicClient,
    FakeApifyClient,
    FakeTwitchClient,
    FakeYouTubeClient,
    fake_search_item,
)
from sixstars.clients.youtube import FeedEntry
from sixstars.discovery import config as discovery_config
from sixstars.discovery import doctor, health, sources
from sixstars.discovery.run import DiscoveryClients, DiscoverySummary, run_discovery
from sixstars.discovery.ytsearch import FakeYtDlpSearch, FlatVideo, YtDlpError
from sixstars.vod import ytdlp
from sixstars.vod.config import DEFAULTS as VOD_DEFAULTS
from sixstars.vod.ytdlp import Access

NOW = FIXED_TIME + timedelta(hours=5)
MIGRATION = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0010_sources.sql"
P6 = "p6_permission_read"
# The exact error of the staging server (Hetzner, data-centre address), every pass, every search.
SERVER_ERROR = (
    "yt-dlp search failed for 'GTA 5': ERROR: query \"GTA 5\" page 1: Unable to download API page: "
    "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
)
BOT_CHECK = (
    "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you're not a bot. Use --cookies-from-browser or "
    "--cookies for the authentication."
)


@pytest.fixture(autouse=True)
def clean_discovery(database: None, test_database_url: str) -> Iterator[None]:
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("truncate creators cascade")
    settings.set("env.target_game", "Grand Theft Auto V")
    settings.set("env.search_terms", ["GTA 5", "GTA V"])
    settings.set("live.max_slots", 2)
    yield
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("truncate creators cascade")


class ProxiedSearch(FakeYtDlpSearch):
    """A yt-dlp search that goes through the owner's proxy (and works)."""

    access = "proxy"


class CountingYouTube(FakeYouTubeClient):
    """The fake, but a `search.list` call writes its `api_usage` row like the real client does."""

    def search_list(self, q: str, **kwargs: Any) -> list[dict[str, Any]]:
        usage.record("youtube", "search.list", units=1)
        return super().search_list(q, **kwargs)


def live_item(video_id: str, channel: str, viewers: int, **snippet: Any) -> dict[str, Any]:
    return {
        "id": video_id,
        "snippet": {
            "publishedAt": "2026-11-17T14:00:00Z",
            "channelId": channel,
            "channelTitle": f"Name {channel}",
            "title": "GTA 5 live",
            "liveBroadcastContent": "live",
            "defaultAudioLanguage": "en",
            **snippet,
        },
        "contentDetails": {"duration": "P0D"},
        "statistics": {"viewCount": "9000", "likeCount": "300"},
        "liveStreamingDetails": {
            "actualStartTime": "2026-11-17T14:05:00Z",
            "concurrentViewers": str(viewers),
        },
    }


def vod_item(video_id: str, channel: str, *, was_live: bool = True, **snippet: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": video_id,
        "snippet": {
            "publishedAt": "2026-11-17T08:00:00Z",
            "channelId": channel,
            "channelTitle": f"Name {channel}",
            "title": "GTA 5 stream",
            "liveBroadcastContent": "none",
            "defaultAudioLanguage": "en",
            **snippet,
        },
        "contentDetails": {"duration": "PT3H" if was_live else "PT25M"},
        "statistics": {"viewCount": "200000", "likeCount": "5000", "commentCount": "200"},
    }
    if was_live:
        item["liveStreamingDetails"] = {
            "actualStartTime": "2026-11-17T08:00:00Z",
            "actualEndTime": "2026-11-17T11:00:00Z",
        }
    return item


class Server:
    """The staging server as discovery sees it: no Twitch keys, every yt-dlp search refused, the Data API
    and the channel feeds working. Two live streams and two finished videos exist."""

    def __init__(self) -> None:
        self.twitch = FakeTwitchClient(configured=False)
        self.youtube = CountingYouTube()
        self.apify = FakeApifyClient()
        self.apify.search_results = []
        self.search = FakeYtDlpSearch()
        self.search.error = YtDlpError(SERVER_ERROR)
        self.anthropic = FakeAnthropicClient({P6: {"tier": "auto", "evidence": None}})
        self.youtube.search_live = [
            fake_search_item("ytlive00001", "UCyt1", "GTA 5 live", live=True),
            fake_search_item("ytlive00002", "UCyt2", "GTA 5 live", live=True),
        ]
        self.youtube.search_recent = [
            fake_search_item("ytvod000001", "UCyt3", "GTA 5 stream"),
            fake_search_item("ytedit00001", "UCyt4", "GTA 5 funny moments"),
        ]
        self.youtube.videos = {
            "ytlive00001": live_item("ytlive00001", "UCyt1", 5000),
            "ytlive00002": live_item("ytlive00002", "UCyt2", 1500),
            "ytvod000001": vod_item("ytvod000001", "UCyt3"),
            "ytedit00001": vod_item("ytedit00001", "UCyt4", was_live=False, title="GTA 5 funny moments"),
        }
        self.youtube.feeds = {}

    @property
    def clients(self) -> DiscoveryClients:
        return DiscoveryClients(self.twitch, self.youtube, self.apify, self.search, self.anthropic)

    def run(self, minutes: float = 0) -> DiscoverySummary:
        settings.invalidate()
        return run_discovery(self.clients, now=NOW + timedelta(minutes=minutes))

    def searches(self) -> list[dict[str, Any]]:
        return self.youtube.calls_to("search_list")


def source(summary: DiscoverySummary, name: str) -> dict[str, Any]:
    return next(row for row in summary.sources if row["name"] == name)


def count(table: str, where: str = "true", params: Sequence[Any] = ()) -> int:
    row = db.fetch_one(f"select count(*) as n from {table} where {where}", tuple(params))
    assert row is not None
    return int(row["n"])


def notifications() -> list[tuple[str, str, str]]:
    rows = db.fetch_all("select severity, title, body from notify_outbox order by id")
    return [(str(row["severity"]), str(row["title"]), str(row["body"])) for row in rows]


# ── the server condition ─────────────────────────────────────


def test_with_ytdlp_search_dead_discovery_runs_on_the_data_api_search() -> None:
    server = Server()

    first = server.run()
    assert source(first, "youtube_search")["status"] == "error"
    assert first.errors == [f"youtube_search: {SERVER_ERROR}"]
    api = source(first, "youtube_search_api")
    assert (api["status"], api["found"], api["detail"]) == ("ok", 2, "live search, 1 of 60 calls today")
    call = server.searches()[0]
    assert call["q"] == "GTA 5" and call["event_type"] == "live"  # live: one term per call
    assert (call["order"], call["video_category_id"], call["relevance_language"]) == ("viewCount", "20", "en")
    assert (call["published_after"], call["video_duration"], call["max_results"]) == (None, None, 50)
    assert (first.live_seen, first.sessions_assigned) == (2, 2)
    assert (first.youtube_search_api_calls, first.youtube_search_api_calls_today) == (1, 1)
    assert len(server.search.calls) == 6  # the first pass tries every yt-dlp search

    # 10 minutes later: one probe instead of six searches, no Data API search (interval), and the two
    # streams are still followed through videos.list.
    server.youtube.videos["ytlive00001"] = live_item("ytlive00001", "UCyt1", 7500)
    second = server.run(10)
    assert len(server.search.calls) == 7
    assert "probe: 1 search instead of 6" in source(second, "youtube_search")["detail"]
    api = source(second, "youtube_search_api")
    assert api["status"] == "skipped" and api["detail"].startswith("interval not reached")
    assert len(server.searches()) == 1
    assert (second.live_seen, second.sessions_active, second.sessions_released) == (2, 2, 0)
    row = db.fetch_one(
        "select viewers, score_parts from candidates where source_id = 'ytlive00001' "
        "order by checked_at desc limit 1"
    )
    assert row is not None and row["viewers"] == 7500 and row["score_parts"]["growth"] == 0.5

    # 20 minutes: yt-dlp backs off (no subprocess at all) and the owner is told once.
    third = server.run(20)
    assert len(server.search.calls) == 7
    skipped = source(third, "youtube_search")
    assert skipped["status"] == "skipped" and skipped["detail"].startswith("backing off until 18:10 UTC")
    assert third.errors == [] and third.live_seen == 2
    assert third.notes == ["Discovery source youtube_search keeps failing"]
    sent = notifications()
    assert [(severity, title) for severity, title, _ in sent] == [
        ("warn", "Discovery source youtube_search keeps failing")
    ]
    assert "RemoteDisconnected('Remote end closed connection without response')" in sent[0][2]
    assert "PROXY_URL" in sent[0][2] and "3 passes in a row" in sent[0][2]
    assert count("events", "type = 'discovery.source_failing'") == 1

    # 30 minutes: the next Data API search is the recent one; a finished stream and an edited upload
    # are found and queued for the VOD lane.
    fourth = server.run(30)
    api = source(fourth, "youtube_search_api")
    assert (api["status"], api["found"], api["detail"]) == ("ok", 2, "recent search, 2 of 60 calls today")
    call = server.searches()[1]
    assert call["q"] == '"GTA 5"|"GTA V"' and call["video_duration"] == "long"  # recent: every term at once
    assert call["event_type"] is None and call["published_after"] == NOW + timedelta(minutes=30, hours=-48)
    assert (fourth.live_seen, fourth.videos_seen, fourth.videos_queued) == (2, 2, 2)
    assert count("videos", "source_id in ('ytvod000001', 'ytedit00001')") == 2
    assert count("jobs", "type = 'vod.process'") == 2
    assert count("api_usage", "provider = 'youtube' and operation = 'search.list'") == 2

    # No second warning while it keeps failing, however many passes follow.
    server.run(40)
    server.run(50)
    assert len(notifications()) == 1 and len(server.search.calls) == 7

    # An hour later the Data API search is the live one again, and yt-dlp probes once.
    later = server.run(60)
    assert source(later, "youtube_search_api")["detail"].startswith("live search")
    assert server.searches()[2]["q"] == "GTA V"  # ... for the next term
    server.run(70)
    assert len(server.search.calls) == 8

    # The owner sets PROXY_URL and restarts the containers. The back-off would last until 19:10, but the
    # access has changed: the very next pass probes, the probe works, all searches run, and one
    # "recovered" message goes out.
    assert source(server.run(75), "youtube_search")["detail"].startswith("backing off until 19:10 UTC")
    server.search = ProxiedSearch()
    server.search.default_live = [
        FlatVideo("ytlive00001", "GTA 5 live", "UCyt1", "Name UCyt1", "is_live", 7500, None, None)
    ]
    after_backoff = server.run(80)
    assert source(after_backoff, "youtube_search")["status"] == "ok"
    assert len(server.search.calls) == 6
    api = source(after_backoff, "youtube_search_api")
    assert api["status"] == "skipped" and api["detail"].startswith("not needed: the yt-dlp search works")
    sent = notifications()
    assert [(severity, title) for severity, title, _ in sent] == [
        ("warn", "Discovery source youtube_search keeps failing"),
        ("info", "Discovery source youtube_search recovered"),
    ]
    assert settings.get(health.STATE_KEY) == {}


def test_while_ytdlp_search_works_the_data_api_search_is_not_used() -> None:
    server = Server()
    server.search.error = None
    server.search.default_live = [
        FlatVideo("ytlive00001", "GTA 5 live", "UCyt1", "Name UCyt1", "is_live", 5000, None, None)
    ]
    summary = server.run()
    assert source(summary, "youtube_search")["status"] == "ok"
    assert source(summary, "youtube_search_api")["status"] == "skipped"
    assert server.searches() == [] and summary.youtube_search_api_calls == 0

    settings.set("discovery.search_api_mode", "always")
    summary = server.run(10)
    assert source(summary, "youtube_search_api")["status"] == "ok" and len(server.searches()) == 1

    settings.set("discovery.search_api_mode", "off")
    summary = server.run(60)
    assert all(row["name"] != "youtube_search_api" for row in summary.sources)
    assert len(server.searches()) == 1


def test_a_spent_search_budget_is_said_once_and_discovery_goes_on_without_alerts() -> None:
    server = Server()
    server.run()  # creators become known through the first search
    db.execute(
        "insert into api_usage (provider, operation, units) "
        "select 'youtube', 'search.list', 1 from generate_series(1, 59)"
    )
    server.youtube.feeds = {
        "UCyt1": [FeedEntry("rssgta00001", "UCyt1", "GTA 5 heist stream", NOW + timedelta(minutes=20))]
    }
    server.youtube.videos["rssgta00001"] = vod_item("rssgta00001", "UCyt1", title="GTA 5 heist stream")

    spent = server.run(40)
    api = source(spent, "youtube_search_api")
    assert (api["status"], api["found"]) == ("skipped", 0)
    assert api["detail"] == "daily budget spent (60 of 60 calls)"
    assert len(spent.notes) == 1 and spent.notes[0].startswith("search.list daily budget spent (60 of 60")
    assert "discovery continues on channel feeds" in spent.notes[0]
    assert len(server.searches()) == 1  # no call was made
    # ... and the pass still found things: the known live streams and a feed entry
    assert spent.live_seen == 2 and source(spent, "youtube_rss")["found"] == 1
    assert count("candidates", "source_id = 'rssgta00001'") == 1

    again = server.run(70)
    assert source(again, "youtube_search_api")["status"] == "skipped"
    assert len(server.searches()) == 1
    # the budget is not said again and never notifies; the one warning of this pass is about yt-dlp
    assert again.notes == ["Discovery source youtube_search keeps failing"]
    assert [title for _, title, _ in notifications()] == ["Discovery source youtube_search keeps failing"]

    # the 3,000-unit budget of videos.list is counted apart from the search calls
    assert again.youtube_quota_units_today == 0
    assert again.youtube_search_api_calls_today == 60


def test_a_quota_refusal_from_google_pauses_the_search_until_the_reset() -> None:
    server = Server()
    refusal = "The request cannot be completed because you have exceeded your quota."
    server.youtube.search_error = ApiError("youtube", "GET /youtube/v3/search", 403, refusal)
    first = server.run()
    api = source(first, "youtube_search_api")
    assert api["status"] == "error" and refusal in api["detail"]
    state = settings.get(sources.SEARCH_API_STATE_KEY)
    assert state["quota_until"] == "2026-11-18T08:10:00+00:00"

    server.youtube.search_error = None
    paused = server.run(40)
    api = source(paused, "youtube_search_api")
    assert api["status"] == "skipped" and "next try after 08:10 UTC" in api["detail"]
    assert len(server.searches()) == 1

    resumed = server.run(15 * 60 + 20)  # 08:20 UTC the next day
    assert source(resumed, "youtube_search_api")["status"] == "ok" and len(server.searches()) == 2


# ── RSS ──────────────────────────────────────────────────────


def test_the_feed_rotation_reaches_every_known_creator_and_queues_a_finished_stream() -> None:
    server = Server()
    settings.set("discovery.search_api_mode", "off")
    settings.set("discovery.live_recheck_max", 0)
    settings.set("discovery.rss_max_channels_per_run", 3)
    channels = [f"UCrss{index:02d}" for index in range(7)]
    db.execute(
        "insert into creators (platform, channel_id, handle, display_name, tier, description_checked_at) "
        "select 'youtube', c, c, c, 'auto', now() from unnest(%s::text[]) as c",
        (channels,),
    )
    db.execute(
        "insert into creators (platform, channel_id, handle, tier) values "
        "('youtube', 'UCrssblocked', 'blocked', 'blocked'), ('twitch', '777', 'twitchguy', 'auto')"
    )
    # a stream of the last creator in the rotation ended an hour ago
    server.youtube.feeds = {channel: [] for channel in channels}
    server.youtube.feeds["UCrss06"] = [
        FeedEntry("rssdone0001", "UCrss06", "GTA 5 RP marathon", NOW - timedelta(hours=5)),
        FeedEntry("rssother001", "UCrss06", "Minecraft with friends", NOW - timedelta(hours=5)),
    ]
    server.youtube.videos["rssdone0001"] = vod_item("rssdone0001", "UCrss06", title="GTA 5 RP marathon")

    read: list[list[str]] = []
    queued: list[int] = []
    for number in range(5):
        before = len(server.youtube.calls_to("channel_feed"))
        summary = server.run(10 * number)
        read.append([call["channel_id"] for call in server.youtube.calls_to("channel_feed")[before:]])
        queued.append(summary.videos_queued)
        assert source(summary, "youtube_rss")["status"] in ("ok", "empty")
    assert [sorted(batch) for batch in read[:3]] == [
        channels[0:3],
        channels[3:6],
        [*channels[0:2], channels[6]],
    ]
    assert set(read[0]) | set(read[1]) | set(read[2]) == set(channels)  # everyone within three passes
    assert all("UCrssblocked" not in batch and "777" not in batch for batch in read)
    assert sorted(read[3]) == channels[2:5] and sorted(read[4]) == [channels[0], *channels[5:7]]

    # the finished stream was found by the feed alone (pass 3) and went to the VOD lane
    assert queued == [0, 0, 1, 0, 0]
    video = db.fetch_one("select status, has_chat, duration_s from videos where source_id = 'rssdone0001'")
    assert video is not None
    assert (video["status"], video["has_chat"], video["duration_s"]) == ("new", True, 10800)
    assert count("jobs", "type = 'vod.process' and payload->>'source_id' = 'rssdone0001'") == 1
    assert count("candidates", "source_id = 'rssother001'") == 0


# ── known live streams ───────────────────────────────────────


def test_a_stream_that_ends_between_searches_becomes_a_video_for_the_vod_lane() -> None:
    server = Server()
    server.run()
    assert count("live_sessions", "status = 'assigned'") == 2

    # ten minutes later one stream has ended; no source lists it any more
    server.youtube.videos["ytlive00002"] = vod_item("ytlive00002", "UCyt2", title="GTA 5 live")
    second = server.run(10)
    assert (second.live_seen, second.videos_seen) == (1, 1)
    row = db.fetch_one(
        "select source_type, score_parts from candidates where source_id = 'ytlive00002' "
        "order by checked_at desc limit 1"
    )
    assert row is not None and row["source_type"] == "vod" and row["score_parts"]["origin"] == "recheck"
    assert second.videos_queued == 1 and count("videos", "source_id = 'ytlive00002'") == 1

    # without the Data API nothing can be said about a known stream: it is simply not seen
    settings.set("discovery.youtube_quota_daily_units", 0)
    third = server.run(20)
    assert third.live_seen == 0 and source(third, "youtube_api")["status"] == "skipped"

    settings.set("discovery.youtube_quota_daily_units", 3000)
    settings.set("discovery.live_recheck_max", 0)
    off = server.run(25)
    assert off.live_seen == 0  # switched off: only what a source lists in this pass


# ── language ─────────────────────────────────────────────────


def test_a_hindi_stream_marked_english_no_longer_takes_the_slot_from_an_english_one() -> None:
    server = Server()
    settings.set("live.max_slots", 1)
    server.youtube.search_live = [
        fake_search_item("hindi000001", "UChindi", "GTA 5 LIVE | Train vs 100 Luxury Cars", live=True),
        fake_search_item("english0001", "UCenglish", "ITS A PRIVILEGE! | Iconic RP", live=True),
    ]
    server.youtube.videos["hindi000001"] = live_item(
        "hindi000001",
        "UChindi",
        8477,
        title="GTA 5 LIVE | Train vs 100 Luxury Cars",
        channelTitle="Sunil Gaming",
        description="Daily live stream\n#gta5 #gta5hindi",
    )
    server.youtube.videos["english0001"] = live_item(
        "english0001",
        "UCenglish",
        6000,
        title="ITS A PRIVILEGE! | Iconic RP",
        channelTitle="Grizzley Gang",
        categoryId="20",  # a role-play stream that names the game nowhere: kept for its category
    )
    summary = server.run()
    top = {row["channel"]: row for row in summary.top_live}
    hindi, english = top["Sunil Gaming"], top["Grizzley Gang"]
    assert (hindi["language"], hindi["parts"]["language_signal"]) == ("und", "term:hindi")
    assert english["language"] == "en" and "language_signal" not in english["parts"]
    assert hindi["viewers"] > english["viewers"] and hindi["score"] < english["score"]
    assert (english["picked"], hindi["picked"]) == (True, False)
    # allowed, only ranked lower (decision D11): it is stored as a candidate like any other
    assert count("candidates", "source_id = 'hindi000001' and language = 'und'") == 1

    settings.set("discovery.non_english_terms", [])
    settings.set("live.max_slots", 2)
    plain = server.run(60)
    assert {row["channel"]: row["language"] for row in plain.top_live}["Sunil Gaming"] == "en"


# ── settings ─────────────────────────────────────────────────


def test_code_defaults_match_migration_0010() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for key, value in discovery_config.SOURCES_DEFAULTS.items():
        assert f"('{key}'," in sql, key
        assert settings.get(key) == value, key
    seeded = {line.split("'")[1] for line in sql.splitlines() if line.strip().startswith("('discovery.")}
    assert seeded == set(discovery_config.SOURCES_DEFAULTS)
    assert not seeded & set(discovery_config.DEFAULTS)
    # the three values the migration changes, and the code defaults that mirror them
    assert settings.get("spikes.ab_min_confidence") == 0.55
    assert settings.get("vod.fallback_below_spikes") == VOD_DEFAULTS["vod.fallback_below_spikes"] == 3
    assert settings.get("discovery.rss_max_channels_per_run") == 50
    assert discovery_config.DEFAULTS["discovery.rss_max_channels_per_run"] == 50
    config = discovery_config.DiscoveryConfig.from_settings()
    assert (config.search_api_mode, config.search_api_daily_calls, config.search_api_interval_s) == (
        "fallback",
        60,
        1800,
    )
    assert config.score.edited_upload_bonus == 6 and config.score.thin_chat_factor == 0.75


def test_the_migration_is_idempotent_and_keeps_values_set_by_hand(test_database_url: str) -> None:
    settings.set("spikes.ab_min_confidence", 0.7)
    settings.set("discovery.search_api_daily_calls", 20)
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
        conn.execute(MIGRATION.read_bytes())
    settings.invalidate()
    assert settings.get("spikes.ab_min_confidence") == 0.7
    assert settings.get("discovery.search_api_daily_calls") == 20
    assert settings.get("vod.fallback_below_spikes") == 3


# ── the doctor ───────────────────────────────────────────────


def doctor_deps(server: Server, **overrides: Any) -> doctor.DoctorDeps:
    def chat_check(video_id: str, access: Access) -> doctor.Check:
        return doctor.Check("chat_replay", doctor.OK, f"chat replay of {video_id} readable")

    def bot_check(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        return 1, "", BOT_CHECK

    values: dict[str, Any] = {
        "youtube": server.youtube,
        "twitch": server.twitch,
        "apify": FakeApifyClient(),
        "search": server.search,
        "access": Access(),
        "runner": bot_check,
        "chat_check": chat_check,
    }
    values.update(overrides)
    return doctor.DoctorDeps(**values)


def test_the_doctor_reports_the_server_as_it_is(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = Server()
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(tmp_path / "secrets" / "youtube_cookies.txt"))
    from sixstars import config as env_config

    env_config.env.cache_clear()
    checks = {check.name: check for check in doctor.run_doctor(deps=doctor_deps(server))}
    assert list(checks) == [
        "environment",
        "ytdlp_search",
        "data_api",
        "rss",
        "chat_replay",
        "window_download",
        "twitch_token",
        "apify",
    ]
    assert "PROXY_URL not set" in checks["environment"].detail
    assert "YOUTUBE_COOKIES_FILE names a file that does not exist" in checks["environment"].detail
    search = checks["ytdlp_search"]
    assert (search.status, search.error_class) == ("blocked", "YtDlpError")
    assert "Remote end closed connection without response" in search.detail
    assert checks["data_api"].status == "ok" and checks["rss"].status == "ok"
    window = checks["window_download"]
    assert (window.status, window.error_class) == ("blocked", "DownloadBlockedError")
    assert "Sign in to confirm you're not a bot" in window.detail
    assert checks["twitch_token"].status == "not configured"
    assert checks["apify"].status == "ok"
    # read-only and cheap: one search, one videos.list, one feed, no search.list, no Apify run
    assert len(server.search.calls) == 1 and server.search.calls[0]["limit"] == 5
    assert len(server.youtube.calls_to("videos_list")) == 1 and server.searches() == []
    assert len(server.youtube.calls_to("channel_feed")) == 1
    assert count("videos") == count("candidates") == count("jobs") == count("notify_outbox") == 0

    table = doctor.format_table(list(checks.values()))
    assert "ytdlp_search     BLOCKED" in table and "[DownloadBlockedError]" in table
    assert "Blocked from this machine: ytdlp_search, window_download" in table


def test_the_doctor_command_with_everything_working(monkeypatch: pytest.MonkeyPatch) -> None:
    server = Server()
    server.search.error = None
    server.twitch = FakeTwitchClient()
    db.execute(
        "insert into creators (platform, channel_id, handle, tier) values ('youtube', 'UCknown', 'k', 'auto')"
    )
    db.execute(
        "insert into videos (creator_id, platform, source_id, url, has_chat) "
        "select id, 'youtube', 'paststream1', 'https://youtu.be/paststream1', true from creators"
    )
    seen: dict[str, Any] = {}

    def download(url: str, section: Any, out_dir: Path, name: str, **kwargs: Any) -> Any:
        seen.update(url=url, section=section, max_height=kwargs["max_height"])
        from sixstars.vod.sections import Downloaded

        return Downloaded(out_dir / "doctor.mp4", 0.0, 5.0, 640, 360)

    monkeypatch.setattr(doctor.sections, "download_section", download)
    monkeypatch.setattr(doctor.DoctorDeps, "default", classmethod(lambda cls, config: doctor_deps(server)))
    result = CliRunner().invoke(app, ["discovery", "doctor", "--search-api"])
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    for name in ("ytdlp_search", "data_api", "data_api_search", "rss", "chat_replay", "window_download"):
        assert " OK " in lines[name], lines[name]
    assert " OK " in lines["twitch_token"] and "game id" in lines["twitch_token"]
    assert "Blocked from this machine" not in result.output
    # the known past stream and creator are what gets probed
    assert seen["url"].endswith("paststream1") and seen["section"].duration_s == 5.0
    assert server.youtube.calls_to("channel_feed")[0]["channel_id"] == "UCknown"
    assert "paststream1" in lines["chat_replay"]
    assert len(server.searches()) == 1 and server.searches()[0]["max_results"] == 5

    as_json = CliRunner().invoke(app, ["discovery", "doctor", "--json", "--video", "not a video"])
    assert as_json.exit_code == 2


def test_blocked_and_plain_errors_are_told_apart() -> None:
    assert doctor.looks_blocked(SERVER_ERROR) and doctor.looks_blocked(BOT_CHECK)
    assert not doctor.looks_blocked("ERROR: [youtube] abc: Video unavailable")
    blocked = doctor.failure("window_download", ytdlp.error_for(ytdlp.classify_failure(1, BOT_CHECK)))
    assert (blocked.status, blocked.error_class) == ("blocked", "DownloadBlockedError")
    gone = doctor.failure(
        "window_download", ytdlp.error_for(ytdlp.classify_failure(1, "ERROR: Private video"))
    )
    assert (gone.status, gone.error_class) == ("error", "VideoUnavailableError")
    api = doctor.failure("data_api", ApiError("youtube", "GET /youtube/v3/videos", 400, "API key not valid"))
    assert (api.status, api.error_class) == ("error", "ApiError 400")

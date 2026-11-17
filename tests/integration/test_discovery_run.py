"""Integration tests of the discovery pass against Postgres with fake clients: a pass creates creators,
candidates, live sessions and video jobs; a later pass computes growth from history; a repeated pass
duplicates nothing; Twitch without keys is skipped; forbidden creators never produce anything; the Apify
and quota guards hold."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import structlog
from typer.testing import CliRunner

from sixstars import db, jobs, settings
from sixstars.ai.schemas import PermissionVerdict
from sixstars.cli import app
from sixstars.clients._http import ApiError
from sixstars.clients.fakes import (
    FIXED_TIME,
    FakeAnthropicClient,
    FakeApifyClient,
    FakeTwitchClient,
    FakeYouTubeClient,
    fake_video,
)
from sixstars.clients.youtube import FeedEntry
from sixstars.discovery import config as discovery_config
from sixstars.discovery import run as discovery_run
from sixstars.discovery import sources, tiers
from sixstars.discovery.run import DiscoveryClients, DiscoverySummary, run_discovery
from sixstars.discovery.ytsearch import FakeYtDlpSearch, FlatVideo

NOW = FIXED_TIME + timedelta(hours=5)
MIGRATION = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0006_discovery.sql"
P6 = "p6_permission_read"
AUTO = {"tier": "auto", "evidence": None}


@pytest.fixture(autouse=True)
def clean_discovery(database: None, test_database_url: str) -> Iterator[None]:
    """Empty the tables discovery writes and use staging-like settings with two live slots."""
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("truncate creators cascade")
    settings.set("env.target_game", "Grand Theft Auto V")
    settings.set("env.search_terms", ["GTA 5"])
    settings.set("live.max_slots", 2)
    yield
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("truncate creators cascade")


def live_item(video_id: str, channel: str, viewers: int, title: str = "GTA 5 live") -> dict[str, Any]:
    return {
        "id": video_id,
        "snippet": {
            "publishedAt": "2026-11-17T14:00:00Z",
            "channelId": channel,
            "channelTitle": f"Name {channel}",
            "title": title,
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


def vod_item(
    video_id: str, channel: str, views: int = 200_000, title: str = "GTA 5 stream", duration: str = "PT3H"
) -> dict[str, Any]:
    return {
        "id": video_id,
        "snippet": {
            "publishedAt": "2026-11-17T08:00:00Z",
            "channelId": channel,
            "channelTitle": f"Name {channel}",
            "title": title,
            "liveBroadcastContent": "none",
            "defaultAudioLanguage": "en",
        },
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": str(views), "likeCount": str(views // 40), "commentCount": "200"},
        "liveStreamingDetails": {
            "actualStartTime": "2026-11-17T08:00:00Z",
            "actualEndTime": "2026-11-17T11:00:00Z",
        },
    }


class World:
    """Fake clients with a small, known world: 3 Twitch streams, 2 Twitch archives, 2 live YouTube streams
    and 3 finished YouTube videos (one of them too short)."""

    def __init__(self, twitch_keys: bool = True) -> None:
        self.twitch = FakeTwitchClient(configured=twitch_keys)
        self.twitch.videos = [replace(fake_video(i), stream_id=f"ended{i}") for i in range(2)]
        self.youtube = FakeYouTubeClient()
        self.apify = FakeApifyClient()
        self.search = FakeYtDlpSearch()
        self.anthropic = FakeAnthropicClient({P6: AUTO})
        self.search.default_live = [
            FlatVideo("ytlive00001", "GTA 5 live", "UCyt1", "Name UCyt1", "is_live", 5000, None, None),
            FlatVideo("ytlive00002", "GTA 5 live", "UCyt2", "Name UCyt2", "is_live", 1500, None, None),
        ]
        self.search.default_recent = [
            FlatVideo("ytvod000001", "GTA 5 stream", "UCyt3", "Name UCyt3", "was_live", None, 200_000, 10800),
            FlatVideo("ytvod000002", "GTA 5 stream", "UCyt4", "Name UCyt4", "was_live", None, 90_000, 10800),
            FlatVideo("ytshort0001", "GTA 5 short", "UCyt5", "Name UCyt5", None, None, 900_000, 45),
        ]
        self.youtube.videos = {
            "ytlive00001": live_item("ytlive00001", "UCyt1", 5000),
            "ytlive00002": live_item("ytlive00002", "UCyt2", 1500),
            "ytvod000001": vod_item("ytvod000001", "UCyt3"),
            "ytvod000002": vod_item("ytvod000002", "UCyt4", views=90_000),
            "ytshort0001": vod_item("ytshort0001", "UCyt5", views=900_000, duration="PT45S"),
        }

    @property
    def clients(self) -> DiscoveryClients:
        return DiscoveryClients(self.twitch, self.youtube, self.apify, self.search, self.anthropic)

    def run(self, at: datetime = NOW) -> DiscoverySummary:
        settings.invalidate()
        return run_discovery(self.clients, now=at)


def count(table: str, where: str = "true") -> int:
    row = db.fetch_one(f"select count(*) as n from {table} where {where}")
    assert row is not None
    return int(row["n"])


def one(sql: str, *params: Any) -> dict[str, Any]:
    row = db.fetch_one(sql, params)
    assert row is not None
    return row


# ── the acceptance flow ──────────────────────────────────────


def test_pass_creates_creators_candidates_sessions_and_video_jobs() -> None:
    world = World()
    summary = world.run()

    assert summary.errors == []
    assert (summary.live_seen, summary.videos_seen) == (5, 4)
    assert summary.creators_seen == summary.new_creators == 7  # 3 Twitch users + 4 YouTube channels
    assert summary.skipped == {"too_short": 1}
    assert count("creators") == 7
    assert count("candidates") == summary.candidates_stored == 9
    assert count("candidates", "source_type = 'live'") == 5

    # live slots: the two biggest streams (Twitch 10,000 and 9,900 viewers), status assigned
    sessions = db.fetch_all(
        "select platform, source_id, channel_login, status from live_sessions order by source_id"
    )
    assert [(s["platform"], s["source_id"], s["status"]) for s in sessions] == [
        ("twitch", "stream0", "assigned"),
        ("twitch", "stream1", "assigned"),
    ]
    assert sessions[0]["channel_login"] == "streamer0"
    assert (summary.sessions_assigned, summary.sessions_active) == (2, 2)

    # every finished video above the cutoff is queued once, with a vod.process job
    videos = db.fetch_all(
        "select id::text as id, platform, source_id, status, has_chat, duration_s from videos"
    )
    assert {(v["platform"], v["source_id"]) for v in videos} == {
        ("twitch", "video0"),
        ("twitch", "video1"),
        ("youtube", "ytvod000001"),
        ("youtube", "ytvod000002"),
    }
    assert all(v["status"] == "new" and v["has_chat"] for v in videos)
    queued = db.fetch_all("select * from jobs where type = 'vod.process'")
    assert len(queued) == summary.videos_queued == 4
    by_dedupe = {job["dedupe_key"]: job for job in queued}
    for video in videos:
        job = by_dedupe[f"vod:{video['id']}"]
        assert (job["queue"], job["priority"], job["status"]) == ("normal", 30, "queued")
        assert job["payload"]["video_id"] == video["id"]
        assert job["payload"]["source_id"] == video["source_id"]
        assert job["payload"]["url"].startswith("https://")

    # the creators that were picked had their permission checked first; picked rows are flagged
    assert summary.permission_checked == 6
    assert count("creators", "description_checked_at is not null") == summary.permission_checked
    assert count("candidates", "picked") == 6
    assert count("events", "type = 'video.queued'") == 4
    assert count("events", "type = 'live.assigned'") == 2
    event = one("select level, data from events where type = 'discovery.run'")
    assert event["level"] == "info"
    assert event["data"]["live_seen"] == 5 and event["data"]["videos_queued"] == 4
    assert "top_live" not in event["data"]
    assert count("heartbeats", "name = 'trigger:discovery'") == 1

    # summary for the HTTP route
    body = summary.to_dict()
    assert body["started_at"] == NOW.isoformat()
    assert [row["viewers"] for row in body["top_live"][:2]] == [10000, 9900]
    assert body["top_live"][0]["picked"] is True
    assert len(body["top_videos"]) == 4 and body["youtube_quota_units"] == 2
    assert {source["name"]: source["status"] for source in body["sources"]} == {
        "twitch": "ok",
        "youtube_search": "ok",
        "youtube_rss": "skipped",
        "apify": "ok",
        "youtube_api": "ok",
    }


def test_second_pass_ten_minutes_later_computes_growth_and_duplicates_nothing() -> None:
    world = World()
    world.run()
    first = one("select score_parts from candidates where source_id = 'ytlive00001'")
    assert first["score_parts"]["growth"] == 0.5  # no history yet

    world.youtube.videos["ytlive00001"] = live_item("ytlive00001", "UCyt1", 7500)  # +50 %
    world.youtube.videos["ytlive00002"] = live_item("ytlive00002", "UCyt2", 750)  # halved
    summary = world.run(NOW + timedelta(minutes=10))

    rows = db.fetch_all(
        "select source_id, viewers, score_parts from candidates where platform = 'youtube' "
        "and source_type = 'live' and checked_at = %s order by source_id",
        (NOW + timedelta(minutes=10),),
    )
    assert [(row["viewers"], row["score_parts"]["growth"]) for row in rows] == [(7500, 0.5), (750, 0.0)]
    twitch = one(
        "select score_parts from candidates where source_id = 'stream0' order by checked_at desc limit 1"
    )
    assert twitch["score_parts"]["growth"] == 0.0  # unchanged audience

    assert summary.new_creators == 0 and count("creators") == 7
    # a second observation per source, except the two YouTube videos that are already queued
    assert count("candidates") == 16
    assert summary.videos_queued == 0 and count("videos") == 4
    assert count("jobs", "type = 'vod.process'") == 4
    assert summary.skipped["already_queued"] == 2  # the YouTube videos are not even looked up again
    assert (summary.sessions_assigned, summary.sessions_active) == (0, 2)
    assert count("live_sessions") == 2
    assert summary.permission_checked == 0


def test_pass_repeated_at_once_is_idempotent() -> None:
    world = World()
    world.run()
    before = {table: count(table) for table in ("creators", "candidates", "videos", "live_sessions", "jobs")}
    summary = world.run(NOW + timedelta(seconds=30))
    after = {table: count(table) for table in before}
    assert after == before
    assert summary.candidates_stored == 0
    refreshed = one("select checked_at from candidates where source_id = 'stream0'")
    assert refreshed["checked_at"] == NOW + timedelta(seconds=30)


def test_twitch_without_keys_is_skipped_cleanly() -> None:
    world = World(twitch_keys=False)
    with structlog.testing.capture_logs() as logs:
        summary = world.run()
    assert summary.errors == []
    assert summary.sources[0] == {
        "name": "twitch",
        "status": "not_configured",
        "found": 0,
        "detail": "no Twitch keys",
    }
    assert world.twitch.calls == []
    assert (summary.live_seen, summary.videos_seen) == (2, 2)
    assert count("creators", "platform = 'twitch'") == 0
    assert count("live_sessions", "platform = 'youtube'") == 2
    assert count("notify_outbox") == 0
    assert count("events", "level in ('warn', 'error', 'critical')") == 0
    skipped = [entry for entry in logs if entry["event"] == "discovery_twitch_skipped"]
    assert len(skipped) == 1 and skipped[0]["log_level"] == "info"
    assert not [entry for entry in logs if entry["log_level"] in ("warning", "error")]

    # keys arrive: the next pass uses Twitch with no other change
    world.twitch.configured = True
    later = world.run(NOW + timedelta(minutes=10))
    assert later.sources[0]["status"] == "ok"
    assert count("creators", "platform = 'twitch'") == 3


def test_failing_sources_do_not_fail_the_pass() -> None:
    world = World()
    world.search.error = RuntimeError("yt-dlp exploded")

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise ApiError("twitch", "GET /helix/streams", 503, "down")

    world.twitch.get_streams = broken  # type: ignore[method-assign]
    summary = world.run()
    assert [error.split(":")[0] for error in summary.errors] == ["twitch", "youtube_search"]
    assert (summary.live_seen, summary.videos_seen) == (0, 0)
    assert one("select level from events where type = 'discovery.run'")["level"] == "warn"
    assert one("select severity, title from notify_outbox")["title"] == "Discovery found nothing"


# ── creator tiers ────────────────────────────────────────────


def test_forbidden_creators_never_produce_a_candidate_a_session_or_a_job() -> None:
    world = World()
    world.youtube.channels["UCyt1"] = {
        "id": "UCyt1",
        "snippet": {"title": "Name UCyt1", "description": "Do NOT clip or reupload my content."},
        "statistics": {"subscriberCount": "50000"},
    }

    class Reader(FakeAnthropicClient):
        """Answers like P6 would: restricted when the channel text forbids clipping."""

        def complete_json(
            self, prompt_id: str, system: str, user_content: Any, schema: Any, *a: Any, **k: Any
        ) -> Any:
            if "Do NOT clip" in str(user_content):
                return schema.model_validate({"tier": "restricted", "evidence": "Do NOT clip or reupload"})
            return schema.model_validate(AUTO)

    world.anthropic = Reader()
    settings.set("discovery.excluded_channels", ["twitch:streamer0", "UCyt3"])
    db.execute(
        "insert into creators (platform, channel_id, handle, tier, tier_reason) "
        "values ('twitch', 'user1', 'streamer1', 'blocked', 'takedown request')"
    )
    summary = world.run()

    tiers_now = {
        row["channel_id"]: (row["tier"], row["tier_reason"])
        for row in db.fetch_all("select channel_id, tier, tier_reason from creators")
    }
    assert tiers_now["user0"] == ("blocked", tiers.EXCLUDED_REASON)
    assert tiers_now["UCyt3"] == ("blocked", tiers.EXCLUDED_REASON)
    assert tiers_now["user1"] == ("blocked", "takedown request")
    assert tiers_now["UCyt1"] == ("restricted", "Do NOT clip or reupload")
    forbidden = "creator_id in (select id from creators where tier in ('restricted', 'blocked'))"
    assert count("candidates", forbidden) == 0
    assert count("videos", forbidden) == 0
    assert count("live_sessions", forbidden) == 0
    # excluded: stream0, video0, ytvod000001; blocked before: stream1, video1; restricted now: ytlive00001
    assert summary.skipped["forbidden_creator"] == 6
    assert {s["source_id"] for s in db.fetch_all("select source_id from live_sessions")} == {
        "stream2",
        "ytlive00002",
    }
    assert count("events", "type = 'creator.blocked'") == 2
    assert (
        one("select level, data from events where type = 'creator.tier_changed'")["data"]["new"]
        == "restricted"
    )

    # later passes keep them out, and blocked creators are never asked about again
    world.run(NOW + timedelta(minutes=10))
    assert count("candidates", forbidden) == 0 and count("live_sessions", forbidden) == 0
    blocked_jobs = (
        "type = 'creator.permission' and payload->>'creator_id' in "
        "(select id::text from creators where tier = 'blocked')"
    )
    assert count("jobs", blocked_jobs) == 0


def test_creators_are_not_picked_before_their_permission_check() -> None:
    world = World(twitch_keys=False)
    world.anthropic = FakeAnthropicClient({P6: RuntimeError("model down")})
    summary = world.run()
    assert (summary.sessions_assigned, summary.videos_queued, summary.permission_checked) == (0, 0, 0)
    assert count("candidates") == 4 and count("videos") == 0 and count("live_sessions") == 0
    # every unchecked creator has one queued creator.permission job (normal queue, priority 20)
    queued = db.fetch_all(
        "select queue, priority, payload, dedupe_key from jobs where type = 'creator.permission'"
    )
    assert len(queued) == summary.permission_jobs == 4
    assert all((job["queue"], job["priority"]) == ("normal", 20) for job in queued)
    assert all(job["dedupe_key"] == f"creator.permission:{job['payload']['creator_id']}" for job in queued)

    # the same pass again queues no second job per creator; a working check then lets them through
    world.anthropic = FakeAnthropicClient({P6: {"tier": "preferred", "evidence": "clips welcome"}})
    world.youtube.videos["ytlive00001"] = live_item("ytlive00001", "UCyt1", 7500)
    world.youtube.videos["ytlive00002"] = live_item("ytlive00002", "UCyt2", 2250)
    later = world.run(NOW + timedelta(minutes=10))
    assert count("jobs", "type = 'creator.permission'") == 4
    assert (later.sessions_assigned, later.videos_queued, later.permission_checked) == (2, 2, 4)
    top = later.top_live[0]
    assert top["tier"] == "preferred" and top["checked"] is True
    stored = one(
        "select trend_score from candidates where source_id = 'ytlive00001' order by checked_at desc limit 1"
    )
    assert float(stored["trend_score"]) == top["score"]  # the preferred bonus is in the stored score


def test_permission_job_restricts_a_creator_and_retires_their_work(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(twitch_keys=False)
    world.run()
    creator = one("select id::text as id from creators where channel_id = 'UCyt3'")
    video = one("select id::text as id from videos where source_id = 'ytvod000001'")
    assert count("jobs", f"dedupe_key = 'vod:{video['id']}' and status = 'queued'") == 1

    monkeypatch.setattr(tiers, "YouTubeClient", lambda: world.youtube)
    monkeypatch.setattr(tiers, "TwitchClient", lambda: world.twitch)
    monkeypatch.setattr(
        tiers.calls,
        "read_permission",
        lambda *a, **k: PermissionVerdict(tier="restricted", evidence="no clips please"),
    )
    job_id = tiers.enqueue_permission_job(creator["id"])
    assert job_id is not None
    job = jobs.get(job_id)
    assert job is not None and job.payload == {"creator_id": creator["id"]}
    tiers.handle_permission_job(job)

    row = one("select tier, tier_reason from creators where id = %s", creator["id"])
    assert (row["tier"], row["tier_reason"]) == ("restricted", "no clips please")
    skipped = one("select status, skip_reason from videos where id = %s", video["id"])
    assert (skipped["status"], skipped["skip_reason"]) == ("skipped", "creator forbids clipping")
    assert count("jobs", f"dedupe_key = 'vod:{video['id']}' and status = 'queued'") == 0
    assert one("select data from events where type = 'creator.tier_changed'")["data"]["videos_skipped"] == 1

    # a manual tier set by the owner survives an ordinary check, and the CLI can block
    runner = CliRunner()
    result = runner.invoke(
        app, ["discovery", "set-tier", "youtube", "UCyt4", "blocked", "--reason", "asked us"]
    )
    assert result.exit_code == 0 and "blocked" in result.output
    blocked = one("select tier, tier_reason, blocked_at from creators where channel_id = 'UCyt4'")
    assert (blocked["tier"], blocked["tier_reason"]) == ("blocked", "manual: asked us")
    assert blocked["blocked_at"] is not None
    assert one("select status from videos where source_id = 'ytvod000002'")["status"] == "skipped"
    assert runner.invoke(app, ["discovery", "set-tier", "youtube", "nobody", "blocked"]).exit_code == 1


# ── live slots over time ─────────────────────────────────────


def test_slot_is_released_after_staying_below_the_cutoff() -> None:
    world = World(twitch_keys=False)
    settings.set("live.max_slots", 1)
    world.run()
    session = one("select source_id, below_cutoff_since from live_sessions")
    assert (session["source_id"], session["below_cutoff_since"]) == ("ytlive00001", None)

    world.youtube.videos["ytlive00001"] = live_item("ytlive00001", "UCyt1", 60)  # the audience left
    world.youtube.videos["ytlive00002"] = live_item("ytlive00002", "UCyt2", 6000)
    drop = NOW + timedelta(minutes=10)
    summary = world.run(drop)
    session = one("select status, below_cutoff_since from live_sessions where source_id = 'ytlive00001'")
    assert (session["status"], session["below_cutoff_since"]) == ("assigned", drop)
    assert (summary.sessions_released, summary.sessions_assigned) == (0, 0)  # the slot is still taken

    world.run(NOW + timedelta(minutes=20))
    assert one("select status from live_sessions where source_id = 'ytlive00001'")["status"] == "assigned"

    summary = world.run(NOW + timedelta(minutes=30))  # 20 minutes below the cutoff
    assert (summary.sessions_released, summary.sessions_assigned) == (1, 1)
    statuses = {
        row["source_id"]: row["status"] for row in db.fetch_all("select source_id, status from live_sessions")
    }
    assert statuses == {"ytlive00001": "released", "ytlive00002": "assigned"}
    assert one("select data from events where type = 'live.released'")["data"]["reason"] == "below_cutoff"


# ── cost guards ──────────────────────────────────────────────


def test_apify_runs_rarely_inside_its_budget_and_is_read_on_a_later_pass() -> None:
    world = World(twitch_keys=False)
    first = world.run()
    assert len(world.apify.calls_to("start_run")) == 1
    start = world.apify.calls_to("start_run")[0]
    assert start["input"]["isLive"] is True and start["input"]["maxResultStreams"] == 5
    assert start["max_charge_usd"] == pytest.approx(1 * 5 * 0.003)
    assert world.apify.calls_to("wait_for_run") == []  # never blocks a pass
    assert first.apify_cost_usd == 0.0
    state = settings.get("discovery.apify_last_run")
    assert state["run_id"] == "run1" and state["collected"] is False and state["mode"] == "live"

    # the next pass reads the finished run: its videos join the pool and its cost is recorded
    world.apify.runs["run1"] = replace(world.apify.runs["run1"], cost_usd=0.012)
    found = {"id": "apifylive01", "title": "GTA 5 RP", "streamType": "live", "viewCount": 3000}
    found |= {"channelId": "UCapify", "channelName": "Apify Find", "numberOfSubscribers": 70000}
    world.apify.datasets["dataset-run1"] = [found, {"error": "NO_VIDEOS"}]
    world.youtube.videos["apifylive01"] = live_item("apifylive01", "UCapify", 3000)
    second = world.run(NOW + timedelta(minutes=10))
    assert second.apify_cost_usd == pytest.approx(0.012)
    assert second.apify_spent_today_usd == pytest.approx(0.012)
    assert count("candidates", "source_id = 'apifylive01'") == 1
    assert count("creators", "channel_id = 'UCapify'") == 1
    usage = one("select operation, cost_usd, ref_id from api_usage where provider = 'apify'")
    assert (usage["operation"], float(usage["cost_usd"]), usage["ref_id"]) == (
        "discovery_search",
        0.012,
        "run1",
    )
    assert len(world.apify.calls_to("start_run")) == 1  # interval not reached

    # after the interval the next run is a recent-videos search
    third = world.run(NOW + timedelta(hours=6, minutes=1))
    assert len(world.apify.calls_to("start_run")) == 2
    assert world.apify.calls_to("start_run")[1]["input"].get("isLive") is None
    assert "started recent run" in next(s["detail"] for s in third.sources if s["name"] == "apify")

    # over the daily budget nothing starts, and discovery still works
    db.execute("insert into api_usage (provider, operation, cost_usd) values ('apify', 'other', 0.39)")
    fourth = world.run(NOW + timedelta(hours=13))
    assert len(world.apify.calls_to("start_run")) == 2
    apify = next(s for s in fourth.sources if s["name"] == "apify")
    assert "daily budget" in apify["detail"]
    assert fourth.errors == [] and fourth.live_seen >= 2


def test_youtube_quota_budget_falls_back_to_the_search_listing() -> None:
    world = World(twitch_keys=False)
    settings.set("discovery.youtube_quota_daily_units", 100)
    db.execute("insert into api_usage (provider, operation, units) values ('youtube', 'videos.list', 100)")
    summary = world.run()
    assert world.youtube.calls_to("videos_list") == []
    assert summary.youtube_quota_units == 0
    api = next(s for s in summary.sources if s["name"] == "youtube_api")
    assert api["status"] == "skipped" and "daily quota budget" in api["detail"]
    assert (summary.live_seen, summary.videos_seen) == (2, 2)
    live = one("select viewers, started_at from candidates where source_id = 'ytlive00001'")
    assert (live["viewers"], live["started_at"]) == (5000, None)


def test_known_creators_are_watched_through_rss_and_off_topic_uploads_are_ignored() -> None:
    world = World(twitch_keys=False)
    world.run()
    fresh = NOW + timedelta(minutes=5)
    world.youtube.feeds = {channel: [] for channel in ("UCyt1", "UCyt2", "UCyt3", "UCyt4")}
    world.youtube.feeds["UCyt3"] = [
        FeedEntry("rssgta00001", "UCyt3", "GTA 5 heist stream", fresh),
        FeedEntry("rssother001", "UCyt3", "Minecraft with friends", fresh),
        FeedEntry("rssold00001", "UCyt3", "GTA 5 from last month", NOW - timedelta(days=30)),
    ]
    world.youtube.videos["rssgta00001"] = vod_item(
        "rssgta00001", "UCyt3", views=50_000, title="GTA 5 heist stream"
    )
    summary = world.run(NOW + timedelta(minutes=10))
    rss = next(s for s in summary.sources if s["name"] == "youtube_rss")
    assert (rss["status"], rss["found"], rss["detail"]) == ("ok", 1, "4 feeds")
    assert count("candidates", "source_id = 'rssgta00001'") == 1
    assert count("candidates", "source_id in ('rssother001', 'rssold00001')") == 0
    assert settings.get("discovery.rss_cursor") == "UCyt4"


def test_low_queue_widens_the_run_and_the_backlog_cap_stops_it() -> None:
    world = World(twitch_keys=False)
    settings.set("discovery.vod_score_cutoff", 95)
    summary = world.run()
    assert (summary.vod_widened, summary.vod_cutoff, summary.vod_max) == (True, 85, 12)
    assert summary.videos_queued == 0  # nothing scores 85

    settings.set("discovery.vod_score_cutoff", 50)
    settings.set("discovery.max_vod_backlog", 1)
    capped = world.run(NOW + timedelta(minutes=10))
    assert (capped.vod_max, capped.videos_queued) == (1, 1)
    full = world.run(NOW + timedelta(minutes=20))
    assert (full.vod_max, full.videos_queued) == (0, 0) and count("videos") == 1


# ── settings and CLI ─────────────────────────────────────────


def test_code_defaults_match_the_migration() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for key, value in discovery_config.DEFAULTS.items():
        assert f"('{key}'," in sql, key
        assert settings.get(key) == value, key
    seeded = {line.split("'")[1] for line in sql.splitlines() if line.strip().startswith("('discovery.")}
    assert seeded == set(discovery_config.DEFAULTS)
    assert sources.APIFY_STATE_KEY not in discovery_config.DEFAULTS  # seeded by migration 0002


def test_cli_run_once_prints_summary_and_top_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    monkeypatch.setattr(
        discovery_run.DiscoveryClients, "default", classmethod(lambda cls, config: world.clients)
    )
    result = CliRunner().invoke(app, ["discovery", "run-once"])
    assert result.exit_code == 0, result.output
    assert "Discovery for Grand Theft Auto V" in result.output
    assert "Top 5 live streams" in result.output and "videos\n" in result.output
    assert "Streamer0" in result.output and "PICKED" in result.output
    assert "twitch ok 5" in result.output

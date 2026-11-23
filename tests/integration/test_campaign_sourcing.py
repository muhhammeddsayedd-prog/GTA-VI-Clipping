"""Campaign sourcing against the real schema (D118, PR 2): the watcher gives a live campaign stream a slot
(preempting the weakest trending session when the lane is full), re-tags instead of restarting, queues the
archive at campaign priority when a streamer without a slot goes offline, and new clips get their
campaign tag."""

from datetime import UTC, datetime, timedelta

import pytest
from posting_support import seed

from sixstars import db, settings
from sixstars.campaigns import service, tagging, watcher
from sixstars.clients.fakes import FIXED_TIME, FakeTwitchClient, FakeYouTubeClient, fake_stream, fake_video

NOW = FIXED_TIME + timedelta(hours=1)  # 13:00 UTC: inside campaign hours


@pytest.fixture(autouse=True)
def clean() -> None:
    db.execute(
        "truncate clip_campaigns, clip_campaign_creators, lane_quotas, creator_live_status, live_sessions, "
        "videos, jobs, posts, clips, spikes, creators, platform_accounts restart identity cascade"
    )
    seed("production")
    settings.set("live.max_slots", 3)
    watcher._feed_checked.clear()
    watcher._settled.clear()


def add_creator(channel_id: str, handle: str) -> str:
    row = db.fetch_one(
        "insert into creators (platform, channel_id, handle, tier) values ('twitch', %s, %s, 'auto') "
        "returning id::text as id",
        (channel_id, handle),
    )
    assert row is not None
    return str(row["id"])


def activate(slug: str, creator_id: str, game: str | None = None) -> str:
    campaign = service.apply_change(
        slug,
        {
            "name": slug,
            "rate_per_1k": 1.0,
            "budget_total": 1000,
            "budget_left": 1000,
            "ends_at": datetime.now(UTC) + timedelta(days=3),
            "platforms": ["youtube", "instagram"],
            "creator_ids": [creator_id],
            "quota": {"youtube": "1-2", "instagram": "1-4"},
            "required_game": game,
        },
        "test",
        create=True,
    )
    service.set_status(slug, "active", "test")
    assert campaign.id is not None
    return campaign.id


def add_session(creator_id: str, source_id: str, score: float, campaign_id: str | None = None) -> str:
    row = db.fetch_one(
        "insert into live_sessions (creator_id, platform, source_id, channel_login, url, status, "
        "trend_score, "
        "campaign_id) values (%s, 'twitch', %s, %s, 'https://twitch.tv/x', 'recording', %s, %s) "
        "returning id::text as id",
        (creator_id, source_id, source_id, score, campaign_id),
    )
    assert row is not None
    return str(row["id"])


def sessions() -> dict[str, dict[str, object]]:
    rows = db.fetch_all(
        "select id::text as id, source_id, status, campaign_id::text as campaign_id from live_sessions"
    )
    return {str(row["source_id"]): row for row in rows}


def twitch_with(*indexes: int) -> FakeTwitchClient:
    client = FakeTwitchClient()
    client.streams = [fake_stream(index) for index in indexes]
    return client


def test_a_live_campaign_streamer_gets_a_slot_and_a_status_row() -> None:
    creator = add_creator("user0", "streamer0")
    campaign_id = activate("live-one", creator)
    summary = watcher.run_once(twitch_with(0), FakeYouTubeClient(), NOW)
    assert summary["started"] == 1
    row = sessions()["stream0"]
    assert (row["status"], row["campaign_id"]) == ("assigned", campaign_id)
    status = db.fetch_one(
        "select live, game, stream_id from creator_live_status where creator_id = %s", (creator,)
    )
    assert status is not None and status["live"] and status["stream_id"] == "stream0"
    beat = db.fetch_one("select name from heartbeats where name = 'campaign:watcher'")
    assert beat is not None
    # A second pass changes nothing.
    assert watcher.run_once(twitch_with(0), FakeYouTubeClient(), NOW)["started"] == 0
    assert len(sessions()) == 1


def test_a_full_lane_releases_the_weakest_trending_session() -> None:
    creator = add_creator("user0", "streamer0")
    activate("full-lane", creator)
    others = [add_creator(f"t{i}", f"trend{i}") for i in range(3)]
    for i, other in enumerate(others):
        add_session(other, f"trend-stream{i}", 60.0 + i)
    watcher.run_once(twitch_with(0), FakeYouTubeClient(), NOW)
    rows = sessions()
    assert rows["trend-stream0"]["status"] == "released"
    assert rows["stream0"]["status"] == "assigned"


def test_a_running_trending_session_is_retagged_not_restarted() -> None:
    creator = add_creator("user0", "streamer0")
    campaign_id = activate("retag", creator)
    session_id = add_session(creator, "stream0", 40.0)
    summary = watcher.run_once(twitch_with(0), FakeYouTubeClient(), NOW)
    assert (summary["started"], summary["retagged"]) == (0, 1)
    row = sessions()["stream0"]
    assert (row["id"], row["status"], row["campaign_id"]) == (session_id, "recording", campaign_id)


def test_wrong_game_means_no_slot() -> None:
    creator = add_creator("user0", "streamer0")
    activate("gta6-only", creator, game="Grand Theft Auto VI")  # the fake stream plays GTA V
    assert watcher.run_once(twitch_with(0), FakeYouTubeClient(), NOW)["started"] == 0
    assert sessions() == {}


def test_offline_without_a_slot_queues_the_archive_at_campaign_priority() -> None:
    creator = add_creator("user0", "streamer0")
    campaign_id = activate("vod-after", creator)
    settings.set("live.campaign_slots", 0)  # no slot: the stream is only watched
    twitch = twitch_with(0)
    assert watcher.run_once(twitch, FakeYouTubeClient(), NOW)["overflow"] == 1
    twitch.streams = []
    twitch.videos = [fake_video(0)]
    summary = watcher.run_once(twitch, FakeYouTubeClient(), NOW)
    assert summary["vods_queued"] == 1
    video = db.fetch_one(
        "select id::text as id, campaign_id::text as campaign_id from videos where source_id = 'video0'"
    )
    assert video is not None and video["campaign_id"] == campaign_id
    job = db.fetch_one("select priority from jobs where type = 'vod.process'")
    assert job is not None and job["priority"] == 35


def test_paused_campaign_untags_its_session() -> None:
    creator = add_creator("user0", "streamer0")
    activate("pause-me", creator)
    watcher.run_once(twitch_with(0), FakeYouTubeClient(), NOW)
    service.set_status("pause-me", "paused", "test")
    watcher.run_once(twitch_with(0), FakeYouTubeClient(), NOW)
    assert sessions()["stream0"]["campaign_id"] is None


def test_new_clips_get_the_campaign_tag() -> None:
    creator = add_creator("user0", "streamer0")
    campaign_id = activate("tag-me", creator)
    tag = tagging.tag_for(creator, "hourly", NOW)
    assert tag is not None and (tag.campaign_id, tag.render_profile) == (campaign_id, "brand-disclose")
    assert tagging.tag_for(creator, "spike", NOW.replace(hour=23)) is None  # night spike stays Normal
    assert tagging.tag_for(creator, "spike", NOW) is not None
    service.set_status("tag-me", "paused", "test")
    assert tagging.tag_for(creator, "hourly", NOW) is None


def test_one_moment_once_viewer_clips_see_live_clipped_ranges() -> None:
    from sixstars.viewer_clips import store as clips_store

    creator = add_creator("user0", "streamer0")
    db.execute(
        "insert into live_sessions (creator_id, platform, source_id, channel_login, url, status, started_at, "
        "vod_source_id, clipped_ranges) "
        "values (%s, 'twitch', 'stream0', 'streamer0', 'u', 'replay_done', %s, "
        "'video0', %s::jsonb)",
        (
            creator,
            FIXED_TIME,
            '[{"start_wall": "2026-11-17T12:10:00+00:00", "end_wall": "2026-11-17T12:10:30+00:00"}]',
        ),
    )
    taken = clips_store.taken_ranges({"video0"})
    assert taken["video0"] == [(600.0, 630.0)]

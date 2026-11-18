"""Console media against Postgres: creator pictures (`discovery.media.fill_avatars`), source thumbnails on
discovery rows, and the one-time backfill (`sixstars console-media-backfill`)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from posting_support import add_clip, add_creator, production, video

from sixstars import console_media, db
from sixstars.clients.fakes import FakeTwitchClient, FakeYouTubeClient, fake_clip
from sixstars.discovery import media
from sixstars.discovery import store as discovery_store
from sixstars.discovery.models import Candidate
from sixstars.storage import FakeClipStorage

__all__ = ["production", "video"]  # fixtures

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
PARAMS = media.AvatarParams(refresh_days=7, per_pass=500)


def creator_row(creator_id: str) -> dict[str, object]:
    row = db.fetch_one("select avatar_url, avatar_checked_at from creators where id = %s", (creator_id,))
    assert row is not None
    return row


def test_fill_avatars_both_platforms(production: None) -> None:
    tw = add_creator("AvatarTwitch", platform="twitch")
    yt = add_creator("AvatarYoutube", platform="youtube")
    twitch, youtube = FakeTwitchClient(), FakeYouTubeClient()
    result = media.fill_avatars(twitch, youtube, NOW, quota_ok=lambda units: True, params=PARAMS)
    assert creator_row(tw) == {
        "avatar_url": "https://example.test/avatar/chan-AvatarTwitch.png",
        "avatar_checked_at": NOW,
    }
    assert creator_row(yt)["avatar_url"] == "https://yt.example.test/chan-AvatarYoutube/240.jpg"
    assert result.youtube_units >= 1 and result.found["twitch"] >= 1
    # Checked now: not asked again until the refresh age has passed.
    again = media.fill_avatars(
        twitch, youtube, NOW + timedelta(days=1), quota_ok=lambda u: True, params=PARAMS
    )
    assert again.checked == {}
    later = media.fill_avatars(
        twitch, youtube, NOW + timedelta(days=8), quota_ok=lambda u: True, params=PARAMS
    )
    assert later.checked.get("twitch", 0) >= 1


def test_fill_avatars_marks_missing_and_respects_quota(production: None) -> None:
    yt = add_creator("NoQuota", platform="youtube")
    tw = add_creator("GoneUser", platform="twitch")
    twitch = FakeTwitchClient()
    twitch.get_users = lambda ids: []  # type: ignore[method-assign]  # banned / unknown users
    result = media.fill_avatars(twitch, FakeYouTubeClient(), NOW, quota_ok=lambda units: False, params=PARAMS)
    assert creator_row(tw) == {"avatar_url": None, "avatar_checked_at": NOW}, "checked even with no picture"
    assert creator_row(yt)["avatar_checked_at"] is None, "no quota: left for a later pass"
    assert "youtube: quota budget used up" in result.errors


def test_save_thumbnails_updates_videos_and_sessions(production: None) -> None:
    creator = add_creator("ThumbStreamer", platform="twitch")
    db.execute(
        "insert into videos (creator_id, platform, source_id, url) values (%s, 'twitch', 'v-thumb', 'u')",
        (creator,),
    )
    db.execute(
        "insert into live_sessions (creator_id, platform, source_id, channel_login, url, status) "
        "values (%s, 'twitch', 's-thumb', 'x', 'u', 'assigned')",
        (creator,),
    )

    def candidate(source_id: str, url: str) -> Candidate:
        return Candidate("twitch", "vod", source_id, "u", "t", "c", "h", "n", "twitch", thumbnail_url=url)

    changed = discovery_store.save_thumbnails(
        [candidate("v-thumb", "https://a"), candidate("s-thumb", "https://b")]
    )
    assert changed == 2
    assert discovery_store.save_thumbnails([candidate("v-thumb", "https://a")]) == 0, "unchanged: no write"
    row = db.fetch_one("select thumbnail_url from videos where source_id = 'v-thumb'")
    assert row is not None and row["thumbnail_url"] == "https://a"


def test_backfill(production: None, video: Path) -> None:
    creator = add_creator("BackfillStreamer", platform="twitch")
    clip = add_clip(creator, video)  # the local file is not a real video: the frame fails, nothing breaks
    no_file = add_clip(creator, None)
    db.execute("update clips set storage_path = null where id = %s", (no_file,))
    db.execute(
        "insert into videos (creator_id, platform, source_id, url) values "
        "(%s, 'youtube', 'ytbackfill1', 'u'), (%s, 'twitch', 'video1', 'u')",
        (creator, creator),
    )
    db.execute(
        "insert into viewer_clips (source_id, creator_id, url) values ('FakeClipSlug1', %s, 'u')", (creator,)
    )
    twitch = FakeTwitchClient()
    twitch.clips = [fake_clip(1)]
    result = console_media.backfill(
        50, twitch=twitch, youtube=FakeYouTubeClient(), storage=FakeClipStorage(), now=NOW
    )
    assert result["frames"]["failed"] >= 1
    events = db.fetch_all("select type from events where entity_id = %s", (clip,))
    assert {"type": "clip.thumb_failed"} in events

    def thumb(sql: str) -> object:
        row = db.fetch_one(sql)
        assert row is not None
        return row["thumbnail_url"]

    assert thumb("select thumbnail_url from videos where source_id = 'ytbackfill1'") == (
        "https://i.example.test/vi/ytbackfill1/hqdefault.jpg"
    )
    assert thumb("select thumbnail_url from videos where source_id = 'video1'") == (
        "https://example.test/vod/1-320x180.jpg"
    )
    assert thumb("select thumbnail_url from viewer_clips where source_id = 'FakeClipSlug1'") == (
        "https://example.test/clip/1.jpg"
    )
    assert creator_row(creator)["avatar_url"] == "https://example.test/avatar/chan-BackfillStreamer.png"
    # Idempotent: a second run does not look these rows up again.
    youtube = FakeYouTubeClient()
    twitch.calls.clear()
    console_media.backfill(50, twitch=twitch, youtube=youtube, storage=FakeClipStorage(), now=NOW)
    looked_up = [video_id for call in youtube.calls_to("videos_list") for video_id in call["ids"]]
    assert "ytbackfill1" not in looked_up
    users = [user_id for call in twitch.calls_to("get_users") for user_id in call["ids"]]
    assert "chan-BackfillStreamer" not in users

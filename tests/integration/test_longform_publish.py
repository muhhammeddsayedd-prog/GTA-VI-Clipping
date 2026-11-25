"""M20 L6 + go-live: publishing a ready episode, the day-format rotation and the creator-day plan, against
Postgres with fake Upload-Post and storage."""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from sixstars import db, settings
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.longform import planner, publish
from sixstars.storage import FakeClipStorage

NOW = datetime(2032, 3, 3, 17, tzinfo=UTC)


def put(key: str, value: Any) -> None:
    db.execute(
        "insert into settings (key, value, description) values (%s, %s, '') "
        "on conflict (key) do update set value = excluded.value",
        (key, Jsonb(value)),
    )
    settings.invalidate()


@pytest.fixture
def live() -> Iterator[None]:
    put("longform.enabled", True)
    put("longform.publish_mode", "public")
    put("control.emergency_stop", False)
    put("launch.dry_run", False)
    put("launch.launch_at", "2020-01-01T00:00:00Z")
    db.execute(
        "insert into platform_accounts (id, platform, upload_post_user, handle, lanes, cap_24h, privacy) "
        "values ('youtube_main', 'youtube', 'main', '@t', '{hourly}', 16, 'public') "
        "on conflict (id) do update set enabled = true, upload_post_user = 'main', platform = 'youtube'"
    )
    clean()
    yield
    clean()


def clean() -> None:
    db.execute("delete from episodes where episode_date >= '2032-03-01' and episode_date < '2032-04-01'")
    db.execute(r"delete from clips where creator_id in (select id from creators where handle like 'lfp\_%%')")
    db.execute(r"delete from creators where handle like 'lfp\_%%'")


def ready_episode(day: str = "2032-03-03") -> str:
    row = db.fetch_one(
        """
        insert into episodes (episode_date, status, account_id, title, description, tags, storage_path,
                              thumb_path)
        values (%s, 'ready', 'youtube_main', 'TOP 3 Wildest GTA 6 Moments', 'Intro.\n00:00 #3',
                '{gta 6,gta 6 funny moments}', 'episodes/test/x.mp4', 'episodes/test/x.jpg')
        returning id::text as id
        """,
        (day,),
    )
    assert row is not None
    return str(row["id"])


def status_of(episode_id: str) -> dict[str, Any]:
    row = db.fetch_one("select * from episodes where id = %s", (episode_id,))
    assert row is not None
    return dict(row)


def test_publish_uploads_by_url_and_records_the_post(live: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "app_env", lambda: "production")
    ep = ready_episode()
    up = FakeUploadPostClient()
    assert publish.publish(ep, client=up, storage=FakeClipStorage(), now=NOW) == "posted"
    call = up.calls_to("upload_video_url")[0]
    fields = call["fields"]
    assert call["user"] == "main" and call["platform"] == "youtube"
    assert fields["privacyStatus"] == "public" and fields["categoryId"] == "20"
    assert fields["tags"] == ["gta 6", "gta 6 funny moments"] and fields["thumbnail_url"]
    assert "#Shorts" not in fields["youtube_description"]
    assert call["idempotency_key"] == f"episode:{ep}:youtube_main"
    row = status_of(ep)
    assert row["status"] == "posted" and row["post_url"] and row["privacy"] == "public"
    # Run again (a retried job): nothing is uploaded twice.
    assert publish.publish(ep, client=up, storage=FakeClipStorage(), now=NOW) == "posted"
    assert len(up.calls_to("upload_video_url")) == 1


def test_non_production_is_always_private(live: None) -> None:
    ep = ready_episode()
    up = FakeUploadPostClient()
    assert publish.publish(ep, client=up, storage=FakeClipStorage(), now=NOW) == "posted_private"
    assert up.calls_to("upload_video_url")[0]["fields"]["privacyStatus"] == "private"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("control.emergency_stop", True),
        ("launch.dry_run", True),
        ("longform.publish_mode", "off"),
        ("longform.enabled", False),
    ],
)
def test_publish_refuses_when_posting_is_off(live: None, key: str, value: Any) -> None:
    put(key, value)
    ep = ready_episode()
    up = FakeUploadPostClient()
    assert publish.publish(ep, client=up, storage=FakeClipStorage(), now=NOW) == "ready"
    assert up.calls_to("upload_video_url") == []


def test_publish_waits_when_the_youtube_cap_is_used(live: None) -> None:
    put("posting.platform_max_24h", {**settings.get_dict("posting.platform_max_24h"), "youtube": 0})
    ep = ready_episode()
    up = FakeUploadPostClient()
    assert publish.publish(ep, client=up, storage=FakeClipStorage(), now=NOW) == "ready"
    assert up.calls_to("upload_video_url") == []


def test_due_tick_enqueues_ready_episodes_after_the_publish_hour(live: None) -> None:
    ep = ready_episode()
    put("longform.publish_hour_utc", 16)
    assert publish.due_tick(NOW - timedelta(hours=2)) == []
    jobs = publish.due_tick(NOW)
    assert len(jobs) == 1
    assert publish.due_tick(NOW) == []  # deduplicated while queued
    row = db.fetch_one("select payload from jobs where id = %s", (jobs[0],))
    assert row is not None and row["payload"]["episode_id"] == ep


# ── day formats ──────────────────────────────────────────────


def make_clips(handle: str, n: int, score: float = 8.0) -> str:
    cr = db.fetch_one(
        "insert into creators (platform, channel_id, handle, display_name) values ('twitch', %s, %s, %s) "
        "returning id::text as id",
        (uuid.uuid4().hex, handle, handle.title()),
    )
    assert cr is not None
    for k in range(n):
        db.execute(
            """
            insert into clips (creator_id, source_platform, source_id, source_url, source_type, lane, start_s,
                               end_s, status, title, score, created_at, topic)
            values (%s, 'twitch', %s, 'https://www.twitch.tv/videos/1', 'vod', 'hourly', 0, 40, 'approved',
                    %s, %s, %s, %s)
            """,
            (
                cr["id"],
                uuid.uuid4().hex,
                f"{handle} moment {k}",
                score,
                NOW - timedelta(hours=3 + k),
                f"{handle}-{k}",
            ),
        )
    return str(cr["id"])


def test_rotation_plans_a_creator_day_after_a_mix_day(live: None) -> None:
    put("longform.auto_build", False)
    put("longform.min_s", 60)
    put("longform.solo", {"pool_hours": 168, "min_s": 200, "target_s": 300, "max_s": 400, "cooldown_days": 14,
                          "min_moments": 5})  # fmt: skip
    star = make_clips("lfp_star", 9)
    make_clips("lfp_other", 3)
    db.execute(
        "insert into episodes (episode_date, status, format) values ('2032-03-01', 'posted', 'creator')"
    )
    day1, day2 = date(2032, 3, 2), date(2032, 3, 3)
    first = planner.plan_episode(day1, NOW - timedelta(days=1))
    assert first.format == "mix"
    second = planner.plan_episode(day2, NOW)
    assert second.format == "creator" and second.status == "planned"
    row = db.fetch_one(
        "select featured_creator_id::text as f from episodes where id = %s", (second.episode_id,)
    )
    assert row is not None and row["f"] == star
    creators = db.fetch_all("select distinct creator_id::text as c from episode_items where episode_id = %s",
                            (second.episode_id,))  # fmt: skip
    assert [r["c"] for r in creators] == [star]


def test_creator_day_falls_back_to_a_mix_day(live: None) -> None:
    put("longform.auto_build", False)
    put("longform.min_s", 60)
    make_clips("lfp_solo_a", 3)
    make_clips("lfp_solo_b", 3)
    db.execute("insert into episodes (episode_date, status, format) values ('2032-03-02', 'posted', 'mix')")
    plan = planner.plan_episode(date(2032, 3, 3), NOW)
    assert plan.format == "mix" and plan.status == "planned"


# ── takedown ─────────────────────────────────────────────────


def test_creator_takedown_unpublishes_their_episodes(live: None) -> None:
    from sixstars.longform.takedown import remove_episodes_of_creator

    creator = make_clips("lfp_taken", 1)
    clip = db.fetch_one("select id::text as id from clips where creator_id = %s", (creator,))
    assert clip is not None
    posted = ready_episode("2032-03-04")
    upcoming = ready_episode("2032-03-05")
    db.execute("update episodes set status = 'posted', platform_post_id = 'yt1' where id = %s", (posted,))
    for ep in (posted, upcoming):
        db.execute(
            "insert into episode_items (episode_id, position, clip_id, creator_id, status) "
            "values (%s, 1, %s, %s, 'rendered')",
            (ep, clip["id"], creator),
        )
    up = FakeUploadPostClient()
    done = remove_episodes_of_creator(creator, "DMCA", up)
    assert {d.episode_id for d in done} == {posted, upcoming} and all(d.done for d in done)
    assert up.calls_to("delete_post") == [{"user": "main", "platform": "youtube", "post_id": "yt1"}]
    assert status_of(posted)["status"] == "removed" and status_of(upcoming)["status"] == "removed"

"""M20 L2: the pool SQL and the planner against a real schema (pool excludes, episode rows, idempotency)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db, settings
from sixstars.longform import planner, pool
from sixstars.longform.config import LongformConfig

NOW = datetime(2030, 1, 2, 12, tzinfo=UTC)
DAY = NOW.date()


def ensure_account() -> None:
    db.execute(
        """
        insert into platform_accounts (id, platform, upload_post_user, handle, lanes, cap_24h)
        values ('youtube_main', 'youtube', 'default', '@test', '{hourly,spike}', 16)
        on conflict (id) do nothing
        """
    )


def add_creator(tier: str = "auto") -> str:
    row = db.fetch_one(
        """
        insert into creators (platform, channel_id, handle, tier)
        values ('twitch', %s, %s, %s) returning id::text
        """,
        (uuid.uuid4().hex, f"lf_{uuid.uuid4().hex[:8]}", tier),
    )
    assert row is not None
    return str(row["id"])


def add_clip(creator_id: str, **over: Any) -> str:
    values: dict[str, Any] = {
        "status": "approved",
        "score": 8,
        "start_s": 100,
        "end_s": 130,
        "title": "big moment",
        "topic": None,
        "spoiler": False,
        "safety_flags": {"hate": False, "sexual": False},
        "reject_reason": None,
        "created_at": NOW - timedelta(hours=3),
    }
    values.update(over)
    row = db.fetch_one(
        """
        insert into clips (creator_id, source_platform, source_id, source_url, source_type, lane,
                           start_s, end_s, status, score, title, topic, spoiler, safety_flags,
                           reject_reason, created_at)
        values (%s, 'twitch', %s, 'https://www.twitch.tv/videos/1', 'vod', 'hourly', %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s)
        returning id::text
        """,
        (
            creator_id,
            uuid.uuid4().hex,
            values["start_s"],
            values["end_s"],
            values["status"],
            values["score"],
            values["title"],
            values["topic"],
            values["spoiler"],
            Jsonb(values["safety_flags"]),
            values["reject_reason"],
            values["created_at"],
        ),
    )
    assert row is not None
    return str(row["id"])


def cfg(**over: Any) -> LongformConfig:
    ensure_account()
    base = LongformConfig.load()
    return base.__class__(**{**base.__dict__, **over})


def test_pool_applies_the_hard_excludes(test_database_url: str) -> None:
    ok = add_clip(add_creator())
    allowed_reject = add_clip(add_creator(), status="rejected", reject_reason="creator_daily_cap")
    excluded = [
        add_clip(add_creator(), score=5),
        add_clip(add_creator(), spoiler=True),
        add_clip(add_creator(), safety_flags={"hate": True}),
        add_clip(add_creator(), status="rejected", reject_reason="music_over_limit"),
        add_clip(add_creator(tier="restricted")),
        add_clip(add_creator(tier="blocked")),
        add_clip(add_creator(), created_at=NOW - timedelta(hours=100)),
    ]
    ids = {c.clip_id for c in pool.load_candidates(cfg(), NOW)}
    assert {ok, allowed_reject} <= ids
    assert not ids & set(excluded)


def test_plan_writes_the_episode_once(test_database_url: str) -> None:
    db.execute("delete from episodes where episode_date = %s", (DAY,))
    for _ in range(30):
        add_clip(add_creator())
    c = cfg(target_s=120.0, min_s=90.0, max_s=150.0)
    first = planner.plan_episode(DAY, NOW, c)
    assert first.created and first.status == "planned" and first.n_moments >= 3
    items = db.fetch_all(
        "select position from episode_items where episode_id = %s order by position", (first.episode_id,)
    )
    assert [r["position"] for r in items] == list(range(1, first.n_moments + 1))
    again = planner.plan_episode(DAY, NOW, c)
    assert not again.created and again.episode_id == first.episode_id
    used = db.fetch_all("select clip_id::text from episode_items where episode_id = %s", (first.episode_id,))
    later = {x.clip_id for x in pool.load_candidates(c, NOW)}
    assert not {r["clip_id"] for r in used} & later, "a clip used in an episode is not picked again"


def test_thin_pool_marks_the_day_skipped(test_database_url: str) -> None:
    day = date(2030, 2, 1)
    db.execute("delete from episodes where episode_date = %s", (day,))
    result = planner.plan_episode(day, datetime(2030, 2, 1, 12, tzinfo=UTC), cfg(min_s=10_000.0))
    assert result.status == "skipped" and result.n_moments == 0


def test_due_tick_waits_for_the_build_hour_and_the_switch(test_database_url: str) -> None:
    day = date(2030, 3, 1)
    db.execute("delete from episodes where episode_date = %s", (day,))
    early = datetime(2030, 3, 1, 1, tzinfo=UTC)
    late = datetime(2030, 3, 1, 23, tzinfo=UTC)
    assert planner.due_tick(late, cfg(enabled=False)) is None
    assert planner.due_tick(early, cfg(enabled=True, build_hour_utc=10)) is None
    job_id = planner.due_tick(late, cfg(enabled=True, build_hour_utc=10))
    assert job_id is not None
    assert planner.due_tick(late, cfg(enabled=True, build_hour_utc=10)) is None, "deduplicated per day"
    settings.invalidate()

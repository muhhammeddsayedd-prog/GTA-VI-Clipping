"""M20 L3: the two-phase build bookkeeping (drop, renumber without gaps, phase hand-offs) on a real schema."""

import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from sixstars import db
from sixstars.longform import build
from sixstars.longform.config import LongformConfig

DAY = date(2031, 5, 1)


@pytest.fixture(autouse=True)
def _cleanup(test_database_url: str) -> Iterator[None]:
    yield
    db.execute("delete from episodes where episode_date >= '2031-01-01'")
    db.execute(r"delete from clips where creator_id in (select id from creators where handle like 'lfb\_%%')")
    db.execute(r"delete from creators where handle like 'lfb\_%%'")


def make_episode(n: int) -> tuple[str, list[str]]:
    db.execute(
        "insert into platform_accounts (id, platform, upload_post_user, handle, lanes, cap_24h) "
        "values ('youtube_main', 'youtube', 'default', '@t', '{hourly}', 16) on conflict (id) do nothing"
    )
    ep = db.fetch_one(
        "insert into episodes (episode_date, status, account_id) values (%s, 'planned', 'youtube_main') "
        "returning id::text",
        (DAY,),
    )
    assert ep is not None
    items: list[str] = []
    for pos in range(1, n + 1):
        cr = db.fetch_one(
            "insert into creators (platform, channel_id, handle) values ('twitch', %s, %s) "
            "returning id::text",
            (uuid.uuid4().hex, f"lfb_{uuid.uuid4().hex[:6]}"),
        )
        assert cr is not None
        clip = db.fetch_one(
            """
            insert into clips (creator_id, source_platform, source_id, source_url, source_type, lane, start_s,
                               end_s, status)
            values (%s, 'twitch', %s, 'https://www.twitch.tv/videos/1', 'vod', 'hourly', 10, 30, 'approved')
            returning id::text
            """,
            (cr["id"], uuid.uuid4().hex),
        )
        assert clip is not None
        item = db.fetch_one(
            "insert into episode_items (episode_id, position, clip_id, creator_id, est_s) "
            "values (%s, %s, %s, %s, 30) returning id::text",
            (ep["id"], pos, clip["id"], cr["id"]),
        )
        assert item is not None
        items.append(str(item["id"]))
    return str(ep["id"]), items


def positions(episode_id: str) -> dict[str, Any]:
    rows = db.fetch_all("select id::text as id, position, status from episode_items where episode_id = %s",
                        (episode_id,))  # fmt: skip
    return {r["id"]: (r["position"], r["status"]) for r in rows}


def test_start_build_queues_phase_one(test_database_url: str) -> None:
    episode_id, items = make_episode(3)
    assert build.start_build(episode_id, LongformConfig.load()) == 3
    queued = db.fetch_all("select payload->>'item_id' as i from jobs where type = %s", (build.JOB_ITEM,))
    assert {r["i"] for r in queued} == set(items)
    status = db.fetch_one("select status from episodes where id = %s", (episode_id,))
    assert status is not None and status["status"] == "fetching"


def test_renumber_closes_gaps_left_by_dropped_moments(test_database_url: str) -> None:
    episode_id, items = make_episode(5)
    db.execute("update episode_items set status = 'fetched' where episode_id = %s", (episode_id,))
    build.drop(items[1], "no archive")  # position 2
    build.drop(items[3], "deleted")  # position 4
    build.renumber(episode_id)
    got = positions(episode_id)
    kept = sorted(p for p, s in got.values() if s == "fetched")
    assert kept == [1, 2, 3]
    assert got[items[0]][0] == 1 and got[items[2]][0] == 2 and got[items[4]][0] == 3
    assert all(p > 100000 for p, s in got.values() if s == "dropped")


def test_advance_moves_from_fetch_to_render_to_assemble(test_database_url: str) -> None:
    cfg = LongformConfig.load()
    episode_id, items = make_episode(3)
    build.start_build(episode_id, cfg)
    assert build.advance(episode_id, cfg) == "fetching"
    db.execute("update episode_items set status = 'fetched' where episode_id = %s", (episode_id,))
    assert build.advance(episode_id, cfg) == "rendering"
    renders = db.fetch_all("select 1 from jobs where type = %s", (build.JOB_RENDER,))
    assert len(renders) == 3
    db.execute("update episode_items set status = 'rendered' where episode_id = %s", (episode_id,))
    assert build.advance(episode_id, cfg) == "assembling"
    assert db.fetch_one("select 1 from jobs where type = %s", (build.JOB_ASSEMBLE,)) is not None


def live_row(vod: str | None) -> dict[str, Any]:
    return {
        "start_s": 1,
        "end_s": 2,
        "source_type": "live",
        "vod_source_id": vod,
        "session_platform": "twitch",
        "source_platform": "twitch",
        "source_url": "x",
    }


def test_live_moment_without_archive_is_dropped(test_database_url: str) -> None:
    with pytest.raises(build.SourceGoneError):
        build.source_of(live_row(None))
    assert build.source_of(live_row("123")).url == "https://www.twitch.tv/videos/123"

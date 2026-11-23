"""Integration tests for `sixstars.db`: helpers, transactions and heartbeats."""

import pytest

from sixstars import db


def test_ping() -> None:
    assert db.ping() is True


def test_schema_objects_exist() -> None:
    tables = db.fetch_all(
        "select table_name from information_schema.tables "
        "where table_schema = 'public' and table_type = 'BASE TABLE'"
    )
    # 18 from 0001 plus viewer_clips (0019), youtube_channel_cache (0024), scoring_config,
    # scoring_config_history, scoring_log (0026), clip_renders (0027) and the six clipping-campaign tables
    # of 0040 (clip_campaigns, clip_campaign_creators, clip_campaign_posts, render_profiles, lane_quotas,
    # creator_live_status)
    assert len(tables) == 30
    assert db.fetch_one("select * from v_queue_depth") is None
    assert db.fetch_all("select * from v_posts_last_24h") == []


def test_fetch_helpers_return_dicts() -> None:
    row = db.fetch_one("select %s::int as a, %s::text as b", (1, "x"))
    assert row == {"a": 1, "b": "x"}
    assert db.fetch_one("select 1 as a where false") is None
    assert db.fetch_all("select generate_series(1, 3) as n") == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_execute_returns_rowcount() -> None:
    db.heartbeat("a")
    db.heartbeat("b")
    assert db.execute("delete from heartbeats where name = any(%s)", (["a", "b"],)) == 2


def test_transaction_commits() -> None:
    with db.transaction():
        db.execute("insert into heartbeats (name) values ('tx-commit')")
    assert db.fetch_one("select name from heartbeats where name = 'tx-commit'") is not None


def test_transaction_rolls_back_on_error() -> None:
    with pytest.raises(RuntimeError), db.transaction():
        db.execute("insert into heartbeats (name) values ('tx-rollback')")
        assert db.fetch_one("select name from heartbeats where name = 'tx-rollback'") is not None
        raise RuntimeError("boom")
    assert db.fetch_one("select name from heartbeats where name = 'tx-rollback'") is None


def test_nested_transaction_is_a_savepoint() -> None:
    with db.transaction():
        db.execute("insert into heartbeats (name) values ('outer')")
        with pytest.raises(RuntimeError), db.transaction():
            db.execute("insert into heartbeats (name) values ('inner')")
            raise RuntimeError("boom")
    names = {row["name"] for row in db.fetch_all("select name from heartbeats")}
    assert names == {"outer"}


def test_heartbeat_upserts() -> None:
    db.heartbeat("svc", {"n": 1})
    first = db.fetch_one("select last_seen, info from heartbeats where name = 'svc'")
    db.heartbeat("svc", {"n": 2})
    second = db.fetch_one("select last_seen, info from heartbeats where name = 'svc'")
    assert first is not None and second is not None
    assert second["info"] == {"n": 2}
    assert second["last_seen"] >= first["last_seen"]
    assert db.fetch_one("select count(*) as n from heartbeats") == {"n": 1}

"""Migration 0056 (YouTube spike-dominant split): changes only the values 0034 set; can run twice."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from posting_support import clean_posting_tables, seed

from sixstars import db, settings

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0056_youtube_spike_dominant.sql"
OLD_HOURS = [0, 10, 13, 14, 16, 17, 18, 19, 20, 21, 22, 23]
NEW_HOURS = [0, 14, 17, 19, 21, 23]


@pytest.fixture(autouse=True)
def _clean(test_database_url: str) -> Iterator[None]:
    clean_posting_tables()
    seed("production")
    yield
    clean_posting_tables()
    settings.invalidate()


def apply_twice(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
        conn.execute(MIGRATION.read_bytes())


def set_youtube(**values: Any) -> None:
    assignments = ", ".join(f"{name} = %({name})s" for name in values)
    db.execute(f"update platform_accounts set {assignments} where id = 'youtube_main'", values)  # type: ignore[arg-type]


def youtube() -> dict[str, Any]:
    row = db.fetch_one(
        "select cap_24h, spike_reserve, hourly_hours_utc, hourly_min_gap_s from platform_accounts "
        "where id = 'youtube_main'"
    )
    assert row is not None
    return dict(row)


def test_the_0034_split_becomes_10_spike_6_hourly(test_database_url: str) -> None:
    set_youtube(spike_reserve=4, hourly_hours_utc=OLD_HOURS)  # production on 2026-11-23

    apply_twice(test_database_url)

    assert youtube() == {
        "cap_24h": 16,
        "spike_reserve": 10,
        "hourly_hours_utc": NEW_HOURS,
        "hourly_min_gap_s": 3000,
    }


def test_values_an_operator_set_are_kept(test_database_url: str) -> None:
    set_youtube(spike_reserve=6, hourly_hours_utc=[18, 19, 20])

    apply_twice(test_database_url)

    row = youtube()
    assert (row["spike_reserve"], row["hourly_hours_utc"]) == (6, [18, 19, 20])


def test_other_accounts_are_not_touched(test_database_url: str) -> None:
    set_youtube(spike_reserve=4, hourly_hours_utc=OLD_HOURS)
    before = db.fetch_all("select * from platform_accounts where id <> 'youtube_main' order by id")

    apply_twice(test_database_url)

    after = db.fetch_all("select * from platform_accounts where id <> 'youtube_main' order by id")
    strip = [{k: v for k, v in row.items() if k != "updated_at"} for row in before]
    assert [{k: v for k, v in row.items() if k != "updated_at"} for row in after] == strip


def test_the_spike_lane_quota_follows_the_reserve(test_database_url: str) -> None:
    set_youtube(spike_reserve=4, hourly_hours_utc=OLD_HOURS)
    db.execute(
        "insert into lane_quotas (account_id, lane_key, min_day, max_day) "
        "values ('youtube_main', 'spike', 0, 4) "
        "on conflict (account_id, lane_key) do update set max_day = 4"
    )

    apply_twice(test_database_url)

    row = db.fetch_one(
        "select max_day from lane_quotas where account_id = 'youtube_main' and lane_key = 'spike'"
    )
    assert row is not None and row["max_day"] == 10

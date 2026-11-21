"""Migration 0034 (YouTube lane tuning): changes only values the system set, and can run twice."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from posting_support import clean_posting_tables, seed

from sixstars import db, settings
from sixstars.posting.store import DbStore

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0034_youtube_lane_tuning.sql"
YT_HOURS = [0, 10, 13, 14, 16, 17, 18, 19, 20, 21, 22, 23]
NEW_COMMENT = "Would you have survived this? 👇 Follow {handle} for daily GTA 6 chaos"


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
    settings.invalidate()


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


@pytest.mark.parametrize(("before", "after"), [(10, 16), (12, 16), (16, 16), (28, 16), (20, 20), (24, 24)])
def test_the_cap_is_raised_only_from_a_value_the_system_set(
    test_database_url: str, before: int, after: int
) -> None:
    # production on 2026-11-21: learned floor 10, every hour, no hourly gap
    set_youtube(cap_24h=before, spike_reserve=4, hourly_hours_utc=None, hourly_min_gap_s=None)

    apply_twice(test_database_url)

    row = youtube()
    assert row["cap_24h"] == after
    assert (row["spike_reserve"], row["hourly_hours_utc"], row["hourly_min_gap_s"]) == (4, YT_HOURS, 3000)


def test_the_cap_never_goes_over_the_upload_post_ceiling(test_database_url: str) -> None:
    settings.set("posting.platform_max_24h", {"tiktok": 15, "instagram": 50, "youtube": 12})
    try:
        set_youtube(cap_24h=10)
        apply_twice(test_database_url)
        assert youtube()["cap_24h"] == 12
    finally:
        settings.set("posting.platform_max_24h", {"tiktok": 15, "instagram": 50, "youtube": 30})


def test_hours_and_gap_an_operator_set_are_kept(test_database_url: str) -> None:
    set_youtube(cap_24h=10, hourly_hours_utc=[18, 19, 20], hourly_min_gap_s=1800)

    apply_twice(test_database_url)

    row = youtube()
    assert (row["hourly_hours_utc"], row["hourly_min_gap_s"]) == ([18, 19, 20], 1800)


def test_other_accounts_are_not_touched(test_database_url: str) -> None:
    before = db.fetch_all("select * from platform_accounts where id <> 'youtube_main' order by id")

    apply_twice(test_database_url)

    after = db.fetch_all("select * from platform_accounts where id <> 'youtube_main' order by id")
    strip = [{k: v for k, v in row.items() if k != "updated_at"} for row in before]
    assert [{k: v for k, v in row.items() if k != "updated_at"} for row in after] == strip


@pytest.mark.parametrize(
    "old",
    [
        "Follow {handle} for the best GTA 6 moments every hour 🎮⭐",
        "Follow {handle} for the best GTA moments every hour 🎮⭐",
    ],
)
def test_the_first_comment_changes_from_a_known_text(test_database_url: str, old: str) -> None:
    settings.set("posting.first_comment", old)

    apply_twice(test_database_url)

    assert settings.get("posting.first_comment") == NEW_COMMENT


def test_an_edited_first_comment_and_hashtags_are_kept(test_database_url: str) -> None:
    settings.set("posting.first_comment", "Our own words {handle}")
    settings.set("posting.base_hashtags", ["#GTA6", "#Custom"])

    apply_twice(test_database_url)

    assert settings.get("posting.first_comment") == "Our own words {handle}"
    assert settings.get("posting.base_hashtags") == ["#GTA6", "#Custom"]


@pytest.mark.parametrize(
    "old", [["#GTA6", "#GTAVI", "#GTA6Clips", "#Gaming"], ["#GTA5", "#GTAV", "#GTA", "#Gaming"]]
)
def test_the_hashtags_change_from_a_known_list(test_database_url: str, old: list[str]) -> None:
    settings.set("posting.base_hashtags", old)

    apply_twice(test_database_url)

    assert settings.get("posting.base_hashtags") == ["#GTA6", "#GTAVI", "#GTA6Clips"]


def test_the_store_loads_the_hourly_gap(test_database_url: str) -> None:
    apply_twice(test_database_url)

    accounts = {account.id: account for account in DbStore().load_accounts()}

    assert accounts["youtube_main"].hourly_min_gap_s == 3000
    assert accounts["instagram_main"].hourly_min_gap_s is None

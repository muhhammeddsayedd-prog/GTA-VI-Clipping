"""Migration 0038: the `cover.video_ready` flag (off) and the clip columns, idempotent; the factory reads the
flag through `coverready.load_rules`."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from sixstars.factory import coverready
from sixstars.scoring import config as scoring_config

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0038_cover_video_ready.sql"


@pytest.fixture(autouse=True)
def _fresh_cache(test_database_url: str) -> Iterator[None]:
    scoring_config.invalidate()
    yield
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(
            "update scoring_config set value = jsonb_set(value, '{enabled}', 'false') where key = %s",
            (coverready.FLAG,),
        )
    scoring_config.invalidate()


def test_flag_starts_off_and_columns_exist(test_database_url: str) -> None:
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
        conn.execute(MIGRATION.read_bytes())
        row = conn.execute("select value from scoring_config where key = %s", (coverready.FLAG,)).fetchone()
        columns = {
            name
            for (name,) in conn.execute(
                "select column_name from information_schema.columns where table_name = 'clips'"
            ).fetchall()
        }
    assert row is not None and row[0]["enabled"] is False
    assert coverready.CoverVideoRules.from_flag(row[0]) == coverready.CoverVideoRules()
    assert {"peak_s", "peak_source", "facecam_h"} <= columns
    assert coverready.load_rules() is None


def test_load_rules_when_on(test_database_url: str) -> None:
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
        conn.execute(
            "update scoring_config set value = value || '{\"enabled\": true, \"facecam_h\": 800}' where key = %s",
            (coverready.FLAG,),
        )
    scoring_config.invalidate()
    rules = coverready.load_rules()
    assert rules is not None and rules.facecam_h == 800 and rules.phrase_words == 6

"""Migration 0058 (M20 L1, D122): episodes + episode_items, the episodes bucket and the longform.* settings;
idempotent and never overwrites an operator's value."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from sixstars import db, settings
from sixstars.longform.config import LongformConfig, PublishMode, RenderSpec

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0058_longform.sql"


@pytest.fixture(autouse=True)
def _restore(test_database_url: str) -> Iterator[None]:
    saved = settings.get("longform.min_score", None)
    yield
    if saved is not None:
        settings.set("longform.min_score", saved)
    settings.invalidate()


def apply_twice(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
        conn.execute(MIGRATION.read_bytes())
    settings.invalidate()


def test_tables_and_bucket_exist(test_database_url: str) -> None:
    apply_twice(test_database_url)
    for table in ("episodes", "episode_items"):
        row = db.fetch_one("select to_regclass(%s) is not null as ok", (f"public.{table}",))
        assert row is not None and row["ok"], table
    bucket = db.fetch_one("select id, public from storage.buckets where id = 'episodes'")
    assert bucket is not None and bucket["public"] is False


def test_settings_load_with_production_defaults(test_database_url: str) -> None:
    apply_twice(test_database_url)
    cfg = LongformConfig.load()
    assert cfg.enabled is False
    assert cfg.publish_mode is PublishMode.OFF
    assert cfg.render == RenderSpec()
    assert (cfg.target_s, cfg.min_s, cfg.max_s) == (720, 600, 870)
    assert (cfg.segment_min_s, cfg.segment_max_s) == (15, 45)
    assert cfg.account_id == "youtube_main"
    assert "approved" in cfg.pool_statuses


def test_an_operators_value_is_kept(test_database_url: str) -> None:
    apply_twice(test_database_url)
    settings.set("longform.min_score", 6)
    apply_twice(test_database_url)
    assert settings.get("longform.min_score") == 6


def test_status_checks_reject_unknown_values(test_database_url: str) -> None:
    apply_twice(test_database_url)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into episodes (episode_date, status) values ('2000-01-01', 'bogus')")

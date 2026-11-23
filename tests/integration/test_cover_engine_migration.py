"""Migration 0039: the covers table, the cover.* keys (master switch off) and the buckets; the store and
the config read them back."""

from pathlib import Path

import psycopg
import pytest
from gate_support import add_clip, add_creator

from sixstars import db
from sixstars.covers import store
from sixstars.covers.config import CoverConfig
from sixstars.scoring import config as scoring_config

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0039_cover_engine.sql"


@pytest.fixture(autouse=True)
def _applied(test_database_url: str) -> None:
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
        conn.execute(MIGRATION.read_bytes())
    scoring_config.invalidate()


def test_keys_buckets_and_config() -> None:
    config = CoverConfig.load()
    assert not config.enabled and config.mode_for("normal") == "off"
    assert config.lane_modes["*"] == "frame_only" and config.exploration == 0.2 and config.version > 0
    buckets = {row["id"]: row["public"] for row in db.fetch_all("select id, public from storage.buckets")}
    assert buckets.get("covers-work") is False and buckets.get("covers-public") is True


def test_store_round_trip() -> None:
    clip_id = add_clip(add_creator(handle="CoverStreamer"), None)
    cover_id = store.insert(
        store.NewCover(
            clip_id=clip_id,
            lane="normal",
            platform="instagram",
            arm="split|quote",
            config_version=CoverConfig.load().version,
            account_id="instagram_main",
            layout="split",
            line_text="SIX STARS?!",
            line_style="quote",
            key_word="STARS",
            scores={"face": 0.4},
            phash="ffee",
        )
    )
    assert store.recent_lines("instagram_main") == []  # drafts do not count
    store.set_status(cover_id, "passed", scores={"vision": 8})
    assert store.recent_lines("instagram_main") == ["SIX STARS?!"]
    store.set_status(cover_id, "applied")
    row = db.fetch_one("select status, scores, applied_at from covers where id = %s", (cover_id,))
    assert row is not None and row["status"] == "applied" and row["applied_at"] is not None
    assert row["scores"] == {"face": 0.4, "vision": 8}
    with pytest.raises(psycopg.errors.CheckViolation):
        store.insert(store.NewCover(clip_id=clip_id, lane="normal", platform="x", arm="a", config_version=1))

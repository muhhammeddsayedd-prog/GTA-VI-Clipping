"""Migration 0054 (Render v2 R1, D119): the four Render v2 settings, the retired frames switched off and
out of the director's affinity; idempotent and never overwrites an operator's value."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from sixstars import settings
from sixstars.factory.config import FactoryConfig
from sixstars.factory.layout import LayoutV2Rules

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0054_render_v2_settings.sql"
KEYS = ("factory.layout_v2", "factory.text_v2", "facecam.detect", "gate.render_v2")
RETIRED = ("hype_montage", "headline_card", "pov_fullframe")


@pytest.fixture(autouse=True)
def _restore(test_database_url: str) -> Iterator[None]:
    saved = {key: settings.get(key, None) for key in (*KEYS, "factory.styles", "factory.style_director")}
    yield
    for key, value in saved.items():
        if value is not None:
            settings.set(key, value)
    settings.invalidate()


def apply_twice(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
        conn.execute(MIGRATION.read_bytes())
    settings.invalidate()


def test_the_settings_exist_and_the_switch_is_off(test_database_url: str) -> None:
    apply_twice(test_database_url)
    for key in KEYS:
        assert isinstance(settings.get(key), dict), key
    cfg = FactoryConfig.load("hourly")
    assert cfg.layout_v2 == LayoutV2Rules(scaler="lanczos") and cfg.scaler == "lanczos"


def test_the_retired_frames_are_off_and_leave_the_affinity(test_database_url: str) -> None:
    apply_twice(test_database_url)
    styles = settings.get("factory.styles")
    for style_id in RETIRED:
        assert styles[style_id]["enabled"] is False, style_id
    assert styles["cam_top_title"].get("enabled", True) is True
    affinity = settings.get("factory.style_director")["affinity"]
    for kind, weights in affinity.items():
        assert not set(RETIRED) & set(weights), kind
    assert affinity["story"]["cam_top_title"] == 3, "other affinities are kept"
    usable = {entry.id for entry in FactoryConfig.load("hourly").catalog.usable("hourly", "youtube_main")}
    assert not set(RETIRED) & usable


def test_an_operators_value_is_kept(test_database_url: str) -> None:
    settings.set("factory.layout_v2", {"enabled": True, "cam_h": 760})
    apply_twice(test_database_url)
    assert settings.get("factory.layout_v2") == {"enabled": True, "cam_h": 760}

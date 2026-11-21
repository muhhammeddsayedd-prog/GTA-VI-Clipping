"""Migration 0036: the per-platform hourly length and the follow-line twins, idempotent."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from sixstars import settings
from sixstars.factory.director import DirectorRules
from sixstars.factory.styles import StyleCatalog

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0036_youtube_length_follow.sql"
TWINS = ("cam_top_viral_follow", "cam_top_wordcap_follow", "cam_top_title_follow")


@pytest.fixture(autouse=True)
def _restore(test_database_url: str) -> Iterator[None]:
    saved = {key: settings.get(key, None) for key in ("factory.styles", "factory.style_director")}
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


def test_the_twins_copy_their_style_for_youtube_hourly_only(test_database_url: str) -> None:
    apply_twice(test_database_url)

    styles = settings.get("factory.styles")
    for twin in TWINS:
        base = styles[twin.removesuffix("_follow")]
        entry = styles[twin]
        assert entry["accounts"] == ["youtube_main"] and entry["lanes"] == ["hourly"]
        assert entry["weight"] == base.get("weight", 1)
        rules = dict(entry["rules"])
        assert rules.pop("follow_overlay_s") == 1.5
        assert rules.pop("follow_overlay_text") == "Follow @6starsClips-m3x for Part 2"
        assert rules == base.get("rules", {})
    catalog = StyleCatalog.from_setting(styles)
    youtube = {e.id for e in catalog.usable("hourly", "youtube_main")}
    instagram = {e.id for e in catalog.usable("hourly", "instagram_main")}
    spikes = {e.id for e in catalog.usable("spike", "youtube_main")}
    assert set(TWINS) <= youtube and not set(TWINS) & instagram and not set(TWINS) & spikes
    assert catalog.get("cam_top_viral_follow").rules.follow_overlay_s == 1.5  # type: ignore[union-attr]


def test_the_twins_get_their_styles_affinity(test_database_url: str) -> None:
    apply_twice(test_database_url)

    director = DirectorRules.from_setting(settings.get("factory.style_director"))
    for kind, styles in director.affinity.items():
        for twin in TWINS:
            base = twin.removesuffix("_follow")
            if base in styles:
                assert styles[twin] == styles[base], (kind, twin)


def test_an_operators_twin_is_kept(test_database_url: str) -> None:
    styles = dict(settings.get("factory.styles"))
    styles["cam_top_viral_follow"] = {"enabled": False, "rules": {}}
    settings.set("factory.styles", styles)

    apply_twice(test_database_url)

    assert settings.get("factory.styles")["cam_top_viral_follow"] == {"enabled": False, "rules": {}}


def test_the_length_setting_is_documented_and_seeded(test_database_url: str) -> None:
    apply_twice(test_database_url)

    assert settings.get("gate.hourly_len_by_platform") == {"youtube": {"min_len": 8, "max_len": 30}}
    docs = (ROOT / "docs" / "16-settings-reference.md").read_text("utf-8")
    assert "| `gate.hourly_len_by_platform` |" in docs

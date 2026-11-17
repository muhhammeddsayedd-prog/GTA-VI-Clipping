"""Worker-side pure pieces of the live lane, the settings defaults and the command helpers."""

import re
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sixstars.cli import app
from sixstars.clients.fakes import fake_video
from sixstars.live import commands, handlers
from sixstars.live.config import DEFAULTS, LiveConfig

MIGRATION = Path(__file__).resolve().parents[3] / "supabase" / "migrations" / "0012_live.sql"
SETTINGS_DOC = Path(__file__).resolve().parents[3] / "docs" / "16-settings-reference.md"


# ── replay matching ──────────────────────────────────────────


def test_the_archive_of_a_stream_is_matched_by_stream_id() -> None:
    videos = [replace(fake_video(index), stream_id=f"stream-{index}") for index in range(3)]
    found = handlers.match_archive(videos, "stream-1")
    assert found is not None and found.id == videos[1].id


def test_no_archive_matches_another_stream_or_a_highlight() -> None:
    videos = [replace(fake_video(0), stream_id="stream-0"), replace(fake_video(1), stream_id=None)]
    assert handlers.match_archive(videos, "stream-9") is None
    assert handlers.match_archive([], "stream-0") is None


# ── stage timings ────────────────────────────────────────────


def test_every_stage_from_the_chat_peak_to_the_posting_decision_is_measured() -> None:
    info = {
        "spike_wall": 1000.0,
        "queued_at": 1030.0,
        "timings_ms": {"detect_lag_ms": 18000, "confirm_ms": 2500, "cut_wait_ms": 6000, "cut_ms": 1500},
    }
    stages = handlers.stage_timings(info, claimed_at=1031.5, rendered_at=1081.5, decided_at=1090.0)
    assert stages == {
        "detect_lag_ms": 18000,
        "confirm_ms": 2500,
        "cut_wait_ms": 6000,
        "cut_ms": 1500,
        "queue_wait_ms": 1500,
        "render_ms": 50000,
        "gate_post_ms": 8500,
        "spike_to_decision_s": 90.0,
    }


def test_timings_survive_a_payload_with_missing_parts() -> None:
    stages = handlers.stage_timings({}, claimed_at=None, rendered_at=10.0, decided_at=12.0)
    assert stages == {"gate_post_ms": 2000}


# ── settings ─────────────────────────────────────────────────


def test_the_code_defaults_are_the_values_the_migration_seeds() -> None:
    import json

    seeded = {
        key: json.loads(value)
        for key, value in re.findall(
            r"\('(live\.[a-z_]+)', '([^']*)'::jsonb", MIGRATION.read_text(encoding="utf-8")
        )
    }
    assert seeded.pop("live.source_health") is None  # written by code, no default
    assert seeded == DEFAULTS


def test_every_new_setting_is_in_the_settings_reference() -> None:
    documented = SETTINGS_DOC.read_text(encoding="utf-8")
    for key in [*DEFAULTS, "live.source_health"]:
        assert f"| `{key}` |" in documented


def test_the_offline_config_holds_the_documented_defaults() -> None:
    cfg = LiveConfig.offline()
    assert (cfg.segment_s, cfg.buffer_s, cfg.max_reconnects) == (60.0, 1800.0, 3)
    assert cfg.max_height == 1080 and cfg.blocked_backoff_s == 1800 and cfg.priority_express == 100
    assert LiveConfig.offline(max_slots=0).max_slots == 0


# ── commands ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "platform"),
    [
        ("kiwo", "twitch"),
        ("https://www.twitch.tv/kiwo", "twitch"),
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
    ],
)
def test_the_platform_is_read_from_the_source(source: str, platform: str) -> None:
    assert commands.platform_of(source) == platform


def test_a_twitch_login_is_read_from_a_url_or_a_name() -> None:
    assert commands.twitch_login("https://www.twitch.tv/Kiwo/") == "kiwo"
    assert commands.twitch_login("@kiwo") == "kiwo"


def test_the_live_commands_exist() -> None:
    result = CliRunner().invoke(app, ["live", "--help"])
    assert result.exit_code == 0
    for name in ("status", "probe", "cut"):
        assert name in result.output


def test_cut_refuses_a_bad_time(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["live", "cut", str(tmp_path), "--start", "yesterday", "--end", "now"])
    assert result.exit_code == 2


def test_cut_reports_an_empty_buffer(tmp_path: Path) -> None:
    arguments = [
        "live",
        "cut",
        str(tmp_path),
        "--start",
        "2026-11-17T09:00:00",
        "--end",
        "2026-11-17T09:00:30",
    ]
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 1 and "cut failed" in result.output

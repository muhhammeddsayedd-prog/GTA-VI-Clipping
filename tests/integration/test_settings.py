"""Integration tests for `sixstars.settings` and the settings and db CLI commands."""

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from sixstars import db, settings
from sixstars.cli import app

runner = CliRunner()


def test_get_seeded_value() -> None:
    assert settings.get("spikes.lead_s") == 20
    assert settings.get_int("spikes.lead_s") == 20


def test_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        settings.get("spikes.led_s")
    assert settings.get("spikes.led_s", None) is None
    assert settings.get("spikes.led_s", 7) == 7


def test_typed_helpers_on_seeded_values() -> None:
    assert settings.get_bool("launch.dry_run") is True
    assert settings.get_float("spikes.z_s") == 3.4
    assert settings.get_float("spikes.lead_s") == 20.0
    assert settings.get_str("env.target_game") == "Grand Theft Auto VI"
    assert settings.get_dict("spikes.weights")["msgs"] == 0.15
    assert "#GTA6" in settings.get_list("posting.base_hashtags")
    assert settings.get_dt("launch.launch_at") == datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
    assert settings.get_dt("vod.paused_until") is None


def test_typed_helper_rejects_wrong_type() -> None:
    with pytest.raises(TypeError):
        settings.get_int("env.target_game")
    with pytest.raises(TypeError):
        settings.get_bool("spikes.lead_s")


def test_set_then_get() -> None:
    settings.set("spikes.lead_s", 25)
    assert settings.get("spikes.lead_s") == 25
    settings.set("test.new_key", {"a": [1, 2]}, description="made by a test")
    assert settings.get_dict("test.new_key") == {"a": [1, 2]}


def test_set_keeps_description() -> None:
    before = db.fetch_one("select description from settings where key = 'spikes.lead_s'")
    settings.set("spikes.lead_s", 25)
    after = db.fetch_one("select description from settings where key = 'spikes.lead_s'")
    assert before is not None and after == before


def test_change_from_another_process_is_seen_after_cache_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(settings.time, "monotonic", lambda: clock[0])
    assert settings.get("spikes.lead_s") == 20
    db.execute("update settings set value = '30'::jsonb where key = 'spikes.lead_s'")
    clock[0] += settings.CACHE_TTL_S - 1
    assert settings.get("spikes.lead_s") == 20
    clock[0] += 2
    assert settings.get("spikes.lead_s") == 30


def test_cli_settings_get_and_set() -> None:
    result = runner.invoke(app, ["settings", "get", "spikes.lead_s"])
    assert result.exit_code == 0
    assert result.output.strip() == "20"
    result = runner.invoke(app, ["settings", "set", "spikes.lead_s", "22"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["settings", "get", "spikes.lead_s"])
    assert result.output.strip() == "22"


def test_cli_settings_errors() -> None:
    assert runner.invoke(app, ["settings", "get", "no.such_key"]).exit_code == 1
    assert runner.invoke(app, ["settings", "set", "spikes.lead_s", "not json"]).exit_code == 2
    assert settings.get("spikes.lead_s") == 20


def test_cli_db_ping() -> None:
    result = runner.invoke(app, ["db", "ping"])
    assert result.exit_code == 0
    assert result.output.strip() == "ok (env=test)"

"""Unit tests for `sixstars.settings` with the database replaced by an in-memory table (no network)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from typer.testing import CliRunner

from sixstars import db, settings
from sixstars.cli import app


class FakeTable:
    """Stands in for the `settings` table and counts how often it is read."""

    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows
        self.reads = 0

    def fetch_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.reads += 1
        return [{"key": key, "value": value} for key, value in self.rows.items()]


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTable]:
    fake = FakeTable(
        {
            "a.int": 20,
            "a.float": 2.5,
            "a.whole_float": 3.0,
            "a.bool": True,
            "a.str": "text",
            "a.dict": {"x": 1},
            "a.list": [1, 2],
            "a.null": None,
            "a.dt_z": "2026-11-18T11:00:00Z",
            "a.dt_offset": "2026-11-18T13:00:00+02:00",
            "a.dt_naive": "2026-11-18T11:00:00",
        }
    )
    monkeypatch.setattr(db, "fetch_all", fake.fetch_all)
    settings.invalidate()
    yield fake
    settings.invalidate()


def test_get_and_default(table: FakeTable) -> None:
    assert settings.get("a.int") == 20
    assert settings.get("a.null") is None
    assert settings.get("a.null", "fallback") is None
    assert settings.get("missing", "fallback") == "fallback"
    assert settings.get("missing", None) is None
    with pytest.raises(KeyError, match="missing"):
        settings.get("missing")


def test_typed_helpers(table: FakeTable) -> None:
    assert settings.get_int("a.int") == 20
    assert settings.get_int("a.whole_float") == 3
    assert settings.get_float("a.float") == 2.5
    assert settings.get_float("a.int") == 20.0
    assert settings.get_bool("a.bool") is True
    assert settings.get_str("a.str") == "text"
    assert settings.get_dict("a.dict") == {"x": 1}
    assert settings.get_list("a.list") == [1, 2]
    assert settings.get_int("missing", 5) == 5
    with pytest.raises(KeyError):
        settings.get_int("missing")


@pytest.mark.parametrize(
    ("helper", "key"),
    [
        ("get_int", "a.float"),
        ("get_int", "a.bool"),
        ("get_int", "a.str"),
        ("get_float", "a.bool"),
        ("get_float", "a.str"),
        ("get_bool", "a.int"),
        ("get_str", "a.int"),
        ("get_dict", "a.list"),
        ("get_list", "a.dict"),
        ("get_dt", "a.int"),
        ("get_int", "a.null"),
    ],
)
def test_typed_helpers_reject_wrong_types(table: FakeTable, helper: str, key: str) -> None:
    with pytest.raises(TypeError, match=key):
        getattr(settings, helper)(key)


def test_copies_protect_the_cache(table: FakeTable) -> None:
    settings.get_dict("a.dict")["x"] = 99
    settings.get_list("a.list").append(3)
    assert settings.get_dict("a.dict") == {"x": 1}
    assert settings.get_list("a.list") == [1, 2]


def test_get_dt_is_always_utc(table: FakeTable) -> None:
    expected = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
    for key in ("a.dt_z", "a.dt_offset", "a.dt_naive"):
        value = settings.get_dt(key)
        assert value == expected
        assert value is not None and value.utcoffset() == timedelta(0)
    assert settings.get_dt("a.null") is None
    assert settings.get_dt("missing", None) is None
    plus_two = datetime(2026, 1, 1, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    assert settings.get_dt("missing", plus_two) == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def test_cache_lasts_30_seconds(table: FakeTable, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [500.0]
    monkeypatch.setattr(settings.time, "monotonic", lambda: clock[0])
    assert settings.CACHE_TTL_S == 30
    assert settings.get("a.int") == 20
    table.rows["a.int"] = 21
    clock[0] += 29.9
    assert settings.get("a.int") == 20
    assert table.reads == 1
    clock[0] += 0.2
    assert settings.get("a.int") == 21
    assert table.reads == 2


def test_invalidate_forces_a_reload(table: FakeTable) -> None:
    assert settings.get("a.int") == 20
    table.rows["a.int"] = 22
    settings.invalidate()
    assert settings.get("a.int") == 22


def test_cli_reports_missing_database_url() -> None:
    runner = CliRunner()
    for args in (["settings", "get", "spikes.lead_s"], ["db", "ping"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2
        assert "DATABASE_URL" in result.output


def test_cli_rejects_bad_queue_and_bad_json() -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["worker", "--queue", "fast"]).exit_code == 2
    assert runner.invoke(app, ["settings", "set", "spikes.lead_s", "{oops"]).exit_code == 2

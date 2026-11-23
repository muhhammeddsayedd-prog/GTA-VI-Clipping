"""The display clock: app.timezone formatting, labels and fallbacks (no database)."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sixstars import clock, settings

CAIRO = ZoneInfo("Africa/Cairo")


def _setting(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    def get_str(key: str, default: Any = None) -> Any:
        assert key == clock.TIMEZONE_KEY
        if not isinstance(value, str):
            raise TypeError("not a string")
        return value

    monkeypatch.setattr(settings, "get_str", get_str)


def test_cairo_is_utc_plus_two_in_november() -> None:
    dt = datetime(2026, 11, 21, 12, 32, tzinfo=UTC)
    assert clock.fmt_local(dt, zone=CAIRO) == "14:32 Cairo (12:32 UTC)"


def test_cairo_summer_time_is_utc_plus_three() -> None:
    dt = datetime(2026, 7, 10, 12, 32, tzinfo=UTC)
    assert clock.fmt_local(dt, zone=CAIRO, with_utc=False) == "15:32 Cairo"


def test_date_format_crosses_midnight() -> None:
    dt = datetime(2026, 11, 21, 22, 30, tzinfo=UTC)
    assert clock.fmt_local(dt, clock.DATE_TIME, zone=CAIRO) == "2026-11-22 00:30 Cairo (2026-11-21 22:30 UTC)"


def test_utc_zone_has_no_bracket() -> None:
    dt = datetime(2026, 11, 21, 12, 32, tzinfo=UTC)
    assert clock.fmt_local(dt, zone=UTC) == "12:32 UTC"


def test_naive_datetime_is_taken_as_utc() -> None:
    assert clock.fmt_local(datetime(2026, 11, 21, 12, 32), zone=CAIRO) == "14:32 Cairo (12:32 UTC)"


def test_labels() -> None:
    assert clock.zone_label(CAIRO) == "Cairo"
    assert clock.zone_label(ZoneInfo("America/New_York")) == "New York"
    assert clock.zone_label(UTC) == "UTC"
    assert clock.zone_name(CAIRO) == "Africa/Cairo"
    assert clock.zone_name(UTC) == "UTC"


@pytest.mark.parametrize("name", ["Africa/Cairo", "UTC", "utc", "Europe/London", "America/Los_Angeles"])
def test_valid_zones(name: str) -> None:
    assert clock.is_valid_zone(name)


@pytest.mark.parametrize("name", ["Mars/Base", "", "  ", None, 3, "../../etc/passwd", "Cairo"])
def test_invalid_zones(name: object) -> None:
    assert not clock.is_valid_zone(name)


def test_app_zone_reads_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    _setting(monkeypatch, "Europe/London")
    assert clock.zone_name(clock.app_zone()) == "Europe/London"


def test_app_zone_falls_back_to_cairo(monkeypatch: pytest.MonkeyPatch) -> None:
    _setting(monkeypatch, "Mars/Base")
    assert clock.zone_name(clock.app_zone()) == "Africa/Cairo"
    clock.reset_cache()
    _setting(monkeypatch, 5)
    assert clock.zone_name(clock.app_zone()) == "Africa/Cairo"


def test_app_zone_without_a_database_is_cairo() -> None:
    assert clock.zone_name(clock.app_zone()) == "Africa/Cairo"


def test_app_zone_keeps_the_last_zone_during_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    _setting(monkeypatch, "Asia/Tokyo")
    assert clock.zone_name(clock.app_zone()) == "Asia/Tokyo"

    def broken(key: str, default: Any = None) -> str:
        raise ConnectionError("database down")

    monkeypatch.setattr(settings, "get_str", broken)
    monkeypatch.setattr(clock, "CACHE_TTL_S", 0.0)
    assert clock.zone_name(clock.app_zone()) == "Asia/Tokyo"

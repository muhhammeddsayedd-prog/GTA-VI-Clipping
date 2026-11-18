"""The YouTube quota day starts at midnight Pacific time, DST-correct (pure, no database)."""

from datetime import UTC, datetime

import pytest

from sixstars.discovery import quota


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("now", "start"),
    [
        # Standard time (PST, UTC-8): the day starts 08:00 UTC.
        (utc(2026, 1, 15, 12, 0), utc(2026, 1, 15, 8, 0)),
        # Just before and just after Pacific midnight.
        (utc(2026, 1, 15, 7, 59, 59), utc(2026, 1, 14, 8, 0)),
        (utc(2026, 1, 15, 8, 0, 0), utc(2026, 1, 15, 8, 0)),
        # Back on PST after 2026-11-01.
        (utc(2026, 11, 18, 13, 30), utc(2026, 11, 18, 8, 0)),
        # Daylight time (PDT, UTC-7): the day starts 07:00 UTC.
        (utc(2026, 7, 4, 6, 59), utc(2026, 7, 3, 7, 0)),
        (utc(2026, 7, 4, 7, 0), utc(2026, 7, 4, 7, 0)),
    ],
)
def test_day_start(now: datetime, start: datetime) -> None:
    assert quota.day_start(now) == start


def test_spring_forward_2026_03_08() -> None:
    # Clocks go 02:00 PST -> 03:00 PDT on 2026-03-08. That day began at midnight PST (08:00 UTC) and lasts
    # 23 hours; the next one begins at midnight PDT (07:00 UTC).
    assert quota.day_start(utc(2026, 3, 8, 7, 59)) == utc(2026, 3, 7, 8, 0)
    assert quota.day_start(utc(2026, 3, 8, 8, 0)) == utc(2026, 3, 8, 8, 0)
    assert quota.day_start(utc(2026, 3, 8, 12, 0)) == utc(2026, 3, 8, 8, 0)  # after the switch
    assert quota.next_reset(utc(2026, 3, 8, 12, 0)) == utc(2026, 3, 9, 7, 0)
    assert quota.day_start(utc(2026, 3, 9, 7, 0)) == utc(2026, 3, 9, 7, 0)


def test_fall_back_2026_11_01() -> None:
    # Clocks go 02:00 PDT -> 01:00 PST on 2026-11-01. That day began at midnight PDT (07:00 UTC) and lasts
    # 25 hours; the next one begins at midnight PST (08:00 UTC).
    assert quota.day_start(utc(2026, 11, 1, 6, 59)) == utc(2026, 10, 31, 7, 0)
    assert quota.day_start(utc(2026, 11, 1, 7, 0)) == utc(2026, 11, 1, 7, 0)
    assert quota.day_start(utc(2026, 11, 2, 7, 30)) == utc(2026, 11, 1, 7, 0)  # still Nov 1, 23:30 PST
    assert quota.next_reset(utc(2026, 11, 1, 12, 0)) == utc(2026, 11, 2, 8, 0)
    assert quota.day_start(utc(2026, 11, 2, 8, 0)) == utc(2026, 11, 2, 8, 0)


def test_differs_from_the_utc_day() -> None:
    # 03:00 UTC is still the previous Pacific day: a UTC-day counter would already have reset.
    now = utc(2026, 11, 18, 3, 0)
    assert quota.day_start(now) == utc(2026, 11, 17, 8, 0)
    assert quota.day_start(now, "UTC") == utc(2026, 11, 18, 0, 0)

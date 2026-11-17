"""Pure helpers of the admin console queries: cursors, counts, slot states, search classification, small
statistics. No database."""

import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from sixstars.api import admin_queries as q
from sixstars.posting.models import Account, Lane, Platform

T = datetime(2026, 11, 20, 12, 30, tzinfo=UTC)


def test_cursor_round_trip() -> None:
    cursor = q.encode_cursor(T, "0b6f6f5e-1111-4222-8333-944445555666")
    assert "=" not in cursor and "|" not in cursor
    assert q.decode_cursor(cursor) == (T, "0b6f6f5e-1111-4222-8333-944445555666")
    assert q.decode_cursor(q.encode_cursor(T, 42)) == (T, "42")


def test_cursor_keeps_utc_from_another_offset() -> None:
    other = T.astimezone(UTC).replace(tzinfo=UTC) + timedelta(microseconds=7)
    at, _ = q.decode_cursor(q.encode_cursor(other, 1))
    assert at == other


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "not base64 at all!",
        base64.urlsafe_b64encode(b"no-separator").decode(),
        base64.urlsafe_b64encode(b"2026-11-20T12:00:00|").decode(),
        base64.urlsafe_b64encode(b"not-a-time|1").decode(),
        base64.urlsafe_b64encode(b"2026-11-20T12:00:00|1").decode(),  # no offset
        base64.urlsafe_b64encode(b"\xff\xfe|1").decode(),
    ],
)
def test_bad_cursors_raise_value_error(cursor: str) -> None:
    with pytest.raises(ValueError):
        q.decode_cursor(cursor)


def test_clamp_limit() -> None:
    assert q.clamp_limit(None, 200) == 50
    assert q.clamp_limit(10, 200) == 10
    assert q.clamp_limit(500, 200) == 200
    assert q.clamp_limit(0, 200) == 1
    assert q.clamp_limit(None, 20) == 20  # the default never passes the maximum


def test_shape_counts() -> None:
    rows = [("approved", 3), ("posted", 2), ("odd", 1), (None, 4)]
    counts = q.shape_counts(rows, ("rendering", "approved", "posted"))
    assert counts == {"rendering": 0, "approved": 3, "posted": 2, "odd": 1, "all": 10}
    assert q.shape_counts([], ("a",)) == {"a": 0, "all": 0}


@pytest.mark.parametrize(
    ("hour", "current", "allowed", "post", "state"),
    [
        (5, 12, True, ("published", False), "posted"),
        (5, 12, True, ("removed", False), "posted"),
        (5, 12, True, ("failed", False), "failed"),
        (5, 12, True, ("remove_failed", False), "failed"),
        (5, 12, True, ("skipped", False), "skipped"),
        (5, 12, True, ("skipped", True), "skipped"),  # a dry-run row
        (13, 12, True, ("pending", False), "planned"),
        (5, 12, False, ("published", False), "posted"),  # a post wins over the hour rule
        (5, 12, False, None, "not_allowed"),
        (20, 12, False, None, "not_allowed"),
        (12, 12, True, None, "planned"),
        (20, 12, True, None, "planned"),
        (5, 12, True, None, "empty"),
    ],
)
def test_slot_state(
    hour: int, current: int, allowed: bool, post: tuple[str, bool] | None, state: str
) -> None:
    assert q.slot_state(hour, current, allowed, post) == state


def account(**changes: object) -> Account:
    base: dict[str, object] = {
        "id": "tiktok_main",
        "platform": Platform.TIKTOK,
        "upload_post_user": "u",
        "handle": "h",
        "lanes": frozenset({Lane.HOURLY}),
        "cap_24h": 10,
        "spike_reserve": 2,
        "min_gap_s": 900,
    }
    base.update(changes)
    return Account(**base)  # type: ignore[arg-type]


def test_hour_allowed() -> None:
    assert q.hour_allowed(account(), 3)
    assert q.hour_allowed(account(hourly_hours_utc=frozenset({3})), 3)
    assert not q.hour_allowed(account(hourly_hours_utc=frozenset({4})), 3)
    assert not q.hour_allowed(account(enabled=False), 3)
    assert not q.hour_allowed(account(lanes=frozenset({Lane.SPIKE})), 3)


def test_hour_buckets_oldest_first() -> None:
    now = T
    times = [
        now,
        now - timedelta(minutes=29),
        now - timedelta(hours=1),
        now - timedelta(hours=23, minutes=10),
    ]
    times.append(now - timedelta(hours=30))  # too old, left out
    times.append(now + timedelta(hours=2))  # in the future, left out
    buckets = q.hour_buckets(times, now)
    assert len(buckets) == 24
    assert buckets[-1] == 2
    assert buckets[-2] == 1
    assert buckets[0] == 1
    assert sum(buckets) == 4


def test_percentile() -> None:
    assert q.percentile([], 0.5) is None
    assert q.percentile([10], 0.9) == 10
    assert q.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert q.percentile([5, 1, 3], 0.5) == 3
    assert q.percentile([0, 100], 0.9) == pytest.approx(90)


def test_daily_deltas() -> None:
    day1 = datetime(2026, 11, 18, 10, tzinfo=UTC)
    points = [
        (day1, 100),
        (day1 + timedelta(hours=5), 150),
        (day1 + timedelta(days=1), 400),
        (day1 + timedelta(days=1, hours=2), None),
        (day1 + timedelta(days=3), 1000),
    ]
    assert q.daily_deltas(points) == [
        {"day": "2026-11-19", "views": 250},
        {"day": "2026-11-21", "views": 600},
    ]
    assert q.daily_deltas([]) == []


@pytest.mark.parametrize(
    ("text", "series", "prefix"),
    [
        ("#142", 142, None),
        ("142", 142, None),
        (" 142 ", 142, None),
        ("142857", 142857, "142857"),  # both readings
        ("0b6f6f", None, "0b6f6f"),
        ("0B6F6F5E-1111", None, "0b6f6f5e-1111"),
        ("0b6f6", None, None),  # five hex characters are too few
        ("SomeStreamer", None, None),
        ("#abc", None, None),
        ("", None, None),
    ],
)
def test_classify_search(text: str, series: int | None, prefix: str | None) -> None:
    query = q.classify_search(text)
    assert query.text == text.strip()
    assert query.series_no == series
    assert query.uuid_prefix == prefix


def test_settings_group_and_written_by_code() -> None:
    assert q.settings_group("posting.min_gap_s") == "posting"
    assert q.settings_group("nodot") == "nodot"
    assert "vod.paused_until" in q.WRITTEN_BY_CODE
    assert "posting.min_gap_s" not in q.WRITTEN_BY_CODE


def test_like_escape() -> None:
    assert q.like_escape("50%_off\\") == "50\\%\\_off\\\\"


def test_is_uuid() -> None:
    assert q.is_uuid("0b6f6f5e-1111-4222-8333-944445555666")
    assert not q.is_uuid("0b6f6f5e")
    assert not q.is_uuid("not-a-uuid")
    assert not q.is_uuid("0b6f6f5e-1111-4222-8333-94444555566z")


def test_jsonify() -> None:
    value = {
        "at": datetime(2026, 11, 20, 14, 0, tzinfo=UTC).astimezone(UTC),
        "naive": datetime(2026, 11, 20, 14, 0),
        "n": Decimal("1.5"),
        "id": UUID("0b6f6f5e-1111-4222-8333-944445555666"),
        "list": (1, "a"),
    }
    assert q.jsonify(value) == {
        "at": "2026-11-20T14:00:00+00:00",
        "naive": "2026-11-20T14:00:00+00:00",
        "n": 1.5,
        "id": "0b6f6f5e-1111-4222-8333-944445555666",
        "list": [1, "a"],
    }


def test_number() -> None:
    assert q.number(3) == 3.0
    assert q.number(2.5) == 2.5
    assert q.number(True) is None
    assert q.number("3") is None
    assert q.number(None) is None

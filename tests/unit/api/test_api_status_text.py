"""Pure parts of the status and control routes: the posting mode, the Telegram text and the type rule."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sixstars.api import status
from sixstars.api.routes_control import json_kind, kind_allowed

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def snapshot(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "env": "staging",
        "generated_at": NOW.isoformat(),
        "posting_mode": "live",
        "emergency_stop": False,
        "stop_reason": None,
        "dry_run": False,
        "launch_at": "2026-11-18T11:00:00+00:00",
        "live_sessions": {"active": 3, "max_slots": 6, "by_status": {"assigned": 1, "recording": 2}},
        "approved_queue": {"spike": 1, "hourly": 14, "hourly_low_watermark": 12},
        "posts": {
            "last_hour": {"tiktok_main": 1, "youtube_main": 0},
            "last_24h": {"tiktok_main": 9, "youtube_main": 20},
            "accounts": [
                {
                    "account_id": "tiktok_main",
                    "enabled": True,
                    "paused_until": None,
                    "cap_24h": 12,
                    "published_last_hour": 1,
                    "published_last_24h": 9,
                    "failed_last_24h": 0,
                    "pending": 0,
                    "dry_run_last_24h": 0,
                },
                {
                    "account_id": "youtube_main",
                    "enabled": True,
                    "paused_until": "2026-11-18T13:00:00+00:00",
                    "cap_24h": 28,
                    "published_last_hour": 0,
                    "published_last_24h": 20,
                    "failed_last_24h": 2,
                    "pending": 0,
                    "dry_run_last_24h": 0,
                },
            ],
        },
        "spend_today_usd": {
            "total": 4.5,
            "by_provider": {"anthropic": 4.2, "groq": 0.3},
            "ai_daily_budget": 25,
        },
        "jobs": {
            "queued": {"express": 1, "normal": 7},
            "running": {"express": 0, "normal": 2},
            "failed_last_24h": {"express": 0, "normal": 1},
        },
        "heartbeats": [
            {"name": "scheduler", "kind": "service", "last_seen": "x", "age_s": 12, "stale": False},
            {"name": "trigger:hourly", "kind": "trigger", "last_seen": "x", "age_s": 1900, "stale": False},
            {"name": "worker-normal-a-1", "kind": "service", "last_seen": "x", "age_s": 700, "stale": True},
            {
                "name": "worker-normal-old-9",
                "kind": "service",
                "last_seen": "x",
                "age_s": 9000,
                "stale": True,
            },
        ],
    }
    base.update(changes)
    return base


@pytest.mark.parametrize(
    ("stop", "dry", "launch", "expected"),
    [
        (True, False, NOW - timedelta(hours=1), "stopped"),
        (True, True, None, "stopped"),
        (False, True, NOW - timedelta(hours=1), "dry_run"),
        (False, False, NOW + timedelta(seconds=1), "dry_run"),
        (False, False, None, "dry_run"),
        (False, False, NOW, "live"),
        (False, False, NOW - timedelta(hours=1), "live"),
    ],
)
def test_posting_mode(stop: bool, dry: bool, launch: datetime | None, expected: str) -> None:
    assert status.posting_mode(stop, dry, launch, NOW) == expected


@pytest.mark.parametrize(
    ("age", "text"), [(0, "0s"), (59, "59s"), (60, "1m"), (3599, "59m"), (3900, "1h 05m"), (180000, "2d 2h")]
)
def test_format_age(age: int, text: str) -> None:
    assert status.format_age(age) == text


def test_the_telegram_text_lists_everything_m11_asks_for() -> None:
    text = status.render_telegram(snapshot())
    assert text.splitlines() == [
        "<b>6 Stars Clips status</b> (staging)",
        "Posting: 🟢 live",
        "Live sessions: 3/6 (recording 2)",
        "Approved queue: spike 1 · hourly 14",
        "<b>Posts (last hour / 24 h)</b>",
        "• tiktok_main: 1 / 9 of 12",
        "• youtube_main: 0 / 20 of 28 (paused until 2026-11-18T13:00:00+00:00, failed 2)",
        "Spend today: $4.50 of $25 AI budget (anthropic $4.20 · groq $0.30)",
        "Jobs: express 1 queued, 0 running, 0 failed · normal 7 queued, 2 running, 1 failed",
        "<b>Heartbeats</b>",
        "• scheduler: 12s ago",
        "• trigger:hourly: 31m ago",
        "• worker-normal-a-1: 11m ago ⚠️ stale",
        "• 1 stopped more than 1h 00m ago (not shown)",
    ]
    assert len(text) < 4096  # Telegram's message limit


def test_the_telegram_text_shows_the_stop_and_escapes_what_comes_from_the_database() -> None:
    text = status.render_telegram(
        snapshot(
            posting_mode="stopped",
            emergency_stop=True,
            stop_reason='<b>claim</b> & "strike" <script>',
            heartbeats=[
                {"name": "<i>odd</i>", "kind": "service", "last_seen": "x", "age_s": 5, "stale": False}
            ],
        )
    )
    assert "Posting: 🔴 STOPPED" in text
    assert "Stop reason: &lt;b&gt;claim&lt;/b&gt; &amp; &quot;strike&quot; &lt;script&gt;" in text
    assert "<script>" not in text and "<i>" not in text
    assert "• &lt;i&gt;odd&lt;/i&gt;: 5s ago" in text


def test_the_telegram_text_in_dry_run_with_a_low_queue_and_nothing_else() -> None:
    text = status.render_telegram(
        snapshot(
            posting_mode="dry_run",
            dry_run=True,
            approved_queue={"spike": 0, "hourly": 3, "hourly_low_watermark": 12},
            posts={"last_hour": {}, "last_24h": {}, "accounts": []},
            spend_today_usd={"total": 0.0, "by_provider": {}, "ai_daily_budget": None},
            heartbeats=[],
            live_sessions={"active": 0, "max_slots": None, "by_status": {"assigned": 0, "recording": 0}},
        )
    )
    assert "Posting: 🟡 dry run (nothing is published)" in text
    assert "Launch at: 2026-11-18T11:00:00+00:00 · dry-run switch: on" in text
    assert "Live sessions: 0 (recording 0)" in text
    assert "Approved queue: spike 0 · hourly 3 ⚠️ low" in text
    assert "• no accounts" in text
    assert "Spend today: $0.00" in text
    assert "• none yet" in text


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (None, "null"),
        (True, "boolean"),
        (0, "number"),
        (1.5, "number"),
        ("", "string"),
        ([], "array"),
        ({}, "object"),
    ],
)
def test_json_kind(value: Any, kind: str) -> None:
    assert json_kind(value) == kind


@pytest.mark.parametrize(
    ("old", "new", "allowed"),
    [
        (False, True, True),
        (False, "true", False),
        (False, 1, False),
        (25, 30.5, True),
        (25, True, False),
        (25, "25", False),
        ("a", "b", True),
        ("2026-11-18T11:00:00Z", None, True),
        (None, "reason", True),
        (None, {"a": 1}, True),
        ({"a": 1}, {"b": 2}, True),
        ({"a": 1}, [1], False),
        ([1], [], True),
        ([1], None, False),
        (True, None, False),
        (5, None, False),
    ],
)
def test_a_setting_keeps_its_json_type(old: Any, new: Any, allowed: bool) -> None:
    assert kind_allowed(old, new) is allowed

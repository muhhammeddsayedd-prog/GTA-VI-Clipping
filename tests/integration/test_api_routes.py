"""The control API against Postgres, through FastAPI's test client: health, the hourly trigger, status,
the emergency stop and manual setting changes. Nothing is sent anywhere: the notify webhook is not
configured, so every notification stays in `notify_outbox`."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from posting_support import (
    IG,
    TT,
    TT_LIVE,
    YT,
    add_clip,
    add_creator,
    add_post,
    count,
    production,
    video,
)

from sixstars import config, db, settings, usage
from sixstars.api.main import create_app
from sixstars.posting.slots import slot_start

__all__ = ["production", "video"]  # fixtures

TOKEN = "integration-test-token-0123456789abcdef0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CONTROL_API_TOKEN", TOKEN)
    config.env.cache_clear()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def setting(key: str) -> Any:
    row = db.fetch_one("select value from settings where key = %s", (key,))
    assert row is not None
    return row["value"]


def events_of(event_type: str) -> list[dict[str, Any]]:
    return db.fetch_all("select * from events where type = %s order by id", (event_type,))


def outbox() -> list[tuple[str, str, str]]:
    rows = db.fetch_all("select severity, title, body from notify_outbox order by id")
    return [(row["severity"], row["title"], row["body"]) for row in rows]


def heartbeat_names() -> set[str]:
    return {row["name"] for row in db.fetch_all("select name from heartbeats")}


# ── health ────────────────────────────────────────────────────────────────────────────────────────────────


def test_health_is_ok_with_the_database_up_and_needs_no_token(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "env": "test"}


def test_health_is_503_with_the_database_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing listens on port 9 of this machine: the pool cannot connect.
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:9/nothing")
    config.env.cache_clear()
    db.close_pool()
    response = client.get("/v1/health")
    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "env": "test",
        "error": "database unreachable",
        "code": "db_unreachable",
    }
    assert "127.0.0.1" not in response.text


def test_a_protected_route_is_refused_without_the_token_and_writes_nothing(client: TestClient) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        assert client.post("/v1/control/stop", headers=headers, json={"reason": "x"}).status_code == 401
        assert client.post("/v1/posting/hourly", headers=headers).status_code == 401
    assert setting("control.emergency_stop") is False
    assert count("jobs") == 0 and count("events") == 0 and count("heartbeats") == 0


# ── hourly trigger ────────────────────────────────────────────────────────────────────────────────────────


def test_the_hourly_route_schedules_one_job_however_often_it_is_called(
    production: None, client: TestClient
) -> None:
    before = datetime.now(UTC)
    first = client.post("/v1/posting/hourly", headers=AUTH)
    second = client.post("/v1/posting/hourly", headers=AUTH)
    assert first.status_code == 202 and second.status_code == 202
    a, b = first.json(), second.json()
    if slot_start(datetime.now(UTC)) != slot_start(before):  # the test ran across the top of the hour
        pytest.skip("the hour changed during the test")
    assert set(a) == {"status", "slot_at", "fires_at", "job_id"}
    assert a["status"] == "scheduled" and b["status"] == "already_scheduled"
    assert a["slot_at"] == b["slot_at"] == slot_start(before).isoformat()
    assert a["fires_at"] == b["fires_at"]
    slot, fires = datetime.fromisoformat(a["slot_at"]), datetime.fromisoformat(a["fires_at"])
    assert slot <= fires <= slot + timedelta(seconds=settings.get_int("posting.jitter_max_s"))
    jobs = db.fetch_all("select * from jobs")
    assert len(jobs) == 1
    assert jobs[0]["id"] == a["job_id"] and b["job_id"] is None
    assert (jobs[0]["type"], jobs[0]["queue"], jobs[0]["status"]) == ("post.hourly", "express", "queued")
    assert jobs[0]["run_after"] == fires
    assert "trigger:hourly" in heartbeat_names()


def test_the_hourly_route_queues_nothing_under_the_emergency_stop(
    production: None, client: TestClient
) -> None:
    settings.set("control.emergency_stop", True)
    response = client.post("/v1/posting/hourly", headers=AUTH)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "stopped" and body["fires_at"] is None and body["job_id"] is None
    assert count("jobs") == 0
    assert "trigger:hourly" in heartbeat_names()


def test_the_placeholder_triggers_write_their_heartbeats(client: TestClient) -> None:
    assert client.post("/v1/guardian/check", headers=AUTH).status_code == 501
    assert client.post("/v1/stats/collect", headers=AUTH).status_code == 501
    assert client.post("/v1/guardian/takedown", headers=AUTH, json={"message_id": "m1"}).status_code == 501
    assert client.get("/v1/reports/daily", headers=AUTH).status_code == 501
    assert client.post("/v1/discovery/run", headers=AUTH).status_code in (200, 501)
    assert heartbeat_names() == {"trigger:guardian", "trigger:stats", "trigger:discovery"}


# ── status ────────────────────────────────────────────────────────────────────────────────────────────────


def fill(video: Path) -> None:
    """Two creators, three approved clips, posts in and out of the windows, a live session, spend, beats."""
    now = datetime.now(UTC)
    creator = add_creator()
    queued = add_clip(creator, video)
    add_clip(creator, video, title="second")
    add_clip(creator, video, lane="spike", title="spike one")
    add_clip(creator, video, status="rejected", title="rejected")
    posted = add_clip(creator, video, status="posted", title="posted")
    add_post(posted, YT, now - timedelta(minutes=10))
    add_post(posted, IG, now - timedelta(hours=3))
    add_post(queued, YT, now - timedelta(hours=5), key="k-yt-5h")
    add_post(posted, TT, now - timedelta(hours=30))  # outside both windows
    add_post(queued, IG, now - timedelta(minutes=5), status="failed")
    add_post(queued, TT, now - timedelta(minutes=5), status="skipped", dry_run=True)
    db.execute(
        "insert into live_sessions (creator_id, platform, source_id, channel_login, url, status) values "
        "(%(c)s, 'twitch', 's1', 'a', 'https://example.test/a', 'recording'), "
        "(%(c)s, 'twitch', 's2', 'b', 'https://example.test/b', 'assigned'), "
        "(%(c)s, 'twitch', 's3', 'c', 'https://example.test/c', 'ended')",
        {"c": creator},
    )
    usage.record("anthropic", "clip_writer", cost_usd=1.25)
    usage.record("anthropic", "confirm", cost_usd=0.25)
    usage.record("groq", "transcribe", cost_usd=0.1)
    db.execute("insert into api_usage (provider, operation, cost_usd, created_at) values "
               "('anthropic', 'old', 9, now() - interval '2 days')")  # fmt: skip
    db.heartbeat("scheduler")
    db.heartbeat("trigger:hourly")
    db.execute("insert into heartbeats (name, last_seen) values "
               "('worker-normal-dead-1', now() - interval '20 minutes'), "
               "('worker-normal-ancient-1', now() - interval '3 days'), "
               "('trigger:discovery', now() - interval '3 days')")  # fmt: skip
    db.execute("insert into jobs (queue, type, status) values ('normal', 'a', 'queued'), "
               "('normal', 'a', 'queued'), ('express', 'b', 'running'), ('normal', 'c', 'failed'), "
               "('normal', 'd', 'done')")  # fmt: skip


def test_status_as_json_reports_what_m11_lists(production: None, video: Path, client: TestClient) -> None:
    fill(video)
    response = client.get("/v1/status", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "env",
        "generated_at",
        "posting_mode",
        "emergency_stop",
        "stop_reason",
        "dry_run",
        "launch_at",
        "live_sessions",
        "approved_queue",
        "posts",
        "spend_today_usd",
        "jobs",
        "heartbeats",
    }
    assert body["env"] == "production"
    assert (body["emergency_stop"], body["stop_reason"], body["dry_run"]) == (False, None, True)
    assert body["posting_mode"] == "dry_run"
    assert body["launch_at"] == "2026-11-18T11:00:00+00:00"
    assert body["live_sessions"] == {
        "active": 2,
        "max_slots": settings.get_int("live.max_slots"),
        "by_status": {"assigned": 1, "recording": 1},
    }
    assert body["approved_queue"] == {
        "spike": 1,
        "hourly": 2,
        "hourly_low_watermark": settings.get_int("posting.queue_low_watermark"),
    }
    assert body["posts"]["last_hour"] == {IG: 0, TT_LIVE: 0, TT: 0, YT: 1}
    assert body["posts"]["last_24h"] == {IG: 1, TT_LIVE: 0, TT: 0, YT: 2}
    accounts = {account["account_id"]: account for account in body["posts"]["accounts"]}
    assert accounts[IG]["failed_last_24h"] == 1
    assert accounts[TT]["dry_run_last_24h"] == 1
    assert accounts[YT] == {
        "account_id": YT,
        "enabled": True,
        "paused_until": None,
        "cap_24h": 28,
        "published_last_hour": 1,
        "published_last_24h": 2,
        "failed_last_24h": 0,
        "pending": 0,
        "dry_run_last_24h": 0,
    }
    assert body["spend_today_usd"] == {
        "total": 1.6,
        "by_provider": {"anthropic": 1.5, "groq": 0.1},
        "ai_daily_budget": settings.get_float("ai.daily_budget_usd"),
    }
    assert body["jobs"] == {
        "queued": {"express": 0, "normal": 2},
        "running": {"express": 1, "normal": 0},
        "failed_last_24h": {"express": 0, "normal": 1},
    }
    beats = {beat["name"]: beat for beat in body["heartbeats"]}
    # Triggers are always listed; a service that has been silent for more than a day is dropped.
    assert set(beats) == {"scheduler", "trigger:hourly", "trigger:discovery", "worker-normal-dead-1"}
    assert beats["scheduler"]["stale"] is False and beats["scheduler"]["age_s"] < 60
    assert beats["scheduler"]["kind"] == "service" and beats["trigger:hourly"]["kind"] == "trigger"
    assert beats["worker-normal-dead-1"]["stale"] is True
    assert 1190 <= beats["worker-normal-dead-1"]["age_s"] <= 1260
    assert beats["trigger:discovery"]["stale"] is False  # triggers are judged by the guardian (M13)


def test_status_for_telegram_is_one_html_safe_text(production: None, video: Path, client: TestClient) -> None:
    fill(video)
    settings.set("control.emergency_stop", True)
    settings.set("control.stop_reason", "claim on <video> & more")
    response = client.get("/v1/status?format=telegram", headers=AUTH)
    assert response.status_code == 200
    assert set(response.json()) == {"text"}
    text = response.json()["text"]
    assert text.startswith("<b>6 Stars Clips status</b> (production)\nPosting: 🔴 STOPPED\n")
    assert "Stop reason: claim on &lt;video&gt; &amp; more" in text
    assert "Live sessions: 2/6 (recording 1)" in text
    assert "Approved queue: spike 1 · hourly 2 ⚠️ low" in text
    assert f"• {YT}: 1 / 2 of 28" in text
    assert "Spend today: $1.60 of $25 AI budget (anthropic $1.50 · groq $0.10)" in text
    assert "Jobs: express 0 queued, 1 running, 0 failed · normal 2 queued, 0 running, 1 failed" in text
    assert "• worker-normal-dead-1: 20m ago ⚠️ stale" in text


def test_status_works_on_an_empty_system_and_refuses_an_unknown_format(client: TestClient) -> None:
    db.execute("truncate posts, clips, spikes, creators, platform_accounts restart identity cascade")
    body = client.get("/v1/status?format=json", headers=AUTH).json()
    assert body["posts"] == {"last_hour": {}, "last_24h": {}, "accounts": []}
    assert body["heartbeats"] == [] and body["spend_today_usd"]["total"] == 0
    assert "• none yet" in client.get("/v1/status?format=telegram", headers=AUTH).json()["text"]
    bad = client.get("/v1/status?format=xml", headers=AUTH)
    assert bad.status_code == 422 and bad.json()["code"] == "validation_error"


# ── emergency stop ────────────────────────────────────────────────────────────────────────────────────────


def test_stop_and_start_toggle_the_setting_write_events_and_alert(client: TestClient) -> None:
    stopped = client.post("/v1/control/stop", headers=AUTH, json={"reason": "manual /stop"})
    assert stopped.status_code == 200
    assert stopped.json() == {"stopped": True, "reason": "manual /stop", "changed": True}
    assert setting("control.emergency_stop") is True
    assert setting("control.stop_reason") == "manual /stop"
    assert settings.get_bool("control.emergency_stop") is True  # this process's cache was dropped
    assert client.get("/v1/status", headers=AUTH).json()["posting_mode"] == "stopped"

    started = client.post("/v1/control/start", headers=AUTH)
    assert started.status_code == 200
    assert started.json() == {"stopped": False, "changed": True}
    assert setting("control.emergency_stop") is False
    assert setting("control.stop_reason") is None

    (stop_event,) = events_of("control.stop")
    assert (stop_event["level"], stop_event["service"], stop_event["entity_id"]) == (
        "critical",
        "api",
        "control.emergency_stop",
    )
    assert stop_event["data"] == {
        "old": {"control.emergency_stop": False, "control.stop_reason": None},
        "new": {"control.emergency_stop": True, "control.stop_reason": "manual /stop"},
        "changed": True,
    }
    (start_event,) = events_of("control.start")
    assert start_event["data"]["old"] == {
        "control.emergency_stop": True,
        "control.stop_reason": "manual /stop",
    }
    assert outbox() == [
        ("critical", "Emergency stop ON", "All posting is stopped. Reason: manual /stop"),
        ("critical", "Emergency stop OFF", "Posting may run again. The stop reason was: manual /stop"),
    ]


def test_stop_without_a_body_uses_a_default_reason_and_repeating_it_alerts_once(client: TestClient) -> None:
    first = client.post("/v1/control/stop", headers=AUTH)
    assert first.json() == {"stopped": True, "reason": "manual stop (control API)", "changed": True}
    again = client.post("/v1/control/stop", headers=AUTH, json={"reason": "  second reason  "})
    assert again.json() == {"stopped": True, "reason": "second reason", "changed": False}
    assert setting("control.stop_reason") == "second reason"
    assert len(events_of("control.stop")) == 2  # every call is on record
    assert [title for _, title, _ in outbox()] == ["Emergency stop ON"]
    assert client.post("/v1/control/start", headers=AUTH).json()["changed"] is True
    assert client.post("/v1/control/start", headers=AUTH).json() == {"stopped": False, "changed": False}
    assert [title for _, title, _ in outbox()] == ["Emergency stop ON", "Emergency stop OFF"]


def test_stop_refuses_a_bad_body_and_changes_nothing(client: TestClient) -> None:
    for body in ({"reason": 5}, {"reason": "x" * 501}, {"reason": "x", "extra": 1}, ["x"]):
        assert client.post("/v1/control/stop", headers=AUTH, json=body).status_code == 422
    assert setting("control.emergency_stop") is False
    assert count("events") == 0 and count("notify_outbox") == 0


# ── settings ──────────────────────────────────────────────────────────────────────────────────────────────


def test_a_setting_change_is_stored_recorded_with_old_and_new_and_announced(client: TestClient) -> None:
    old = setting("posting.jitter_max_s")
    response = client.post(
        "/v1/control/settings", headers=AUTH, json={"key": "posting.jitter_max_s", "value": 5}
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"key", "value", "previous", "description", "updated_at", "changed"}
    assert (body["key"], body["value"], body["previous"], body["changed"]) == (
        "posting.jitter_max_s",
        5,
        old,
        True,
    )
    assert body["description"]
    assert setting("posting.jitter_max_s") == 5
    assert settings.get_int("posting.jitter_max_s") == 5
    (event,) = events_of("setting.changed")
    assert (event["service"], event["entity_type"], event["entity_id"]) == (
        "api",
        "setting",
        "posting.jitter_max_s",
    )
    assert event["data"] == {"key": "posting.jitter_max_s", "old": old, "new": 5, "changed": True}
    assert outbox() == [("info", "Setting changed: posting.jitter_max_s", f"{old} → 5")]


def test_an_object_setting_and_a_nullable_one_can_be_changed(client: TestClient) -> None:
    value = {"hourly": 3600, "discovery": 900}
    response = client.post(
        "/v1/control/settings", headers=AUTH, json={"key": "guardian.failover_grace_s", "value": value}
    )
    assert response.status_code == 200 and setting("guardian.failover_grace_s") == value
    for new in ("2026-11-19T00:00:00Z", None):
        response = client.post(
            "/v1/control/settings", headers=AUTH, json={"key": "vod.paused_until", "value": new}
        )
        assert response.status_code == 200 and setting("vod.paused_until") == new


def test_the_same_value_again_is_recorded_but_not_announced(client: TestClient) -> None:
    body = {"key": "launch.dry_run", "value": True}
    response = client.post("/v1/control/settings", headers=AUTH, json=body)
    assert response.status_code == 200 and response.json()["changed"] is False
    assert len(events_of("setting.changed")) == 1 and outbox() == []


def test_an_unknown_key_is_refused_and_no_setting_is_created(client: TestClient) -> None:
    before = count("settings")
    response = client.post(
        "/v1/control/settings", headers=AUTH, json={"key": "posting.no_such_key", "value": 1}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_setting"
    assert count("settings") == before
    assert count("events") == 0 and count("notify_outbox") == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("control.emergency_stop", "false"),
        ("control.emergency_stop", 0),
        ("control.emergency_stop", None),
        ("launch.dry_run", "no"),
        ("posting.jitter_max_s", "240"),
        ("posting.jitter_max_s", True),
        ("guardian.failover_grace_s", [1]),
        ("launch.launch_at", 5),
    ],
)
def test_a_value_of_another_json_type_is_refused(client: TestClient, key: str, value: Any) -> None:
    before = setting(key)
    response = client.post("/v1/control/settings", headers=AUTH, json={"key": key, "value": value})
    assert response.status_code == 422
    assert response.json()["code"] == "type_mismatch"
    assert setting(key) == before
    assert count("events") == 0


@pytest.mark.parametrize(
    "content",
    [
        b'{"key": "launch.dry_run", "value": tru}',
        b'{"key": "launch.dry_run"}',
        b'{"key": "launch.dry_run", "value": NaN}',
        b'{"value": true}',
        b'{"key": "launch.dry_run", "value": true, "description": "x"}',
        b'{"key": "launch.dry_run; drop table settings", "value": true}',
        b'{"key": 7, "value": true}',
        b'"launch.dry_run=false"',
        b"",
    ],
)
def test_a_body_that_is_not_the_expected_json_is_refused(client: TestClient, content: bytes) -> None:
    response = client.post(
        "/v1/control/settings", headers={**AUTH, "Content-Type": "application/json"}, content=content
    )
    assert response.status_code == 422
    assert response.json()["code"] in ("validation_error", "invalid_value")
    assert setting("launch.dry_run") is True
    assert count("events") == 0


def test_a_string_with_a_nul_character_is_refused(client: TestClient) -> None:
    response = client.post(
        "/v1/control/settings", headers=AUTH, json={"key": "control.stop_reason", "value": "a\u0000b"}
    )
    assert response.status_code == 422 and response.json()["code"] == "invalid_value"

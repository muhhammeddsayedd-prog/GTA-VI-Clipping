"""Integration tests for `sixstars.notify`: outbox rows, webhook delivery and rate limiting."""

import json

import httpx
import pytest
import respx

from sixstars import config, db, notify

WEBHOOK = "https://n8n.test/webhook/6stars-notify"


@pytest.fixture(autouse=True)
def webhook_env(database: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_NOTIFY_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("N8N_NOTIFY_SECRET", "test-secret")
    config.env.cache_clear()


def _outbox() -> list[db.Row]:
    return db.fetch_all("select * from notify_outbox order by id")


@respx.mock
def test_send_stores_row_and_marks_it_sent() -> None:
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert notify.send("info", "Clip posted", "details") is True

    rows = _outbox()
    assert len(rows) == 1
    assert (rows[0]["severity"], rows[0]["title"], rows[0]["body"]) == ("info", "Clip posted", "details")
    assert rows[0]["sent_at"] is not None
    assert rows[0]["attempts"] == 1

    request = route.calls.last.request
    assert request.headers["X-6Stars-Secret"] == "test-secret"
    payload = json.loads(request.content)
    assert set(payload) == {"severity", "title", "body", "env", "ts"}
    assert (payload["severity"], payload["title"], payload["body"]) == ("info", "Clip posted", "details")
    assert payload["env"] == "test"
    assert payload["ts"] == rows[0]["created_at"].isoformat()


@respx.mock
def test_webhook_down_leaves_row_unsent_and_does_not_raise() -> None:
    respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("refused"))
    assert notify.send("critical", "Emergency stop", "why") is False
    rows = _outbox()
    assert len(rows) == 1
    assert rows[0]["sent_at"] is None
    assert rows[0]["attempts"] == 1


@respx.mock
def test_webhook_error_status_leaves_row_unsent() -> None:
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500))
    assert notify.send("info", "Hello") is False
    assert _outbox()[0]["sent_at"] is None


@respx.mock
def test_timeout_leaves_row_unsent() -> None:
    respx.post(WEBHOOK).mock(side_effect=httpx.ReadTimeout("slow"))
    assert notify.send("info", "Hello") is False
    assert _outbox()[0]["sent_at"] is None


def test_missing_webhook_url_leaves_row_unsent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("N8N_NOTIFY_WEBHOOK_URL")
    config.env.cache_clear()
    assert notify.send("info", "Hello") is False
    assert _outbox()[0]["sent_at"] is None


def test_database_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(db, "fetch_one", broken)
    assert notify.send("critical", "Hello") is False


@respx.mock
def test_warn_is_rate_limited_per_title() -> None:
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
    assert notify.send("warn", "Queue low") is True
    assert notify.send("warn", "Queue low") is False
    assert notify.send("warn", "Other title") is True
    assert notify.send("critical", "Queue low") is True
    assert notify.send("critical", "Queue low") is True
    assert route.call_count == 4
    assert len(_outbox()) == 4

    db.execute(
        "update notify_outbox set created_at = now() - interval '31 minutes' "
        "where severity = 'warn' and title = 'Queue low'"
    )
    assert notify.send("warn", "Queue low") is True
    assert route.call_count == 5


@respx.mock
def test_unsent_warn_still_counts_for_the_rate_limit() -> None:
    respx.post(WEBHOOK).mock(return_value=httpx.Response(503))
    assert notify.send("warn", "Queue low") is False
    assert notify.send("warn", "Queue low") is False
    assert len(_outbox()) == 1


@respx.mock
def test_retry_unsent_delivers_old_rows() -> None:
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(503))
    notify.send("info", "One")
    notify.send("critical", "Two")
    assert notify.retry_unsent() == 0

    route.mock(return_value=httpx.Response(200))
    assert notify.retry_unsent() == 2
    rows = _outbox()
    assert all(row["sent_at"] is not None for row in rows)
    assert [row["attempts"] for row in rows] == [3, 3]
    assert notify.retry_unsent() == 0

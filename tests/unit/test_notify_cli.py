"""`sixstars notify test`: sends the info notification "Test" and reports whether it was delivered."""

import pytest
from typer.testing import CliRunner

from sixstars import db, notify
from sixstars.cli import app

runner = CliRunner()


def test_notify_test_sends_an_info_notification_named_test(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str, str]] = []

    def fake_send(severity: str, title: str, body: str = "") -> bool:
        sent.append((severity, title, body))
        return True

    monkeypatch.setattr(db, "close_pool", lambda: None)
    monkeypatch.setattr(notify, "send", fake_send)

    result = runner.invoke(app, ["notify", "test"])

    assert result.exit_code == 0
    assert sent == [("info", "Test", "")]
    assert "delivered" in result.output


def test_notify_test_fails_when_the_webhook_did_not_accept_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "close_pool", lambda: None)
    monkeypatch.setattr(notify, "send", lambda severity, title, body="": False)

    result = runner.invoke(app, ["notify", "test"])

    assert result.exit_code == 1
    assert "NOT delivered" in result.output

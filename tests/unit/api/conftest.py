"""Fixtures for the control API unit tests: the application behind FastAPI's test client, a known token
and no database (every call a route makes to the database is replaced in the test that needs it)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sixstars import config, db
from sixstars.api import routes_triggers
from sixstars.api.main import create_app

TOKEN = "unit-test-token-0123456789abcdef0123456789abcdef"


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("CONTROL_API_TOKEN", TOKEN)
    config.env.cache_clear()
    return TOKEN


@pytest.fixture
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def heartbeats(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace `db.heartbeat`; returns the names that were written."""
    names: list[str] = []
    monkeypatch.setattr(db, "heartbeat", lambda name, info=None: names.append(name))
    return names


@pytest.fixture(autouse=True)
def guardian_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[object, ...]]]:
    """Replace the guardian and stats entry points behind the M13 routes (they need the database);
    returns the calls that were made."""
    calls: list[tuple[str, tuple[object, ...]]] = []

    def stub(name: str, answer: dict[str, object]):  # type: ignore[no-untyped-def]
        def call(*args: object) -> dict[str, object]:
            calls.append((name, args))
            return answer

        return call

    service = routes_triggers.guardian_service
    monkeypatch.setattr(
        service, "run_check", stub("run_check", {"status": "ok", "checked": 0, "problems": []})
    )
    monkeypatch.setattr(
        service,
        "handle_takedown",
        stub("handle_takedown", {"is_takedown": False, "status": "ignored", "actions": []}),
    )
    monkeypatch.setattr(
        service, "collect_stats", stub("collect_stats", {"status": "ok", "accounts": 0, "posts": 0})
    )
    monkeypatch.setattr(service, "daily_report", stub("daily_report", {"day": "2026-11-19"}))
    return calls


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client

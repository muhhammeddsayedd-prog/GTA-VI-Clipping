"""Fixtures for the control API unit tests: the application behind FastAPI's test client, a known token
and no database (every call a route makes to the database is replaced in the test that needs it)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sixstars import config, db
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


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client

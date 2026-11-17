"""Shared pytest setup. Tests must never read the developer's real `.env`, so dotenv loading is disabled
and every variable declared on `Env` is removed from the process environment before each test."""

from collections.abc import Iterator

import pytest

from sixstars import config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(config.ENV_FILE_VAR, "")
    for name in config.Env.model_fields:
        monkeypatch.delenv(name, raising=False)
    config.env.cache_clear()
    yield
    config.env.cache_clear()


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the sleep hook of the clients (retry backoff, polling) so no test waits. Returns the list
    of delays that were asked for."""
    from sixstars.clients import _http

    delays: list[float] = []
    monkeypatch.setattr(_http, "sleep", delays.append)
    return delays

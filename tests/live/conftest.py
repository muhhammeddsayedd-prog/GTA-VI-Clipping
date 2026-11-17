"""Live smoke tests: one tiny, read-only call per external service with the real keys.

They are skipped unless `RUN_LIVE_TESTS=1`. Keys are read from the dotenv file named by `LIVE_ENV_FILE`
(default `.env` in the repo root). `DATABASE_URL` is removed for these tests, so nothing is written to
`api_usage` and no setting is read from a real database: every value a client would take from `settings`
is passed in by the test. Nothing is uploaded, posted or deleted. Run with:

    RUN_LIVE_TESTS=1 uv run pytest tests/live -v
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from sixstars import config

REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("RUN_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live tests run only with RUN_LIVE_TESTS=1")
    for item in items:
        if Path(str(item.fspath)).parent.name == "live":
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def sleeps() -> list[float]:
    """Live tests keep the real sleep: polling a real service without waiting would hammer it. This
    overrides the fixture of the same name in the root conftest."""
    return []


@pytest.fixture(autouse=True)
def live_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Load the real keys from the dotenv file, without a database."""
    env_file = Path(os.environ.get("LIVE_ENV_FILE", str(REPO_ROOT / ".env")))
    if not env_file.is_file():
        pytest.skip(f"{env_file} not found: live tests need the real keys")
    monkeypatch.setenv(config.ENV_FILE_VAR, str(env_file))
    monkeypatch.setenv("DATABASE_URL", "")
    config.env.cache_clear()
    yield
    config.env.cache_clear()

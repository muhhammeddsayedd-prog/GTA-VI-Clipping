"""Shared setup for the chat tests: the fixtures directory and the `live` marker.

Tests marked `live` talk to real Twitch and YouTube (read-only) and run only with `RUN_LIVE_TESTS=1`,
so CI never needs the network.
"""

import os
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "chat"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: talks to real Twitch/YouTube; needs RUN_LIVE_TESTS=1")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("RUN_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="network test: set RUN_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR

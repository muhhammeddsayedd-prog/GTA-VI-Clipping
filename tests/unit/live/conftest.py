"""Keeps the unit tests of the live lane running in a whole-suite run.

`tests/unit/chat/conftest.py` skips every test that has the keyword `live` unless `RUN_LIVE_TESTS=1`. It
means the marker `@pytest.mark.live`, but pytest also puts the names of a test's directories among its
keywords, so every test under a directory called `live` is skipped too. The tests here use no network.
This hook runs after that one and takes its skip mark off the tests of this directory (tests that carry
the `live` marker themselves are left alone).
"""

from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
NETWORK_SKIP = "network test"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if HERE not in Path(str(item.path)).resolve().parents:
            continue
        if any(mark.name == "live" for mark in item.own_markers):
            continue
        item.own_markers[:] = [
            mark
            for mark in item.own_markers
            if not (mark.name == "skip" and str(mark.kwargs.get("reason", "")).startswith(NETWORK_SKIP))
        ]

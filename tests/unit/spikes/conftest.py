"""Shared setup for the spike-engine unit tests. No test here touches the network or a database: the
engine's parameters come from the seed migration file, parsed as text, so the tests always run against
the same values a fresh database holds."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from sixstars.spikes.config import SpikeConfig

REPO = Path(__file__).resolve().parents[3]
SEED_SQL = REPO / "supabase" / "migrations" / "0002_seed_settings.sql"
CHAT_FIXTURES = REPO / "tests" / "fixtures" / "chat"
_ROW_RE = re.compile(r"^\s*\('([\w.]+)', '(.*)'::jsonb, '", re.MULTILINE)


def load_seed_values() -> dict[str, Any]:
    """Every `(key, value)` of the seed migration as a `{key: JSON value}` mapping."""
    text = SEED_SQL.read_text(encoding="utf-8")
    return {key: json.loads(raw.replace("''", "'")) for key, raw in _ROW_RE.findall(text)}


@pytest.fixture(scope="session")
def seed_values() -> dict[str, Any]:
    return load_seed_values()


@pytest.fixture
def cfg(seed_values: dict[str, Any]) -> SpikeConfig:
    """The engine parameters exactly as seeded."""
    return SpikeConfig.from_mapping(seed_values)


@pytest.fixture
def chat_fixtures() -> Path:
    return CHAT_FIXTURES

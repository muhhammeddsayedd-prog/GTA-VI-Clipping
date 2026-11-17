"""Fixtures for the AI layer tests: settings without a database and a scripted stand-in for the SDK."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from sixstars import settings, usage
from sixstars.clients.anthropic_client import AnthropicClient

SETTINGS: dict[str, Any] = {
    "ai.model_writer": "claude-sonnet-5-5",
    "ai.model_fast": "claude-haiku-5-5",
    "ai.daily_budget_usd": 25,
    "env.target_game": "Grand Theft Auto V",
    "factory.watermark": "@6starsclipping",
    "gate.spoiler_block_until": "2026-12-03T00:00:00Z",
    "gate.reject_topics": ["real-world politics", "gambling", "sponsored segment"],
    "spikes.s_min_confidence": 0.8,
    "spikes.ab_min_confidence": 0.6,
}


@pytest.fixture(autouse=True)
def ai_settings(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The settings table as a dict the test may edit; today's spend is `values["_spent"]`."""
    values = {**SETTINGS, "_spent": 0.0}
    monkeypatch.setattr(settings, "_all", lambda: values)
    monkeypatch.setattr(usage, "spent_today", lambda provider: values["_spent"])
    monkeypatch.setattr(usage, "record", lambda *args, **kwargs: False)
    return values


class ScriptedSdk:
    """Stand-in for `anthropic.Anthropic`: returns the given reply texts in order."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        text = self.texts[min(len(self.requests), len(self.texts)) - 1]
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            stop_details=None,
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )


@pytest.fixture
def scripted() -> Callable[..., tuple[AnthropicClient, ScriptedSdk]]:
    """`scripted(text1, text2, ...)` gives a real client whose model replies with those texts."""

    def build(*texts: str) -> tuple[AnthropicClient, ScriptedSdk]:
        sdk = ScriptedSdk(list(texts))
        return AnthropicClient(sdk=sdk), sdk

    return build

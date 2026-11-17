"""Anthropic client: JSON extraction, the repair retry, truncation, refusals and cost maths.

The SDK is replaced by a small stand-in with the same `messages.create` surface (the SDK is built on
httpx2, which respx does not intercept).
"""

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from sixstars.clients import anthropic_client
from sixstars.clients._http import NotConfiguredError
from sixstars.clients.anthropic_client import (
    AnthropicClient,
    AnthropicError,
    AnthropicRefusal,
    InvalidModelOutput,
    cost_usd,
    extract_json,
)


class Verdict(BaseModel):
    ok: bool
    confidence: float


def reply(text: str, stop_reason: str = "end_turn", **usage: int) -> SimpleNamespace:
    counts = {"input_tokens": 100, "output_tokens": 20, **usage}
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(**counts),
    )


class FakeSdk:
    def __init__(self, *replies: Any) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ask(sdk: FakeSdk, **overrides: Any) -> Verdict:
    arguments: dict[str, Any] = {
        "prompt_id": "spike_confirm",
        "system": "You judge clips.",
        "user_content": "Is this good?",
        "schema": Verdict,
        "model": "claude-haiku-5-5",
        "max_tokens": 2000,
    }
    arguments.update(overrides)
    return AnthropicClient(sdk=sdk).complete_json(**arguments)


def test_valid_json_is_parsed_and_request_is_shaped_for_current_models() -> None:
    sdk = FakeSdk(reply('{"ok": true, "confidence": 0.9}'))
    assert ask(sdk) == Verdict(ok=True, confidence=0.9)
    request = sdk.requests[0]
    assert request["model"] == "claude-haiku-5-5" and request["max_tokens"] == 2000
    assert request["output_config"] == {"effort": "low"}
    system = request["system"][0]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert system["text"].startswith("You judge clips.") and '"confidence"' in system["text"]
    assert request["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Is this good?"}]}]
    # Rejected by current models: sampling parameters, thinking budgets, assistant prefill.
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(request)


def test_code_fence_and_prose_are_stripped() -> None:
    assert ask(FakeSdk(reply('```json\n{"ok": false, "confidence": 0.1}\n```'))).ok is False
    assert ask(FakeSdk(reply('Here you go: {"ok": true, "confidence": 1} Hope it helps.'))).ok is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ("```json\n[1, 2]\n```", "[1, 2]"),
        ('Sure!\n{"a": {"b": 2}}\nDone', '{"a": {"b": 2}}'),
        ("no json here", "no json here"),
    ],
)
def test_extract_json(text: str, expected: str) -> None:
    assert extract_json(text) == expected


def test_invalid_reply_is_retried_once_with_the_error_appended() -> None:
    sdk = FakeSdk(reply('{"ok": "maybe"}'), reply('{"ok": true, "confidence": 0.7}'))
    assert ask(sdk).confidence == 0.7
    assert len(sdk.requests) == 2
    retry_blocks = sdk.requests[1]["messages"][0]["content"]
    assert retry_blocks[0] == {"type": "text", "text": "Is this good?"}
    repair = retry_blocks[-1]["text"]
    assert '{"ok": "maybe"}' in repair and "confidence" in repair
    # The cached system prefix is identical on both attempts.
    assert sdk.requests[0]["system"] == sdk.requests[1]["system"]


def test_not_json_at_all_is_retried() -> None:
    sdk = FakeSdk(reply("I cannot decide."), reply('{"ok": true, "confidence": 0.5}'))
    assert ask(sdk).ok is True
    assert "not valid JSON" in sdk.requests[1]["messages"][0]["content"][-1]["text"]


def test_second_invalid_reply_raises() -> None:
    sdk = FakeSdk(reply("{}"), reply('{"ok": 1, "confidence": "x"}'))
    with pytest.raises(InvalidModelOutput, match="Verdict"):
        ask(sdk)
    assert len(sdk.requests) == 2


def test_truncated_reply_is_retried_with_twice_the_limit() -> None:
    sdk = FakeSdk(reply('{"ok": tr', stop_reason="max_tokens"), reply('{"ok": true, "confidence": 0.6}'))
    assert ask(sdk).ok is True
    assert sdk.requests[1]["max_tokens"] == 4000
    assert sdk.requests[1]["messages"] == sdk.requests[0]["messages"]


def test_small_max_tokens_leaves_room_for_thinking() -> None:
    sdk = FakeSdk(reply('{"ok": true, "confidence": 1}'))
    ask(sdk, max_tokens=50)
    assert sdk.requests[0]["max_tokens"] == anthropic_client.MIN_MAX_TOKENS


def test_refusal_raises_with_category() -> None:
    refused = reply("", stop_reason="refusal")
    refused.stop_details = SimpleNamespace(category="general_harms")
    sdk = FakeSdk(refused)
    with pytest.raises(AnthropicRefusal) as raised:
        ask(sdk)
    assert raised.value.category == "general_harms" and len(sdk.requests) == 1


def test_images_come_first_and_cache_can_be_switched_off() -> None:
    sdk = FakeSdk(reply('{"ok": true, "confidence": 1}'))
    ask(sdk, images=[b"\xff\xd8jpeg"], cache_system=False, effort=None, model="claude-sonnet-5-5")
    request = sdk.requests[0]
    blocks = request["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/jpeg"
    assert blocks[1]["type"] == "text"
    assert "cache_control" not in request["system"][0] and "output_config" not in request


def test_api_errors_become_anthropic_error() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    status_error = anthropic.APIStatusError("bad", response=httpx2.Response(400, request=request), body=None)
    with pytest.raises(AnthropicError, match="HTTP 400"):
        ask(FakeSdk(status_error))
    with pytest.raises(AnthropicError, match="could not be reached"):
        ask(FakeSdk(anthropic.APIConnectionError(request=request)))


def test_unconfigured_client() -> None:
    client = AnthropicClient()
    assert client.is_configured() is False
    with pytest.raises(NotConfiguredError):
        client.complete_json("p", "s", "u", Verdict, "claude-haiku-5-5", 100)


def test_usage_is_recorded_with_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        anthropic_client.usage,
        "record",
        lambda provider, operation, **kw: rows.append({"p": provider, "o": operation, **kw}),
    )
    sdk = FakeSdk(
        reply(
            '{"ok": true, "confidence": 1}',
            input_tokens=1000,
            output_tokens=200,
            cache_creation_input_tokens=4000,
            cache_read_input_tokens=5000,
        )
    )
    ask(sdk, model="claude-sonnet-5-5", ref_id="clip-1")
    row = rows[0]
    assert (row["p"], row["o"], row["model"], row["ref_id"]) == (
        "anthropic",
        "spike_confirm",
        "claude-sonnet-5-5",
        "clip-1",
    )
    assert row["input_tokens"] == 10000 and row["output_tokens"] == 200
    assert row["cost_usd"] == pytest.approx((1000 * 2 + 4000 * 2.5 + 5000 * 0.10 + 200 * 10) / 1e6)


def test_cost_tables() -> None:
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert cost_usd("claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.50)  # over 100K: long-prompt rates
    assert cost_usd("claude-haiku-5-5", 50_000, 10_000) == pytest.approx(0.005 + 0.005)
    assert cost_usd("claude-haiku-5-5", 60_000, 0, cache_read=60_000) == pytest.approx(0.03 + 0.003)
    # Unknown models are costed at the most expensive known rate.
    assert cost_usd("claude-next", 1_000_000, 0) == pytest.approx(4.0)


def test_system_with_schema_is_deterministic() -> None:
    first = anthropic_client.system_with_schema("Sys ", Verdict)
    assert first == anthropic_client.system_with_schema("Sys ", Verdict)
    assert first.startswith("Sys\n\nReply with exactly one JSON object")

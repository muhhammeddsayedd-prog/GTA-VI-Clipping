"""Anthropic client: structured output requests, the tolerant parser, the repair retries, truncation,
refusals and cost maths.

The SDK is replaced by a small stand-in with the same `messages.create` surface (the SDK is built on
httpx2, which respx does not intercept).
"""

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, Field
from structlog.testing import capture_logs

from sixstars.clients import anthropic_client
from sixstars.clients._http import NotConfiguredError
from sixstars.clients.anthropic_client import (
    AnthropicClient,
    AnthropicError,
    AnthropicRefusal,
    InvalidModelOutput,
    cost_usd,
    first_json,
    output_format,
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
    # The reply shape is enforced by the API: the schema travels as the structured output format, in
    # the API's dialect (every object closed), and the cached system prompt is the prompt alone.
    assert request["output_config"] == {"effort": "low", "format": output_format(Verdict)}
    schema = request["output_config"]["format"]["schema"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False and schema["required"] == ["ok", "confidence"]
    assert request["system"] == [
        {"type": "text", "text": "You judge clips.", "cache_control": {"type": "ephemeral"}}
    ]
    assert "tools" not in request and "tool_choice" not in request
    assert request["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Is this good?"}]}]
    # Rejected by current models: sampling parameters, thinking budgets, assistant prefill.
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(request)


def test_code_fence_and_prose_are_stripped() -> None:
    assert ask(FakeSdk(reply('```json\n{"ok": false, "confidence": 0.1}\n```'))).ok is False
    assert ask(FakeSdk(reply('Here you go: {"ok": true, "confidence": 1} Hope it helps.'))).ok is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ("```json\n[1, 2]\n```", [1, 2]),
        ('Sure!\n{"a": {"b": 2}}\nDone', {"a": {"b": 2}}),
        # The M08 failure "Extra data": a second value after the answer is ignored.
        ('{"a": 1}\n\n{"a": 2}', {"a": 1}),
        ('{"a": "}{"} trailing } braces', {"a": "}{"}),
    ],
)
def test_first_json(text: str, expected: Any) -> None:
    assert first_json(text) == expected


@pytest.mark.parametrize("text", ["no json here", '{"a": 1', ""])
def test_first_json_without_a_complete_value(text: str) -> None:
    with pytest.raises(ValueError):
        first_json(text)


def test_second_json_value_after_the_answer_is_ignored() -> None:
    sdk = FakeSdk(reply('{"ok": true, "confidence": 0.4}\n\n{"ok": false, "confidence": 0.1}'))
    assert ask(sdk) == Verdict(ok=True, confidence=0.4)
    assert len(sdk.requests) == 1


def test_schema_shaped_envelope_is_unwrapped() -> None:
    """The M07 failure: the fast model copied the outline of the JSON Schema around its answer."""
    wrapped = (
        '{"description": "Four frames.", "properties": {"ok": true, "confidence": 0.8}, "required": ["ok"]}'
    )
    sdk = FakeSdk(reply(wrapped))
    assert ask(sdk) == Verdict(ok=True, confidence=0.8)
    assert len(sdk.requests) == 1


def test_envelope_holding_the_schema_itself_is_not_an_answer() -> None:
    copied = '{"properties": {"ok": {"type": "boolean"}, "confidence": {"type": "number"}}}'
    sdk = FakeSdk(reply(copied), reply('{"ok": true, "confidence": 0.5}'))
    assert ask(sdk).confidence == 0.5
    assert len(sdk.requests) == 2


def test_a_model_with_its_own_properties_field_is_not_unwrapped() -> None:
    class Shape(BaseModel):
        properties: dict[str, int]

    sdk = FakeSdk(reply('{"properties": {"a": 1}}'))
    assert ask(sdk, schema=Shape) == Shape(properties={"a": 1})


def test_output_format_uses_the_api_schema_dialect_and_is_the_same_for_bound_subclasses() -> None:
    class Limited(BaseModel):
        score: int = Field(ge=1, le=10)
        note: str = ""

    class Bound(Limited):
        model_config = ConfigDict(title="Limited")

    fmt = output_format(Limited)
    assert fmt == output_format(Bound)
    score = fmt["schema"]["properties"]["score"]
    # Limits the API does not accept move into the description; Pydantic still enforces them here.
    assert "minimum" not in score and "maximum" not in score and "minimum: 1" in score["description"]
    # Every property is required (AI cost plan A6: optional properties made the P2 schema "too complex").
    assert fmt["schema"]["required"] == ["score", "note"]


def test_all_required_keeps_nullable_fields_nullable() -> None:
    class Nested(BaseModel):
        flag: bool = False

    class Reply(BaseModel):
        title: str
        commentary: str | None = None
        nested: Nested = Nested()

    schema = output_format(Reply)["schema"]
    assert schema["required"] == ["title", "commentary", "nested"]
    assert {"type": "null"} in schema["properties"]["commentary"]["anyOf"]
    nested = schema["$defs"]["Nested"]
    assert nested["required"] == ["flag"]


def test_invalid_reply_is_retried_with_the_error_appended() -> None:
    sdk = FakeSdk(reply('{"ok": "maybe"}'), reply('{"ok": true, "confidence": 0.7}'))
    assert ask(sdk).confidence == 0.7
    assert len(sdk.requests) == 2
    retry_blocks = sdk.requests[1]["messages"][0]["content"]
    assert retry_blocks[0] == {"type": "text", "text": "Is this good?"}
    repair = retry_blocks[-1]["text"]
    assert '{"ok": "maybe"}' in repair and "confidence" in repair
    # The cached system prefix and the output format are identical on both attempts.
    assert sdk.requests[0]["system"] == sdk.requests[1]["system"]
    assert sdk.requests[0]["output_config"] == sdk.requests[1]["output_config"]


def test_invalid_reply_is_logged_with_the_reason() -> None:
    sdk = FakeSdk(reply('{"ok": "maybe"}'), reply('{"ok": true, "confidence": 0.7}'))
    with capture_logs() as logs:
        ask(sdk)
    event = next(item for item in logs if item["event"] == "invalid_model_output")
    assert event["prompt_id"] == "spike_confirm" and event["schema"] == "Verdict" and event["attempt"] == 1
    assert "ok" in event["reason"] and "confidence" in event["reason"]


def test_not_json_at_all_is_retried() -> None:
    sdk = FakeSdk(reply("I cannot decide."), reply('{"ok": true, "confidence": 0.5}'))
    assert ask(sdk).ok is True
    assert "not valid JSON" in sdk.requests[1]["messages"][0]["content"][-1]["text"]


def test_second_repair_can_still_succeed() -> None:
    sdk = FakeSdk(reply("{}"), reply('{"ok": true}'), reply('{"ok": true, "confidence": 0.2}'))
    assert ask(sdk).confidence == 0.2
    assert len(sdk.requests) == 3
    # Each repair is built on the original request, with only the latest reply and problem appended.
    last = sdk.requests[2]["messages"][0]["content"]
    assert len(last) == 2 and '{"ok": true}' in last[-1]["text"]


def test_reply_still_invalid_after_the_repairs_raises_with_the_reason() -> None:
    sdk = FakeSdk(reply("{}"), reply('{"ok": 1}'), reply('{"ok": true, "confidence": "x"}'))
    with pytest.raises(InvalidModelOutput, match=r"Verdict: confidence"):
        ask(sdk)
    assert len(sdk.requests) == 1 + anthropic_client.REPAIR_ATTEMPTS


def test_error_names_the_schema_title_of_a_bound_subclass() -> None:
    class Bound(Verdict):
        model_config = ConfigDict(title="Verdict")

    sdk = FakeSdk(*[reply("{}")] * 3)
    with pytest.raises(InvalidModelOutput, match="not valid for Verdict"):
        ask(sdk, schema=Bound)


def test_truncated_reply_is_retried_with_twice_the_limit() -> None:
    sdk = FakeSdk(reply('{"ok": tr', stop_reason="max_tokens"), reply('{"ok": true, "confidence": 0.6}'))
    assert ask(sdk).ok is True
    assert sdk.requests[1]["max_tokens"] == 4000
    assert sdk.requests[1]["messages"] == sdk.requests[0]["messages"]


def test_truncated_twice_doubles_the_limit_only_once() -> None:
    cut = reply('{"ok": tr', stop_reason="max_tokens")
    sdk = FakeSdk(cut, cut, reply('{"ok": true, "confidence": 0.6}'))
    assert ask(sdk).ok is True
    assert [request["max_tokens"] for request in sdk.requests] == [2000, 4000, 4000]


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
    assert "cache_control" not in request["system"][0]
    assert request["output_config"] == {"format": output_format(Verdict)}


def test_api_errors_become_anthropic_error() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    status_error = anthropic.APIStatusError("bad", response=httpx2.Response(529, request=request), body=None)
    with pytest.raises(AnthropicError, match="HTTP 529"):
        ask(FakeSdk(status_error))
    with pytest.raises(AnthropicError, match="could not be reached"):
        ask(FakeSdk(anthropic.APIConnectionError(request=request)))


def bad_request() -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "output_config.format: unsupported", response=httpx2.Response(400, request=request), body=None
    )


def test_rejected_output_format_falls_back_to_the_schema_in_the_system_prompt() -> None:
    sdk = FakeSdk(bad_request(), reply('Here: {"ok": true, "confidence": 0.3}'))
    with capture_logs() as logs:
        assert ask(sdk).confidence == 0.3
    first, second = sdk.requests
    assert "format" in first["output_config"]
    assert second["output_config"] == {"effort": "low"}
    assert second["system"][0]["text"] == anthropic_client.system_with_schema("You judge clips.", Verdict)
    assert second["messages"] == first["messages"]
    events = [item["event"] for item in logs]
    assert "structured_output_rejected" in events
    failed = next(item for item in logs if item["event"] == "api_call_failed")
    assert failed["status"] == 400 and "output_config.format" in failed["message"]


def test_fallback_is_kept_for_the_repair_and_a_second_bad_request_is_an_error() -> None:
    sdk = FakeSdk(bad_request(), reply("{}"), reply('{"ok": true, "confidence": 0.3}'))
    assert ask(sdk).ok is True
    assert [("format" in request["output_config"]) for request in sdk.requests] == [True, False, False]
    with pytest.raises(AnthropicError, match="HTTP 400"):
        ask(FakeSdk(bad_request(), bad_request()))


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

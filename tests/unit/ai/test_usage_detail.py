"""AI cost plan A0: every model request writes one `api_usage` row with its attempt, outcome, stop reason
and cache tokens (migration 0037)."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel, field_validator

from conftest import ScriptedSdk
from sixstars import usage
from sixstars.clients.anthropic_client import AnthropicClient, AnthropicError, AnthropicRefusal


class Answer(BaseModel):
    value: int

    @field_validator("value")
    @classmethod
    def small(cls, value: int) -> int:
        if value > 10:
            raise ValueError("value must be at most 10")
        return value


Rows = list[dict[str, Any]]


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> Rows:
    """The `usage.record` calls, as keyword dicts."""
    captured: Rows = []

    def record(provider: str, operation: str, **kwargs: Any) -> bool:
        captured.append({"provider": provider, "operation": operation, **kwargs})
        return True

    monkeypatch.setattr(usage, "record", record)
    return captured


def ask(client: AnthropicClient, **kwargs: Any) -> Answer:
    return client.complete_json(
        "p2_clip_writer", "system", "user", Answer, "claude-sonnet-5-5", 1024, ref_id="clip-1", **kwargs
    )


def test_a_usable_first_reply_is_one_ok_row(
    scripted: Callable[..., tuple[AnthropicClient, ScriptedSdk]], rows: Rows
) -> None:
    client, _ = scripted('{"value": 3}')
    assert ask(client).value == 3
    assert len(rows) == 1
    detail = rows[0]["detail"]
    assert rows[0]["provider"] == "anthropic"
    assert rows[0]["operation"] == "p2_clip_writer"
    assert rows[0]["ref_id"] == "clip-1"
    assert rows[0]["cost_usd"] > 0
    assert (detail.attempt, detail.outcome, detail.stop_reason) == (1, "ok", "end_turn")
    assert (detail.cache_read_tokens, detail.cache_write_tokens, detail.detail) == (0, 0, None)


def test_a_repaired_reply_writes_an_invalid_row_with_the_failing_check_then_an_ok_row(
    scripted: Callable[..., tuple[AnthropicClient, ScriptedSdk]], rows: Rows
) -> None:
    client, _ = scripted('{"value": 30}', '{"value": 4}')
    assert ask(client).value == 4
    assert [(row["detail"].attempt, row["detail"].outcome) for row in rows] == [(1, "invalid"), (2, "ok")]
    assert "value must be at most 10" in rows[0]["detail"].detail
    assert rows[0]["cost_usd"] > 0  # an unusable reply was still paid for


def test_a_tag_marks_the_row(
    scripted: Callable[..., tuple[AnthropicClient, ScriptedSdk]], rows: Rows
) -> None:
    client, _ = scripted('{"value": 30}', '{"value": 4}')
    ask(client, tag="title_retry")
    assert rows[0]["detail"].detail.startswith("title_retry: ")
    assert rows[1]["detail"].detail == "title_retry"


def test_cache_tokens_are_recorded_and_counted_in_the_input(rows: Rows) -> None:
    sdk = ScriptedSdk(['{"value": 1}'])

    def create(**kwargs: Any) -> SimpleNamespace:
        sdk.requests.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"value": 1}')],
            stop_reason="end_turn",
            stop_details=None,
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=3000,
            ),
        )

    sdk.messages = SimpleNamespace(create=create)
    ask(AnthropicClient(sdk=sdk))
    detail = rows[0]["detail"]
    assert (detail.cache_read_tokens, detail.cache_write_tokens) == (3000, 0)
    assert rows[0]["input_tokens"] == 3100


def test_a_refusal_is_recorded_as_refusal(rows: Rows) -> None:
    def create(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            content=[],
            stop_reason="refusal",
            stop_details=SimpleNamespace(category="cyber"),
            usage=SimpleNamespace(input_tokens=100, output_tokens=5),
        )

    client = AnthropicClient(sdk=SimpleNamespace(messages=SimpleNamespace(create=create)))
    with pytest.raises(AnthropicRefusal):
        ask(client)
    assert len(rows) == 1
    assert (rows[0]["detail"].outcome, rows[0]["detail"].detail) == ("refusal", "cyber")


def test_a_request_that_fails_is_a_zero_cost_http_error_row(rows: Rows) -> None:
    def create(**kwargs: Any) -> SimpleNamespace:
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

    client = AnthropicClient(sdk=SimpleNamespace(messages=SimpleNamespace(create=create)))
    with pytest.raises(AnthropicError):
        ask(client)
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == 0.0
    assert rows[0]["detail"].outcome == "http_error"
    assert rows[0]["detail"].detail == "APIConnectionError"

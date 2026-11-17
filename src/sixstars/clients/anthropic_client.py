"""Client for the Anthropic API (Claude models) used for spike confirmation, clip writing and quality
judgement. Wraps the official SDK with timeouts, retries and cost logging. Implemented in M02.

Sources (checked 2026-11-17):
- Messages API and Python SDK: https://docs.claude.com (SDK `anthropic` 2.9, built on httpx2)
- prices: https://platform.claude.com/docs/en/about-claude/pricing

Facts this module relies on:
- Models come from settings (`ai.model_writer`, `ai.model_fast`); current ids are `claude-sonnet-5-5` and
  `claude-haiku-5-5`. Both run adaptive thinking by default and thinking counts toward `max_tokens`, so a
  response can begin with `thinking` blocks and a small cap can be used up before any text. Depth is set
  with `output_config.effort`; sampling parameters and assistant prefill are rejected by these models.
- A policy decline is HTTP 200 with `stop_reason == "refusal"`: it is raised as `AnthropicRefusal`.
- Structured outputs (https://platform.claude.com/docs/en/build-with-claude/structured-outputs, checked
  2026-11-17): both models accept `output_config.format = {"type": "json_schema", "schema": ...}` and then
  answer with one text block holding JSON of that shape (constrained decoding). Forced tool use
  (`tool_choice` `any` or `tool`) is rejected by `claude-sonnet-5-5`, so the format is the mechanism used
  here. The API schema dialect has no numeric, string-length or array-size limits and wants
  `additionalProperties: false` on every object: `anthropic.transform_schema` rewrites the Pydantic schema
  that way (the dropped limits go into the field descriptions) and the full Pydantic model, with its
  validators, is still applied to the reply here. The format does not change the system prompt, so the
  cached prefix is untouched; the compiled grammar is cached by the API for 24 hours per schema.
- The SDK retries network errors, 408, 409, 429 and 5xx itself with backoff and honours `retry-after`;
  `max_retries=2` gives the three attempts the milestone asks for, so tenacity is not stacked on top.
"""

import base64
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ValidationError

from sixstars import settings, usage
from sixstars.clients._http import (
    CONNECT_TIMEOUT_S,
    MAX_ATTEMPTS,
    READ_TIMEOUT_S,
    ClientError,
    NotConfiguredError,
)
from sixstars.config import env
from sixstars.logging import get_logger

SERVICE = "anthropic"
MIN_MAX_TOKENS = 1024  # room for adaptive thinking before the JSON answer
# How many times a reply that fails validation is sent back with the reason (protocol constant, like
# `MAX_ATTEMPTS` for the HTTP retries). A cut-off reply uses one of them to double `max_tokens` instead.
REPAIR_ATTEMPTS = 2
LOGGED_REASON_CHARS = 300
HAIKU_LONG_PROMPT_TOKENS = 100_000

BudgetKind = Literal["vod", "express"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT: Effort = "low"


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_write_5m: float
    cache_read: float


# https://platform.claude.com/docs/en/about-claude/pricing, read 2026-11-17.
PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0, cache_write_5m=2.5, cache_read=0.10),
    "claude-sonnet-5": ModelPrice(input=2.0, output=10.0, cache_write_5m=2.5, cache_read=0.20),
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_write_5m=0.125, cache_read=0.01),
    "claude-haiku-4-5": ModelPrice(input=1.0, output=5.0, cache_write_5m=1.25, cache_read=0.10),
    "claude-opus-5-5": ModelPrice(input=4.0, output=20.0, cache_write_5m=5.0, cache_read=0.20),
}
# Claude Haiku 5.5 requests whose prompt is over 100,000 tokens pay this rate card instead.
HAIKU_5_5_LONG = ModelPrice(input=0.50, output=2.50, cache_write_5m=0.625, cache_read=0.05)
# Unknown model ids are costed at the most expensive known rate so a budget is never under-counted.
FALLBACK_PRICE = PRICES["claude-opus-5-5"]

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", re.DOTALL)
_JSON_START_RE = re.compile(r"[{\[]")


class AnthropicError(ClientError):
    """The model call failed or its output could not be used."""


class AnthropicBadRequest(AnthropicError):
    """The API rejected the request itself (HTTP 400)."""


class AnthropicRefusal(AnthropicError):
    """The model declined the request (`stop_reason == "refusal"`)."""

    def __init__(self, category: str | None) -> None:
        super().__init__(f"the model declined the request (category: {category or 'unknown'})")
        self.category = category


class InvalidModelOutput(AnthropicError):
    """The reply was not valid JSON for the schema, even after the repair retries."""


def price_for(model: str, prompt_tokens: int = 0) -> ModelPrice:
    """Rate card for `model` (dated or suffixed ids match their base id)."""
    for name, price in PRICES.items():
        if model == name or model.startswith(name + "-"):
            if name == "claude-haiku-5-5" and prompt_tokens > HAIKU_LONG_PROMPT_TOKENS:
                return HAIKU_5_5_LONG
            return price
    return FALLBACK_PRICE


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0
) -> float:
    """Cost of one request. `input_tokens` is the uncached part, as the API reports it."""
    price = price_for(model, input_tokens + cache_write + cache_read)
    total = (
        input_tokens * price.input
        + cache_write * price.cache_write_5m
        + cache_read * price.cache_read
        + output_tokens * price.output
    )
    return total / 1_000_000


def first_json(text: str) -> Any:
    """The first complete JSON value in a model reply.

    A Markdown code fence and prose before the value are skipped, and anything after the value is
    ignored: a second JSON value or a closing remark after the answer does not make the reply unusable.
    Raises `ValueError` (`json.JSONDecodeError`) when there is no complete value.
    """
    cleaned = text.strip()
    fenced = _FENCE_RE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    opening = _JSON_START_RE.search(cleaned)
    if opening is None:
        raise ValueError("no JSON object in the reply")
    value, _ = json.JSONDecoder().raw_decode(cleaned, opening.start())
    return value


def output_format(schema: type[BaseModel]) -> dict[str, Any]:
    """The `output_config.format` value that makes the API answer in the shape of `schema`."""
    return {"type": "json_schema", "schema": anthropic.transform_schema(schema)}


def system_with_schema(system: str, schema: type[BaseModel]) -> str:
    """Append the JSON Schema of `schema` to a system prompt: the fallback used only when the API
    rejects the structured output format. The text is deterministic (sorted keys)."""
    schema_json = json.dumps(schema.model_json_schema(), sort_keys=True, ensure_ascii=False)
    instruction = (
        "Reply with exactly one JSON object that is valid for this JSON Schema. "
        "No prose before or after it and no code fence."
    )
    return f"{system.rstrip()}\n\n{instruction}\n{schema_json}"


def image_block(data: bytes, media_type: str = "image/jpeg") -> dict[str, Any]:
    """Base64 image content block (JPEG frames at most 1568 px on the long side)."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def budget_ok(kind: BudgetKind = "vod") -> bool:
    """False when today's Anthropic spend has reached `ai.daily_budget_usd`. The express (live spike)
    path is always allowed; over budget only the VOD lane pauses."""
    if kind == "express":
        return True
    return usage.spent_today(SERVICE) < settings.get_float("ai.daily_budget_usd")


class AnthropicClient:
    """JSON-in, validated-model-out wrapper around the Messages API."""

    def __init__(self, api_key: str | None = None, sdk: Any | None = None) -> None:
        self._api_key = api_key
        self._sdk = sdk
        self._log = get_logger(SERVICE)

    def is_configured(self) -> bool:
        """True when an API key is available."""
        return self._sdk is not None or bool(self._api_key) or env().ANTHROPIC_API_KEY is not None

    def _client(self) -> Any:
        if self._sdk is None:
            secret = env().ANTHROPIC_API_KEY
            key = self._api_key or (secret.get_secret_value() if secret is not None else None)
            if not key:
                raise NotConfiguredError("ANTHROPIC_API_KEY is not set")
            self._sdk = anthropic.Anthropic(
                api_key=key,
                timeout=anthropic.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                max_retries=MAX_ATTEMPTS - 1,
            )
        return self._sdk

    def _call(
        self,
        prompt_id: str,
        model: str,
        system: str,
        content: list[dict[str, Any]],
        max_tokens: int,
        cache_system: bool,
        effort: Effort | None,
        ref_id: str | None,
        response_format: dict[str, Any] | None = None,
    ) -> tuple[str, str | None]:
        """One Messages request. Returns `(text, stop_reason)` and records tokens and cost.

        `response_format` is the structured output format (`output_format(schema)`), or None to ask in
        free text."""
        system_block: dict[str, Any] = {"type": "text", "text": system}
        if cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [system_block],
            "messages": [{"role": "user", "content": content}],
        }
        output_config: dict[str, Any] = {}
        if effort is not None:
            output_config["effort"] = effort
        if response_format is not None:
            output_config["format"] = response_format
        if output_config:
            kwargs["output_config"] = output_config
        started = time.monotonic()
        try:
            response = self._client().messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            self._log.warning(
                "api_call_failed",
                prompt_id=prompt_id,
                status=exc.status_code,
                message=str(exc.message)[:LOGGED_REASON_CHARS],
            )
            kind = AnthropicBadRequest if exc.status_code == 400 else AnthropicError
            raise kind(f"Anthropic API returned HTTP {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            self._log.warning("api_call_failed", prompt_id=prompt_id, error=type(exc).__name__)
            raise AnthropicError("Anthropic API could not be reached") from exc

        used = response.usage
        input_tokens = int(getattr(used, "input_tokens", 0) or 0)
        output_tokens = int(getattr(used, "output_tokens", 0) or 0)
        cache_write = int(getattr(used, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(used, "cache_read_input_tokens", 0) or 0)
        cost = cost_usd(model, input_tokens, output_tokens, cache_write, cache_read)
        usage.record(
            SERVICE,
            prompt_id,
            model=model,
            input_tokens=input_tokens + cache_write + cache_read,
            output_tokens=output_tokens,
            cost_usd=cost,
            ref_id=ref_id,
        )
        self._log.info(
            "api_call",
            endpoint="messages",
            prompt_id=prompt_id,
            model=model,
            stop_reason=response.stop_reason,
            input_tokens=input_tokens,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AnthropicRefusal(getattr(details, "category", None))
        text = "".join(block.text for block in response.content if block.type == "text")
        return text, response.stop_reason

    def complete_json[T: BaseModel](
        self,
        prompt_id: str,
        system: str,
        user_content: str | Sequence[dict[str, Any]],
        schema: type[T],
        model: str,
        max_tokens: int,
        images: Sequence[bytes] | None = None,
        cache_system: bool = True,
        effort: Effort | None = DEFAULT_EFFORT,
        ref_id: str | None = None,
    ) -> T:
        """Ask `model` for JSON matching `schema` and return the validated object.

        `prompt_id` names the prompt in logs and in `api_usage.operation`. `user_content` is text or a
        list of content blocks; `images` are JPEG bytes placed before the text. The static `system`
        prompt is cached when `cache_system` is true.

        The reply shape is enforced by the API (structured outputs: `output_config.format` carries the
        JSON Schema of `schema`), and the reply is then validated with the Pydantic model, whose
        validators hold the limits a JSON Schema cannot (word counts, time bounds). A reply that fails
        is sent back with the reason, up to `REPAIR_ATTEMPTS` times; a reply cut off by `max_tokens` is
        retried once with twice the limit. If the API rejects the format itself (HTTP 400) the request
        falls back to the schema written into the system prompt and free-text parsing. Raises
        `InvalidModelOutput` when the last reply is still unusable and `AnthropicRefusal` when the model
        declines.
        """
        content: list[dict[str, Any]] = [image_block(image) for image in images or ()]
        if isinstance(user_content, str):
            content.append({"type": "text", "text": user_content})
        else:
            content.extend(user_content)
        limit = max(max_tokens, MIN_MAX_TOKENS)
        name = _schema_name(schema)
        response_format: dict[str, Any] | None
        try:
            response_format = output_format(schema)
        except (ValueError, TypeError) as exc:
            self._log.warning(
                "structured_output_unavailable", prompt_id=prompt_id, schema=name, error=str(exc)
            )
            response_format = None

        def ask(blocks: list[dict[str, Any]], cap: int) -> tuple[str, str | None]:
            nonlocal response_format
            if response_format is not None:
                try:
                    return self._call(
                        prompt_id, model, system, blocks, cap, cache_system, effort, ref_id, response_format
                    )
                except AnthropicBadRequest:
                    self._log.warning("structured_output_rejected", prompt_id=prompt_id, model=model)
                    response_format = None
            legacy = system_with_schema(system, schema)
            return self._call(prompt_id, model, legacy, blocks, cap, cache_system, effort, ref_id)

        blocks = content
        doubled = False
        problem = ""
        for attempt in range(1 + REPAIR_ATTEMPTS):
            text, stop_reason = ask(blocks, limit)
            try:
                return _parse(schema, text)
            except (ValidationError, ValueError) as error:
                problem = _describe(error)
            self._log.warning(
                "invalid_model_output",
                prompt_id=prompt_id,
                model=model,
                schema=name,
                stop_reason=stop_reason,
                attempt=attempt + 1,
                reply_chars=len(text),
                reason=problem[:LOGGED_REASON_CHARS],
            )
            if stop_reason == "max_tokens" and not doubled:
                limit *= 2
                doubled = True
                continue
            repair = (
                "Your previous reply could not be used.\n\n"
                f"Previous reply:\n{text[:4000]}\n\n"
                f"Problem:\n{problem}\n\n"
                "Reply again with the whole JSON object, corrected so that this problem is gone. "
                "Check the corrected value against the limit before you answer."
            )
            blocks = [*content, {"type": "text", "text": repair}]
        raise InvalidModelOutput(f"{prompt_id}: reply is not valid for {name}: {problem}")


def _schema_name(schema: type[BaseModel]) -> str:
    """The schema's title: the per-call subclasses of `ai.schemas` keep the title of their base model."""
    return str(schema.model_config.get("title") or schema.__name__)


def _parse[T: BaseModel](schema: type[T], text: str) -> T:
    """Validate the first JSON value of `text`. An answer wrapped the way a JSON Schema is laid out
    (`{"description": ..., "properties": {<the answer>}}`, seen from the fast model before structured
    outputs) is unwrapped when the wrapper itself is not valid."""
    if not text.strip():
        raise ValueError("the reply contained no text")
    data = first_json(text)
    try:
        return schema.model_validate(data)
    except ValidationError:
        inner = data.get("properties") if isinstance(data, dict) else None
        if not isinstance(inner, dict):
            raise
        try:
            return schema.model_validate(inner)
        except ValidationError:
            pass
        raise


def _describe(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '(root)'}: {item['msg']}"
            for item in error.errors()
        )[:1500]
    return f"not valid JSON: {error}"[:1500]

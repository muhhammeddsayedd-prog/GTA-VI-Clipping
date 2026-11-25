"""OpenAI image generation (GPT Image models): the long-form episode thumbnail (M20 go-live).

Docs checked 2026-11-25 (docs/14 "OpenAI Images"):
- https://developers.openai.com/api/reference/resources/images/methods/edit
  `POST https://api.openai.com/v1/images/edits`, multipart: `model`, `prompt`, one or more `image[]` files
  (up to 16), `size` (1024x1024, 1536x1024, 1024x1536, auto; GPT Image 2.x also any WIDTHxHEIGHT with edges
  divisible by 16, aspect 1:3..3:1, max 3840x2160), `quality` (low, medium, high, auto), `output_format`
  (png, jpeg, webp), `output_compression` (0-100, jpeg/webp), `n`, `moderation` (auto, low).
  The reply carries `data[0].b64_json` (no URLs for GPT Image models) and `usage` (input_tokens with
  image/text detail, output_tokens).
- https://developers.openai.com/api/docs/guides/image-generation: complex prompts may take up to 2 minutes;
  a refusal is an error with code `moderation_blocked`. gpt-image-1 was shut down on 2026-10-23.
- https://developers.openai.com/api/docs/pricing: GPT Image 2.x per 1M tokens: image input $8, text input
  $5, image output $30.

Retries follow `_http` (429 and 5xx, max 3). A moderation refusal is never retried: it raises
`ImageRefusedError` so the caller can try again without the creator picture, then fall back.
"""

import base64
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from sixstars import usage
from sixstars.clients import _http
from sixstars.clients._http import ApiError, ClientError, NotConfiguredError
from sixstars.config import env
from sixstars.logging import get_logger

SERVICE = "openai_images"
EDITS_URL = "https://api.openai.com/v1/images/edits"
ENDPOINT = "POST /v1/images/edits"
# USD per 1M tokens (GPT Image 2.x, pricing page 2026-11-25).
USD_PER_M_IMAGE_IN = 8.0
USD_PER_M_TEXT_IN = 5.0
USD_PER_M_IMAGE_OUT = 30.0
REFUSED_CODES = frozenset({"moderation_blocked", "content_policy_violation"})


class ImageRefusedError(ClientError):
    """The image model refused the request (content policy)."""


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    output_format: str
    cost_usd: float


def _count(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0


def cost_usd(usage_json: object) -> float:
    """USD of one call from its `usage` block (0 when absent). Pure."""
    if not isinstance(usage_json, dict):
        return 0.0
    details = usage_json.get("input_tokens_details")
    image_in = _count(details.get("image_tokens")) if isinstance(details, dict) else 0
    text_in = max(_count(usage_json.get("input_tokens")) - image_in, 0)
    out = _count(usage_json.get("output_tokens"))
    usd = (image_in * USD_PER_M_IMAGE_IN + text_in * USD_PER_M_TEXT_IN + out * USD_PER_M_IMAGE_OUT) / 1e6
    return round(float(usd), 6)


def is_refusal(exc: ApiError) -> bool:
    text = f"{exc.message}".lower()
    return exc.status == 400 and (any(code in text for code in REFUSED_CODES) or "safety" in text)


class OpenAIImagesClient:
    """Image edits with input images. `timeout_s` is the read timeout of one attempt."""

    def __init__(
        self, api_key: str | None = None, http: httpx.Client | None = None, timeout_s: float = 240.0
    ):
        self._key = api_key
        self._http = http or _http.new_client(httpx.Timeout(timeout_s, connect=_http.CONNECT_TIMEOUT_S))
        self._log = get_logger(SERVICE)

    def _api_key(self) -> str:
        if self._key:
            return self._key
        secret = env().OPENAI_API_KEY
        if secret is None:
            raise NotConfiguredError("OPENAI_API_KEY is not set")
        return secret.get_secret_value()

    def is_configured(self) -> bool:
        return bool(self._key) or env().OPENAI_API_KEY is not None

    def edit(
        self,
        images: Sequence[bytes],
        prompt: str,
        *,
        model: str,
        size: str,
        quality: str,
        output_format: str = "jpeg",
        ref_id: str | None = None,
    ) -> GeneratedImage:
        """One image from `prompt` and the input images (JPEG or PNG bytes)."""
        if not images:
            raise ValueError("at least one input image is needed")
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        data = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "n": "1",
        }

        def call() -> httpx.Response:
            files = [("image[]", (f"in{k}.jpg", img, "image/jpeg")) for k, img in enumerate(images)]
            return self._http.post(EDITS_URL, data=data, files=files, headers=headers)

        response = _http.send(SERVICE, ENDPOINT, call)
        try:
            _http.raise_for_status(SERVICE, ENDPOINT, response)
        except ApiError as exc:
            if is_refusal(exc):
                raise ImageRefusedError(exc.message or "refused") from exc
            raise
        body = response.json()
        items = body.get("data") or []
        b64 = items[0].get("b64_json") if items and isinstance(items[0], dict) else None
        if not b64:
            raise ApiError(SERVICE, ENDPOINT, response.status_code, "reply has no image")
        cost = cost_usd(body.get("usage"))
        usage.record(SERVICE, "image_edit", model=model, units=1, cost_usd=cost, ref_id=ref_id)
        self._log.info("image_generated", model=model, size=size, quality=quality, cost_usd=cost)
        return GeneratedImage(base64.b64decode(b64), str(body.get("output_format") or output_format), cost)

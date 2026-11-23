"""Shadow runs of cheaper models next to the production model: AI cost plan B1 (D117).

After a production call of a shadowed prompt (P2 clip writer, P9V visual judge), a sample of the calls
(`ai.shadow_rate`) queues an `ai.shadow` job. The job asks each model of `ai.shadow.<prompt_id>` the same
prompt (rendered again from the same values; P9V frames cut again from the rendered clip at the same times)
and stores both answers in `ai_shadow`. Nothing it says reaches production.

- Async: the job runs on the `normal` queue at low priority; express clips never queue one.
- Own budget: shadow requests are recorded under provider `anthropic_shadow`, which the production AI
  budget (`ai.daily_budget_usd`, provider `anthropic`) does not count; `ai.shadow_daily_budget_usd` caps them.
- `scripts/shadow_report.py` compares the answers.
"""

import random
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from psycopg.types.json import Jsonb

from sixstars import db, jobs, settings, usage
from sixstars.logging import get_logger

JOB: Final = "ai.shadow"
JOB_PRIORITY: Final = -10
PROVIDER: Final = "anthropic_shadow"
LIST_PREFIX: Final = "ai.shadow."
RATE_SETTING: Final = "ai.shadow_rate"
BUDGET_SETTING: Final = "ai.shadow_daily_budget_usd"

_log = get_logger("ai.shadow")


def models_for(prompt_id: str) -> list[str]:
    """The model ids of `ai.shadow.<prompt_id>` (`"anthropic:<model>"` entries; others are skipped)."""
    from sixstars.ai.calls import parse_model_setting

    raw = settings.get(LIST_PREFIX + prompt_id, None)
    if not isinstance(raw, list):
        return []
    return [model for model in (parse_model_setting(item) for item in raw) if model is not None]


def wanted(prompt_id: str, lane: str, draw: Callable[[], float] = random.random) -> list[str]:
    """The shadow models to ask for this call: none for an express call or outside the sample."""
    if lane == "express":
        return []
    models = models_for(prompt_id)
    if not models or draw() >= settings.get_float(RATE_SETTING, 0.0):
        return []
    return models


def maybe_enqueue(
    prompt_id: str,
    *,
    lane: str,
    ref_id: str | None,
    values: Mapping[str, object],
    reply_model: type[Any],
    primary_model: str,
    primary_output: Any,
    frames: Mapping[str, Any] | None = None,
) -> int | None:
    """Queue an `ai.shadow` job for this production call when it is in the sample. `reply_model` is the
    Pydantic reply model of the call (its JSON schema goes with the job); `primary_output` the reply (a model
    or a mapping). Never raises."""
    try:
        models = wanted(prompt_id, lane)
        if not models:
            return None
        return jobs.enqueue(
            "normal",
            JOB,
            {
                "prompt_id": prompt_id,
                "ref_id": ref_id,
                "models": models,
                "values": dict(values),
                "schema": schema_of(reply_model),
                "primary_model": primary_model,
                "primary_output": primary_output
                if isinstance(primary_output, Mapping)
                else as_json(primary_output),
                "frames": dict(frames) if frames is not None else None,
            },
            priority=JOB_PRIORITY,
            dedupe_key=f"{JOB}:{prompt_id}:{ref_id}" if ref_id else None,
            max_attempts=1,
        )
    except Exception as exc:  # noqa: BLE001 - a shadow run must never cost a production call
        _log.warning("shadow_enqueue_failed", prompt_id=prompt_id, error=f"{type(exc).__name__}: {exc}"[:300])
        return None


def budget_left() -> bool:
    return usage.spent_today(PROVIDER) < settings.get_float(BUDGET_SETTING, 0.0)


def _frames(spec: Mapping[str, Any] | None, grab: Callable[[Path, float], bytes]) -> list[bytes] | None:
    """The P9V frames, cut again from the rendered clip at the same times; None when the file is gone."""
    if spec is None:
        return None
    path = Path(str(spec.get("path", "")))
    times = [float(value) for value in spec.get("times", [])]
    if not path.is_file() or not times:
        return None
    return [grab(path, time_s) for time_s in times]


def _store(
    payload: Mapping[str, Any], model: str, output: Any, cost: float, latency_ms: int, error: str | None
) -> None:
    db.execute(
        """
        insert into ai_shadow
          (prompt_id, ref_id, primary_model, shadow_model, primary_output, shadow_output, valid_json,
           cost_usd, latency_ms, error)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            payload["prompt_id"],
            payload.get("ref_id"),
            payload.get("primary_model"),
            model,
            Jsonb(payload.get("primary_output")),
            Jsonb(output) if output is not None else None,
            output is not None,
            cost,
            latency_ms,
            error,
        ),
    )


def handle(
    job: jobs.Job, *, client: Any | None = None, grab: Callable[[Path, float], bytes] | None = None
) -> None:
    """Job `ai.shadow`: ask each shadow model and store the answers (nothing over the shadow budget)."""
    from sixstars.ai import calls, prompt_files
    from sixstars.ai.images import prepare_jpeg
    from sixstars.clients.anthropic_client import AnthropicClient
    from sixstars.factory import tools

    payload = job.payload
    prompt_id = str(payload["prompt_id"])
    if not budget_left():
        _log.info("shadow_budget_used_up", prompt_id=prompt_id)
        return
    images: list[bytes] | None = None
    if payload.get("frames") is not None:
        raw = _frames(payload["frames"], grab or (lambda path, time_s: tools.grab_frame(path, time_s)))
        if raw is None:
            for model in payload.get("models", []):
                _store(payload, str(model), None, 0.0, 0, "the rendered clip is gone")
            return
        images = [prepare_jpeg(frame, max_side=calls.VISUAL_SCORE_MAX_SIDE_PX).data for frame in raw]
    prompt = prompt_files.load(prompt_id)
    values = dict(payload.get("values") or {})
    system = prompt.render_system(calls.system_values())
    user = prompt.render_user(values)
    runner = client or AnthropicClient()
    for model in payload.get("models", []):
        if not budget_left():
            break
        result = runner.complete_raw_json(
            prompt_id,
            system,
            user,
            dict(payload["schema"]),
            str(model),
            calls.MAX_TOKENS[prompt_id],
            images=images,
            effort=calls.EFFORT[prompt_id],
            ref_id=payload.get("ref_id"),
            service=PROVIDER,
        )
        _store(payload, str(model), result.data, result.cost, result.latency_ms, result.error)


def register_handlers() -> None:
    """Register `ai.shadow` with the queue worker."""
    from sixstars.services import worker

    if JOB not in worker.HANDLERS:
        worker.register(JOB, handle)


def schema_of(model: type[Any]) -> dict[str, Any]:
    """The JSON schema the API is given for a Pydantic reply model (as the client sends it)."""
    from sixstars.clients.anthropic_client import output_format

    schema = output_format(model)["schema"]
    assert isinstance(schema, dict)
    return schema


def as_json(value: Any) -> dict[str, Any]:
    """A reply model as plain JSON."""
    dumped = value.model_dump(mode="json")
    assert isinstance(dumped, dict)
    return dumped


__all__: Sequence[str] = (
    "JOB",
    "PROVIDER",
    "as_json",
    "handle",
    "maybe_enqueue",
    "models_for",
    "register_handlers",
    "schema_of",
    "wanted",
)

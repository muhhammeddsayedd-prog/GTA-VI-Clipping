"""AI cost plan B1: shadow runs of a cheaper model next to the production one."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sixstars import db, jobs, usage
from sixstars.ai import calls, shadow
from sixstars.ai.schemas import VisualClipScore
from sixstars.clients.anthropic_client import RawResult


@pytest.fixture
def shadow_on(ai_settings: dict[str, Any]) -> dict[str, Any]:
    ai_settings["ai.shadow.p9_visual_score"] = ["anthropic:claude-haiku-5-5"]
    ai_settings["ai.shadow.p2_clip_writer"] = ["anthropic:claude-haiku-5-5", "openrouter:x/y"]
    ai_settings["ai.shadow_rate"] = 0.2
    ai_settings["ai.shadow_daily_budget_usd"] = 2
    return ai_settings


def test_only_callable_models_are_shadowed(shadow_on: dict[str, Any]) -> None:
    assert shadow.models_for(calls.P2) == ["claude-haiku-5-5"]
    assert shadow.models_for(calls.P5) == []


def test_the_sample_rate_and_the_express_lane_decide(shadow_on: dict[str, Any]) -> None:
    assert shadow.wanted(calls.P9V, "vod", draw=lambda: 0.1) == ["claude-haiku-5-5"]
    assert shadow.wanted(calls.P9V, "vod", draw=lambda: 0.5) == []
    assert shadow.wanted(calls.P9V, "express", draw=lambda: 0.0) == []
    shadow_on["ai.shadow_rate"] = 0
    assert shadow.wanted(calls.P9V, "vod", draw=lambda: 0.0) == []


def test_enqueue_never_raises(shadow_on: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shadow, "wanted", lambda prompt_id, lane: ["claude-haiku-5-5"])

    def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("database down")

    monkeypatch.setattr(jobs, "enqueue", broken)
    assert (
        shadow.maybe_enqueue(
            calls.P9V,
            lane="vod",
            ref_id="clip-1",
            values={},
            reply_model=VisualClipScore,
            primary_model="claude-sonnet-5-5",
            primary_output={"score": 7},
        )
        is None
    )


def test_enqueue_carries_the_schema_and_the_reply(
    shadow_on: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shadow, "wanted", lambda prompt_id, lane: ["claude-haiku-5-5"])
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(jobs, "enqueue", lambda queue, kind, payload, **kw: queued.append(payload) or 1)
    reply = VisualClipScore.model_validate(
        {"seen": "A jump.", "score_parts": {"hook": 7, "payoff": 7, "clarity": 7, "rewatch": 6}, "score": 7}
    )
    shadow.maybe_enqueue(
        calls.P9V,
        lane="vod",
        ref_id="clip-1",
        values={"count": 8},
        reply_model=VisualClipScore,
        primary_model="claude-sonnet-5-5",
        primary_output=reply,
        frames={"path": "/x.mp4", "times": [1.0]},
    )
    (payload,) = queued
    assert payload["schema"]["type"] == "object" and payload["primary_output"]["score"] == 7
    assert payload["models"] == ["claude-haiku-5-5"] and payload["frames"]["times"] == [1.0]


class RawClient:
    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []

    def complete_raw_json(
        self,
        prompt_id: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        **kw: Any,
    ) -> RawResult:  # noqa: E501
        self.asked.append({"prompt_id": prompt_id, "model": model, "user": user, **kw})
        return RawResult({"score": 6}, 0.002, None, 900)


def job(payload: dict[str, Any]) -> jobs.Job:
    return SimpleNamespace(id=1, payload=payload)  # type: ignore[return-value]


def test_the_job_asks_each_model_on_its_own_budget(
    shadow_on: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stored: list[tuple[Any, ...]] = []
    monkeypatch.setattr(db, "execute", lambda sql, params: stored.append(params) or 1)
    spent: list[str] = []
    monkeypatch.setattr(usage, "spent_today", lambda provider: spent.append(provider) or 0.0)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    client = RawClient()
    payload = {
        "prompt_id": calls.P9V,
        "ref_id": "clip-1",
        "models": ["claude-haiku-5-5"],
        "values": calls.visual_values(calls.RenderedClip(hook="Wow", length_s=20, lines=()), [1.0, 2.0]),
        "schema": shadow.schema_of(VisualClipScore),
        "primary_model": "claude-sonnet-5-5",
        "primary_output": {"score": 7},
        "frames": {"path": str(clip), "times": [1.0, 2.0]},
    }
    shadow.handle(job(payload), client=client, grab=lambda path, time_s: _jpeg())
    assert set(spent) == {shadow.PROVIDER}  # never the production budget
    (asked,) = client.asked
    assert asked["model"] == "claude-haiku-5-5" and len(asked["images"]) == 2
    assert asked["service"] == shadow.PROVIDER
    (row,) = stored
    assert row[0] == calls.P9V and row[3] == "claude-haiku-5-5" and row[6] is True


def test_nothing_is_asked_over_the_shadow_budget(
    shadow_on: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage, "spent_today", lambda provider: 2.0)
    client = RawClient()
    shadow.handle(job({"prompt_id": calls.P2, "models": ["claude-haiku-5-5"]}), client=client)
    assert client.asked == []


def test_a_clip_whose_file_is_gone_is_recorded_without_a_call(
    shadow_on: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    stored: list[tuple[Any, ...]] = []
    monkeypatch.setattr(db, "execute", lambda sql, params: stored.append(params) or 1)
    monkeypatch.setattr(usage, "spent_today", lambda provider: 0.0)
    client = RawClient()
    payload = {
        "prompt_id": calls.P9V,
        "models": ["claude-haiku-5-5"],
        "values": {},
        "frames": {"path": "/nowhere.mp4", "times": [1.0]},
    }
    shadow.handle(job(payload), client=client)
    assert client.asked == [] and stored[0][-1] == "the rendered clip is gone"


def _jpeg() -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(out, format="JPEG")
    return out.getvalue()

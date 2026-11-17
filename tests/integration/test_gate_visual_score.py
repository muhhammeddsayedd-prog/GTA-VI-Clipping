"""The gate with the visual score switched on (`ai.visual_score`, migration 0016), against the test
database. Files are real (ffmpeg); Claude is the fake client, so the score it "sees" is scripted."""

from pathlib import Path
from typing import Any

import pytest
from gate_support import (
    P5_CLEAN,
    SAFE,
    Kit,
    add_clip,
    add_creator,
    black_video,
    clip,
    clip_events,
    distinct,
    good_video,
    tables,
)
from psycopg.types.json import Jsonb

from sixstars import db, jobs, settings
from sixstars.clients.anthropic_client import AnthropicError
from sixstars.factory import tools
from sixstars.gate import gate

pytestmark = [pytest.mark.ffmpeg, pytest.mark.usefixtures("tables", "visual_score_on")]
__all__ = ["black_video", "good_video", "tables"]  # fixtures

WRITER_PARTS = {"hook": 6, "clarity": 6, "payoff": 6, "rewatch": 5}
P9V = "p9_visual_score"


def seen(score: int, **parts: int) -> dict[str, Any]:
    return {
        "seen": "He jumps off the crane with no parachute and the mission fails.",
        "score_parts": {"hook": 7, "clarity": 8, "payoff": 7, "rewatch": 6, **parts},
        "score": score,
    }


@pytest.fixture
def visual_score_on() -> None:
    """The seeded production values, with one sample so each test scripts one reply."""
    settings.set("ai.visual_score", True)
    settings.set("ai.visual_score_samples", 1)


def weak_clip(video: Path | None, creator_id: str | None = None, **overrides: Any) -> str:
    """A clip the writer scored 6: below the hourly minimum of 7."""
    values = {"score": 6, "score_parts": Jsonb(WRITER_PARTS), **overrides}
    return add_clip(creator_id or add_creator(), video, **values)


def render_job() -> jobs.Job:
    job_id = jobs.enqueue("normal", "clip.render", {})
    assert job_id is not None
    db.execute("update jobs set status = 'running', attempts = 1 where id = %s", (job_id,))
    job = jobs.get(job_id)
    assert job is not None
    return job


def prompts(kit: Kit) -> list[tuple[str, int]]:
    return [(call["prompt_id"], call["images"]) for call in kit.ai.calls_to("complete_json")]


def test_migration_0016_seeds_the_settings(settings_snapshot: list[tuple[Any, ...]]) -> None:
    seeded = {key: value for key, value, _ in settings_snapshot}  # as the migrations leave the table
    assert seeded["ai.visual_score"] is True
    assert seeded["ai.visual_score_model"] == "writer"
    assert seeded["ai.visual_score_frames"] == 8
    assert seeded["ai.visual_score_samples"] == 3
    # no gate threshold moved
    assert seeded["gate.hourly"]["min_score"] == 7 and seeded["gate.express"]["min_score"] == 8
    migration = Path(__file__).resolve().parents[2] / "supabase/migrations/0016_visual_score.sql"
    assert "('gate." not in migration.read_text("utf-8")


def test_a_clip_the_writer_underrated_is_approved_on_its_visual_score(
    good_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grabbed: list[float] = []
    real_grab = tools.grab_frame

    def counting_grab(source: Path, time_s: float, ffmpeg: str = "ffmpeg") -> bytes:
        grabbed.append(time_s)
        return real_grab(source, time_s, ffmpeg)

    monkeypatch.setattr(tools, "grab_frame", counting_grab)
    kit = Kit(score=seen(8, payoff=8))
    clip_id = weak_clip(good_video)

    outcome = gate.run_gate(clip_id, deps=kit.deps)

    assert outcome.status == "approved"
    row = clip(clip_id)
    assert float(row["score"]) == 8.0 and float(row["rank_score"]) == 8.0
    parts = row["score_parts"]
    assert (parts["hook"], parts["clarity"], parts["payoff"], parts["rewatch"]) == (7, 8, 8, 6)
    assert parts["source"] == "visual" and parts["writer"] == {"score": 6.0, **WRITER_PARTS}
    assert parts["visual"]["score"] == 8 and parts["visual"]["frames"] == 8
    assert parts["visual"]["model"] == settings.get_str("ai.model_writer")
    assert row["gate_result"]["checks"]["score"]["value"] == 8.0
    # one extraction for both calls: eight frames, the score first, then P5 on four of them
    assert len(grabbed) == 8
    assert prompts(kit) == [(P9V, 8), ("p5_visual_check", 4)]
    (scored,) = clip_events(clip_id, "clip.scored")
    assert scored["data"]["score"] == 8 and scored["data"]["score_parts"]["writer"]["score"] == 6.0


def test_a_low_visual_score_rejects_and_p5_is_not_paid_for(good_video: Path) -> None:
    kit = Kit(score=seen(5, payoff=5, clarity=5))
    clip_id = add_clip(add_creator(), good_video, score=9)  # the writer liked it; the frames do not
    assert gate.run_gate(clip_id, deps=kit.deps).status == "rejected"
    row = clip(clip_id)
    assert row["reject_reason"] == "score_too_low" and float(row["score"]) == 5.0
    assert row["score_parts"]["writer"]["score"] == 9.0
    assert prompts(kit) == [(P9V, 8)]


def test_the_rubric_limits_are_enforced_on_the_models_answer(good_video: Path) -> None:
    # an overall 8 with a payoff of 6 is not in the 7-8 band: stored as 6, rejected
    kit = Kit(score=seen(8, payoff=6))
    clip_id = weak_clip(good_video)
    assert gate.run_gate(clip_id, deps=kit.deps).status == "rejected"
    assert float(clip(clip_id)["score"]) == 6.0


@pytest.mark.parametrize("writer_score, status", [(9, "approved"), (6, "rejected")])
def test_a_failed_score_call_falls_back_to_the_writers_score(
    good_video: Path, writer_score: int, status: str
) -> None:
    kit = Kit(score=AnthropicError("overloaded"))
    clip_id = add_clip(add_creator(), good_video, score=writer_score, score_parts=Jsonb(WRITER_PARTS))
    assert gate.run_gate(clip_id, deps=kit.deps).status == status
    row = clip(clip_id)
    assert float(row["score"]) == writer_score and row["score_parts"] == WRITER_PARTS
    (failed,) = clip_events(clip_id, "clip.visual_score_failed")
    assert failed["level"] == "warn"
    if status == "approved":  # P5 still ran, on the frames already extracted
        assert prompts(kit) == [(P9V, 8), ("p5_visual_check", 4)]
        assert row["gate_result"]["inputs"]["visual"] == P5_CLEAN


def test_switched_off_the_writers_score_is_judged_as_before(good_video: Path) -> None:
    settings.set("ai.visual_score", False)
    kit = Kit(score=seen(9, payoff=9, clarity=9))
    clip_id = weak_clip(good_video)
    assert gate.run_gate(clip_id, deps=kit.deps).status == "rejected"
    assert float(clip(clip_id)["score"]) == 6.0 and prompts(kit) == []


def test_a_clip_rejected_for_another_reason_is_not_scored(black_video: Path, good_video: Path) -> None:
    kit = Kit(score=seen(9, payoff=9, clarity=9))
    black = weak_clip(black_video)
    assert gate.run_gate(black, deps=kit.deps).status == "rejected"
    assert clip(black)["reject_reason"] == "black_frames" and prompts(kit) == []
    # a safety flag is judged elsewhere and no score can outweigh it
    flagged = weak_clip(good_video, add_creator(handle="second"), **distinct(2))
    db.execute("update clips set safety_flags = %s where id = %s", (Jsonb({**SAFE, "sexual": True}), flagged))
    assert gate.run_gate(flagged, deps=kit.deps).status == "rejected"
    assert prompts(kit) == [] and float(clip(flagged)["score"]) == 6.0


def test_a_second_gate_run_does_not_score_again(good_video: Path) -> None:
    kit = Kit(p5=AnthropicError("overloaded"), score=seen(8, payoff=8))
    clip_id = weak_clip(good_video)
    with pytest.raises(AnthropicError):
        gate.run_gate(clip_id, deps=kit.deps)  # scored, then P5 failed: still in gating
    assert clip(clip_id)["status"] == "gating" and float(clip(clip_id)["score"]) == 8.0
    again = Kit(score=seen(3, payoff=3, clarity=3))
    assert gate.run_gate(clip_id, deps=again.deps).status == "approved"
    assert prompts(again) == [("p5_visual_check", 4)] and float(clip(clip_id)["score"]) == 8.0


def test_the_scorer_is_told_about_the_chat_spike(good_video: Path) -> None:
    creator_id = add_creator()
    spike = db.fetch_one(
        "insert into spikes (mode, creator_id, spike_offset_s, msgs_in_bucket, unique_chatters, "
        "reaction_count, tier, chat_sample, status) "
        "values ('replay', %s, 1012, 20, 18, 15, 'A', %s, 'clipped') returning id::text as id",
        (creator_id, Jsonb([{"t_rel": 2.0, "text": "NO PARACHUTE LUL", "user": "x"}])),
    )
    assert spike is not None
    kit = Kit(score=seen(7))
    clip_id = add_clip(creator_id, good_video, score=6, spike_id=spike["id"])
    assert gate.run_gate(clip_id, deps=kit.deps).status == "approved"
    user = kit.ai.calls_to("complete_json")[0]["user_content"]
    assert "20 messages from 18 chatters in the peak window" in user
    assert "15 of them (75%) laughter or shock reactions" in user and "spike tier A" in user
    assert "about 12 s into the clip" in user and "14s  NO PARACHUTE LUL" in user
    assert "Hook on screen: He did NOT expect that" in user and "| landed" in user


def test_a_used_up_budget_defers_an_hourly_clip_and_never_an_express_one(good_video: Path) -> None:
    settings.set("ai.daily_budget_usd", 0)
    gate.configure(Kit(score=seen(8, payoff=8)).deps)
    clip_id = weak_clip(good_video)
    gate.after_render(clip_id, render_job())
    assert clip(clip_id)["status"] == "gating" and float(clip(clip_id)["score"]) == 6.0
    assert db.fetch_one("select 1 as ok from jobs where type = 'clip.gate'") is not None

    kit = Kit(score=seen(9, payoff=9, clarity=9))
    kit.deps.hand_off = False
    express = weak_clip(good_video, add_creator(handle="second"), lane="spike", **distinct(2))
    assert gate.run_gate(express, deps=kit.deps).status == "approved"
    assert float(clip(express)["score"]) == 9.0 and prompts(kit) == [(P9V, 8), ("p5_visual_check", 4)]

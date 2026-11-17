"""The visual score at the gate (`gate.rescore`): frame positions, what the scorer is told, the stored
`score_parts`, the setting switch and the fallback to the clip writer's score. No database, no ffmpeg, no
network: the store, the frame grab and Claude are stand-ins."""

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from sixstars import events, settings, usage
from sixstars.ai import calls
from sixstars.ai.schemas import VisualClipScore
from sixstars.clients.anthropic_client import AnthropicError
from sixstars.clients.fakes import FakeAnthropicClient
from sixstars.gate import rescore, store, visual

WRITER_PARTS = {"hook": 6, "clarity": 6, "payoff": 6, "rewatch": 5}
SEEN = "He jumps off the crane with no parachute and the mission fails."
SETTINGS: dict[str, Any] = {
    "ai.model_writer": "claude-sonnet-5-5",
    "ai.model_fast": "claude-haiku-5-5",
    "ai.daily_budget_usd": 25,
    "ai.visual_score": True,
    "ai.visual_score_model": "writer",
    "ai.visual_score_frames": 8,
    "ai.visual_score_samples": 3,
    "env.target_game": "Grand Theft Auto V",
    "factory.watermark": "@6starsclipping",
}
SPIKE = {
    "mode": "replay",
    "spike_offset_s": 1014.0,
    "msgs_in_bucket": 9,
    "unique_chatters": 8,
    "reaction_count": 6,
    "tier": "B",
    "chat_sample": [
        {"t_rel": -51.0, "text": "old news", "user": "a"},
        {"t_rel": -1.0, "text": "LUL LUL", "user": "b"},
        {"t_rel": 4.0, "text": "no parachute OMEGALUL", "user": "c"},
        {"text": "no time", "user": "d"},
    ],
}


def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1080, 1920), (40, 90, 160)).save(out, format="JPEG")
    return out.getvalue()


def visual_reply(score: int = 7, **parts: int) -> dict[str, Any]:
    return {
        "seen": SEEN,
        "score_parts": {"hook": 7, "clarity": 8, "payoff": 7, "rewatch": 6, **parts},
        "score": score,
    }


def clip_row(**raw: Any) -> store.ClipRow:
    values: dict[str, Any] = {
        "score": 6,
        "score_parts": dict(WRITER_PARTS),
        "start_s": 1000.0,
        "hook": "Parachute? Yeah I definitely have one",
        "commentary": None,
        "transcript": {
            "words": [
                {"w": "I", "s": 1.5, "e": 1.7},
                {"w": "have", "s": 1.7, "e": 2.0},
                {"w": "a", "s": 2.0, "e": 2.1},
                {"w": "parachute,", "s": 2.1, "e": 2.8},
                {"w": "right?", "s": 2.8, "e": 3.2},
            ]
        },
        **raw,
    }
    return store.ClipRow(
        id="clip-1",
        creator_id="creator-1",
        spike_id="spike-1",
        status="gating",
        lane="hourly",
        profile="hourly",
        local_path="/data/ready/clip-1.mp4",
        storage_path=None,
        duration_s=20.0,
        gate_result=None,
        precheck_video_id=None,
        raw=values,
    )


class World:
    """The stand-ins of one `rescore.run`: what was stored, which frames were asked for, which events."""

    def __init__(self) -> None:
        self.stored: list[tuple[str, int, dict[str, Any]]] = []
        self.sampled: list[tuple[float, int, float | None]] = []
        self.events: list[dict[str, Any]] = []
        self.grab_error: Exception | None = None
        self.values: dict[str, Any] = dict(SETTINGS)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    state = World()
    values = state.values
    monkeypatch.setattr(settings, "_all", lambda: values)
    monkeypatch.setattr(usage, "spent_today", lambda provider: 0.0)
    monkeypatch.setattr(usage, "record", lambda *args, **kwargs: False)
    monkeypatch.setattr(store, "spike_row", lambda spike_id: dict(SPIKE) if spike_id else None)

    def store_score(clip_id: str, score: int, parts: dict[str, Any]) -> bool:
        state.stored.append((clip_id, score, dict(parts)))
        return True

    def get_clip(clip_id: str) -> store.ClipRow:
        _, score, parts = state.stored[-1]
        return clip_row(score=score, score_parts=parts)

    def sample(
        path: Path, duration_s: float, count: int, peak_s: float | None = None, ffmpeg: str = "ffmpeg"
    ) -> list[tuple[float, bytes]]:
        state.sampled.append((duration_s, count, peak_s))
        if state.grab_error is not None:
            raise state.grab_error
        return [(time_s, jpeg()) for time_s in visual.score_frame_times(duration_s, count, peak_s)]

    def emit(type: str, **fields: Any) -> int:  # noqa: A002
        state.events.append({"type": type, **fields})
        return 1

    monkeypatch.setattr(store, "store_visual_score", store_score)
    monkeypatch.setattr(store, "get_clip", get_clip)
    monkeypatch.setattr(visual, "sample_score_frames", sample)
    monkeypatch.setattr(events, "emit", emit)
    return state


# ── Frame positions ──────────────────────────────────────────


def test_score_frames_always_contain_the_p5_frames() -> None:
    for count in (4, 6, 8):
        for peak in (None, 0.2, 13.6, 39.0, 500.0):
            times = visual.score_frame_times(40.0, count, peak)
            assert set(visual.frame_times(40.0)) <= set(times)
            assert times == sorted(times) and len(times) == len(set(times)) <= count
            assert all(0.0 < time_s < 40.0 for time_s in times)


def test_score_frames_without_a_peak_fill_the_gaps_and_the_end() -> None:
    assert visual.score_frame_times(40.0, 4) == [4.0, 14.0, 24.0, 34.0]
    assert visual.score_frame_times(40.0, 5) == [4.0, 14.0, 24.0, 34.0, 39.0]
    assert visual.score_frame_times(40.0, 8) == [4.0, 9.0, 14.0, 19.0, 24.0, 29.0, 34.0, 39.0]


def test_score_frames_go_to_the_chat_peak_first() -> None:
    # peak at 20 s: 17, 20, 14 (taken: a P5 frame) and 23 s; then the end of the clip
    assert visual.score_frame_times(40.0, 8, 20.0) == [4.0, 14.0, 17.0, 20.0, 23.0, 24.0, 34.0, 39.0]
    # a peak at the very start of the clip: only the frames inside the clip count
    assert visual.score_frame_times(20.0, 6, 0.15)[:2] == [2.0, 3.15]
    # a peak beyond the clip adds nothing
    assert visual.score_frame_times(40.0, 8, 500.0) == visual.score_frame_times(40.0, 8)


def test_p5_frames_are_picked_out_of_the_score_frames() -> None:
    shots = [(time_s, f"{time_s}".encode()) for time_s in visual.score_frame_times(40.0, 8, 20.0)]
    assert visual.p5_frames(shots, 40.0) == [b"4.0", b"14.0", b"24.0", b"34.0"]
    assert rescore.p5_frames(shots, 40.0) == [b"4.0", b"14.0", b"24.0", b"34.0"]
    assert rescore.p5_frames(None, 40.0) is None and rescore.p5_frames(shots[:2], 40.0) is None


# ── What the scorer is told ──────────────────────────────────


def test_the_chat_peak_is_placed_in_clip_time_for_replay_spikes_only() -> None:
    assert rescore.peak_in_clip({"spike_offset_s": 1014.0}, 1000.0, 20.0) == 14.0
    assert rescore.peak_in_clip({"spike_offset_s": None}, 1000.0, 20.0) is None  # a live spike
    assert rescore.peak_in_clip({"spike_offset_s": 5000.0}, 1000.0, 20.0) is None  # nowhere near the clip


def test_chat_reaction_reads_the_spike_row() -> None:
    chat = rescore.chat_reaction(SPIKE, 1000.0, 20.0)
    assert chat is not None
    assert (chat.msgs, chat.uniq, chat.reactions, chat.tier, chat.peak_s) == (9, 8, 6, "B", 14.0)
    assert [(line.t_s, line.text) for line in chat.lines][1:] == [
        (-1.0, "LUL LUL"),
        (4.0, "no parachute OMEGALUL"),
    ]
    assert rescore.chat_reaction(None, 1000.0, 20.0) is None
    empty = rescore.chat_reaction({"chat_sample": "broken"}, 1000.0, 20.0)
    assert empty is not None and empty.lines == [] and empty.msgs is None


def test_rendered_clip_carries_the_hook_and_the_subtitle_lines() -> None:
    clip = rescore.rendered_clip(clip_row(), None)
    assert clip.hook.startswith("Parachute?") and clip.length_s == 20.0 and clip.clip_id == "clip-1"
    assert " ".join(line.text for line in clip.lines) == "I have a parachute, right?"
    assert rescore.rendered_clip(clip_row(transcript=None), None).lines == []


# ── The switch ───────────────────────────────────────────────


def test_wanted_follows_the_setting_and_the_clip(world: World) -> None:
    assert rescore.wanted(clip_row())
    world.values["ai.visual_score"] = False
    assert not rescore.wanted(clip_row())
    del world.values["ai.visual_score"]
    assert not rescore.wanted(clip_row())  # a database without the setting keeps the old path


def test_a_clip_is_scored_once_and_only_with_a_file(world: World) -> None:
    scored = clip_row(score_parts={**WRITER_PARTS, "visual": {"score": 7}})
    assert rescore.already_scored(scored) and not rescore.wanted(scored)
    assert not rescore.already_scored(clip_row(score_parts=None))
    no_file = clip_row()
    assert not rescore.wanted(store.ClipRow(**{**_fields(no_file), "local_path": None}))


def _fields(row: store.ClipRow) -> dict[str, Any]:
    return {name: getattr(row, name) for name in store.ClipRow.__dataclass_fields__}


def test_with_top_score_changes_only_the_score() -> None:
    row = clip_row()
    top = rescore.with_top_score(row)
    assert top.raw["score"] == rescore.TOP_SCORE and row.raw["score"] == 6
    assert {key: value for key, value in top.raw.items() if key != "score"} == {
        key: value for key, value in row.raw.items() if key != "score"
    }


# ── Scoring, storing, falling back ───────────────────────────


def test_run_stores_the_visual_score_and_keeps_the_writers(world: World) -> None:
    fake = FakeAnthropicClient({calls.P9V: [visual_reply(7), visual_reply(8, payoff=8), visual_reply(6)]})
    row, shots = rescore.run(clip_row(), client=fake)

    assert world.sampled == [(20.0, 8, 14.0)]  # frames around the chat peak
    assert shots is not None and len(shots) == 8
    assert len(fake.calls) == 3  # ai.visual_score_samples on the hourly lane
    call = fake.calls[0][1]
    assert call["prompt_id"] == "p9_visual_score" and call["model"] == "claude-sonnet-5-5"
    assert call["images"] == 8 and call["ref_id"] == "clip-1"
    ((clip_id, score, document),) = world.stored
    assert (clip_id, score) == ("clip-1", 7)  # the median
    assert document == {
        "hook": 7,
        "clarity": 8,
        "payoff": 7,
        "rewatch": 6,
        "source": "visual",
        "writer": {"score": 6.0, **WRITER_PARTS},
        "visual": {
            "score": 7,
            "hook": 7,
            "clarity": 8,
            "payoff": 7,
            "rewatch": 6,
            "seen": SEEN,
            "model": "claude-sonnet-5-5",
            "frames": 8,
            "samples": 3,
        },
    }
    # the gate judges the reloaded row, and a second gate run does not score again
    assert row.raw["score"] == 7 and rescore.already_scored(row) and not rescore.wanted(row)
    assert world.events == []


def test_an_express_clip_is_scored_once_in_the_express_budget_lane(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage, "spent_today", lambda provider: 1000.0)  # over the daily budget
    fake = FakeAnthropicClient({calls.P9V: visual_reply(8, payoff=8)})
    express = store.ClipRow(**{**_fields(clip_row()), "lane": "spike", "profile": "express"})
    rescore.run(express, client=fake)
    assert len(fake.calls) == 1 and world.stored[0][1] == 8
    assert world.stored[0][2]["visual"]["samples"] == 1


def test_a_used_up_budget_on_the_hourly_lane_is_raised_so_the_gate_waits(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage, "spent_today", lambda provider: 1000.0)
    fake = FakeAnthropicClient({calls.P9V: visual_reply()})
    with pytest.raises(calls.BudgetExceededError):
        rescore.run(clip_row(), client=fake)
    assert fake.calls == [] and world.stored == []


@pytest.mark.parametrize(
    "failure",
    [AnthropicError("overloaded"), KeyError("no reply"), {"seen": "", "score": 99, "score_parts": {}}],
    ids=["api-error", "unexpected-error", "invalid-reply"],
)
def test_a_failed_score_call_keeps_the_writers_score(world: World, failure: Any) -> None:
    fake = FakeAnthropicClient({calls.P9V: failure})
    before = clip_row()
    row, shots = rescore.run(before, client=fake)
    assert row is before and row.raw["score"] == 6 and world.stored == []
    assert shots is not None and len(shots) == 8  # P5 can still use the frames
    (event,) = world.events
    assert event["type"] == "clip.visual_score_failed" and event["level"] == "warn"
    assert "kept the writer's score" in event["message"]
    assert rescore.wanted(row)  # a later gate run may try again


def test_a_failed_frame_grab_keeps_the_writers_score(world: World) -> None:
    from sixstars.factory.tools import FfmpegError

    world.grab_error = FfmpegError("no frame decoded")
    fake = FakeAnthropicClient({calls.P9V: visual_reply()})
    row, shots = rescore.run(clip_row(), client=fake)
    assert shots is None and row.raw["score"] == 6 and fake.calls == [] and world.stored == []
    assert [event["type"] for event in world.events] == ["clip.visual_score_failed"]


def test_a_missing_or_wrong_setting_keeps_the_writers_score(world: World) -> None:
    world.values["ai.visual_score_model"] = "opus"
    fake = FakeAnthropicClient({calls.P9V: visual_reply()})
    row, _ = rescore.run(clip_row(), client=fake)
    assert row.raw["score"] == 6 and fake.calls == [] and len(world.events) == 1


def test_a_clip_that_left_gating_is_not_reloaded(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "store_visual_score", lambda clip_id, score, parts: False)
    before = clip_row()
    row, _ = rescore.run(before, client=FakeAnthropicClient({calls.P9V: visual_reply()}))
    assert row is before


def test_the_score_document_without_writer_parts() -> None:
    scored = VisualClipScore.model_validate(visual_reply(7))
    document = rescore.score_document(clip_row(score=None, score_parts=None), scored, "m", 6, 1)
    assert document["writer"] == {"score": None} and document["visual"]["frames"] == 6  # type: ignore[index]

"""The eight Claude calls with the fake client, plus malformed model output through the real client
(a scripted stand-in replaces the SDK), which exercises the one repair retry."""

import io
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from ai_samples import LINES, clip_context, spike, verdict_reply, writing_reply
from PIL import Image
from structlog.testing import capture_logs

from sixstars.ai import calls
from sixstars.ai.calls import (
    AudienceEvidence,
    BudgetExceededError,
    ChatLine,
    ChatReaction,
    RenderedClip,
    Segment,
    TranscriptChunk,
)
from sixstars.ai.schemas import ClipScore, ClipWriting, Moment, VisualClipScore
from sixstars.clients.anthropic_client import (
    AnthropicClient,
    AnthropicError,
    AnthropicRefusal,
    InvalidModelOutput,
)
from sixstars.clients.fakes import FakeAnthropicClient

Scripted = Callable[..., tuple[AnthropicClient, Any]]

WRITER = "claude-sonnet-5-5"
FAST = "claude-haiku-5-5"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
VISUAL_OK = {
    "other_watermark": False,
    "platform_logos": False,
    "menu_or_loading": False,
    "pause_or_brb_screen": False,
    "empty_facecam": False,
    "readable_subtitles": True,
    "notes": "",
}


def frame(width: int = 1920, height: int = 1080) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 60)).save(out, format="JPEG")
    return out.getvalue()


def moment(start: float, end: float, confidence: float) -> dict[str, Any]:
    return {"start_s": start, "end_s": end, "moment_type": "funny", "one_line": "x", "confidence": confidence}


def only_call(fake: FakeAnthropicClient) -> dict[str, Any]:
    assert len(fake.calls) == 1
    return dict(fake.calls[0][1])


# ── P1 ───────────────────────────────────────────────────────


def test_confirm_single_spike_uses_the_fast_model() -> None:
    fake = FakeAnthropicClient({calls.P1: verdict_reply()})
    verdicts = calls.confirm_spikes([spike()], lane="express", client=fake)
    assert len(verdicts) == 1 and verdicts[0].is_moment
    call = only_call(fake)
    assert call["model"] == FAST
    assert call["ref_id"] == "sp-1"
    assert "spike_id: sp-1" in call["user_content"]
    assert "NO WAY he landed that" in call["user_content"]
    assert "score_z 4.6, 82 messages, 61 unique chatters" in call["user_content"]


def test_confirm_batches_of_ten_and_keeps_the_input_order() -> None:
    spikes = [spike(f"sp-{index}") for index in range(12)]

    def batch(ids: list[str]) -> dict[str, Any]:
        return {"results": [verdict_reply(spike_id=i, confidence=0.5) for i in reversed(ids)]}

    first = batch([s.spike_id for s in spikes[:10]])
    second = batch([s.spike_id for s in spikes[10:]])
    fake = FakeAnthropicClient({calls.P1: [first, second]})
    verdicts = calls.confirm_spikes(spikes, client=fake)
    assert [getattr(v, "spike_id", None) for v in verdicts] == [s.spike_id for s in spikes]
    assert len(fake.calls) == 2
    assert fake.calls[0][1]["user_content"].startswith("Spikes to judge: 10")
    assert fake.calls[1][1]["user_content"].startswith("Spikes to judge: 2")


def test_confirm_nothing_makes_no_call_and_duplicate_ids_are_refused() -> None:
    fake = FakeAnthropicClient()
    assert calls.confirm_spikes([], client=fake) == []
    assert fake.calls == []
    with pytest.raises(ValueError, match="unique"):
        calls.confirm_spikes([spike("a"), spike("a")], client=fake)


def test_confirm_batch_with_a_missing_spike_is_repaired(scripted: Scripted) -> None:
    incomplete = json.dumps({"results": [verdict_reply(spike_id="a")]})
    complete = json.dumps({"results": [verdict_reply(spike_id="a"), verdict_reply(spike_id="b")]})
    client, sdk = scripted(incomplete, complete)
    verdicts = calls.confirm_spikes([spike("a"), spike("b")], client=client)
    assert len(verdicts) == 2
    assert len(sdk.requests) == 2
    assert "spike_id" in sdk.requests[1]["messages"][0]["content"][-1]["text"]


# ── P2 ───────────────────────────────────────────────────────


def test_write_clip_sends_the_context_and_returns_the_writing() -> None:
    fake = FakeAnthropicClient({calls.P2: writing_reply()})
    writing = calls.write_clip(clip_context(now=NOW), client=fake)
    assert writing.hook.startswith("He did NOT") and not writing.reject
    call = only_call(fake)
    assert call["model"] == WRITER and call["ref_id"] == "clip-1"
    user = call["user_content"]
    assert "[2] 9.50-11.00 | no way no way" in user
    assert "spike_floor_s (latest allowed start): 9.00" in user
    assert "Clip length must be between 20 and 60 seconds." in user
    assert "Current date (UTC): 2026-11-20 12:00" in user
    assert "Story spoilers right now: BLOCKED until 2026-12-03 00:00 UTC (reject a spoiler clip)" in user
    assert "real-world politics, gambling, sponsored segment" in user
    assert "{" not in call["system"]


def test_write_clip_reads_policy_from_settings(ai_settings: dict[str, Any]) -> None:
    ai_settings["gate.spoiler_block_until"] = None
    ai_settings["gate.reject_topics"] = ["gambling"]
    ai_settings["ai.model_writer"] = "claude-opus-5-5"
    fake = FakeAnthropicClient({calls.P2: writing_reply(spoiler=True)})
    writing = calls.write_clip(clip_context(now=NOW), client=fake)
    call = only_call(fake)
    assert call["model"] == "claude-opus-5-5"
    assert "Story spoilers right now: allowed (do not reject" in call["user_content"]
    assert "always rejects: gambling\n" in call["user_content"]
    assert not writing.reject  # no block date: a spoiler clip is allowed


def test_write_clip_rejects_a_spoiler_before_the_block_date() -> None:
    fake = FakeAnthropicClient({calls.P2: writing_reply(spoiler=True)})
    writing = calls.write_clip(clip_context(now=NOW), client=fake)
    assert writing.reject and writing.reject_reason == "story spoiler before the block date"
    after = datetime(2026, 12, 4, tzinfo=UTC)
    assert not calls.write_clip(clip_context(now=after), client=fake).reject


def test_write_clip_rejects_when_a_safety_flag_is_set() -> None:
    reply = writing_reply()
    reply["safety"] = {**reply["safety"], "gambling": True}
    fake = FakeAnthropicClient({calls.P2: reply})
    writing = calls.write_clip(clip_context(now=NOW), client=fake)
    assert writing.reject and writing.reject_reason == "safety: gambling"


def test_write_clip_keeps_the_models_own_reject_reason() -> None:
    fake = FakeAnthropicClient(
        {calls.P2: writing_reply(reject=True, reject_reason="sponsored read", score=2)}
    )
    assert calls.write_clip(clip_context(now=NOW), client=fake).reject_reason == "sponsored read"


def test_write_clip_invalid_then_valid_is_repaired(scripted: Scripted) -> None:
    long_hook = json.dumps(writing_reply(hook="this hook is far too long to fit on the screen at all"))
    client, sdk = scripted(long_hook, "```json\n" + json.dumps(writing_reply()) + "\n```")
    writing = calls.write_clip(clip_context(now=NOW), client=client)
    assert writing.hook == "He did NOT stick this landing 😭"
    assert len(sdk.requests) == 2
    repair = sdk.requests[1]["messages"][0]["content"][-1]["text"]
    assert "hook has 13 words (this | hook | is | far" in repair and "the maximum is 8" in repair
    assert "at least 5 fewer" in repair
    assert sdk.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sdk.requests[0]["system"] == sdk.requests[1]["system"]


def test_write_clip_trim_outside_the_rules_is_sent_back(scripted: Scripted) -> None:
    late = json.dumps(writing_reply(start_s=15.0, end_s=40.0))
    client, sdk = scripted(late, json.dumps(writing_reply()))
    assert calls.write_clip(clip_context(now=NOW), client=client).start_s == 2.0
    assert "spike_floor_s" in sdk.requests[1]["messages"][0]["content"][-1]["text"]


@pytest.mark.parametrize("bad", ["not json at all", '{"hook": "missing everything else"}'])
def test_write_clip_still_invalid_after_the_repairs_fails(scripted: Scripted, bad: str) -> None:
    client, sdk = scripted(bad, bad)
    with pytest.raises(InvalidModelOutput, match="p2_clip_writer: reply is not valid for ClipWriting"):
        calls.write_clip(clip_context(now=NOW), client=client)
    assert len(sdk.requests) == 3


def test_write_clip_request_carries_the_schema_as_output_format(scripted: Scripted) -> None:
    client, sdk = scripted(json.dumps(writing_reply()))
    calls.write_clip(clip_context(now=NOW), client=client)
    request = sdk.requests[0]
    schema = request["output_config"]["format"]["schema"]
    assert request["output_config"]["effort"] == "medium"
    assert schema["title"] == "ClipWriting" and schema["additionalProperties"] is False
    # The parts are written before the overall score, so the score follows from them.
    names = list(schema["properties"])
    assert names.index("score_parts") < names.index("score")
    assert "JSON Schema" not in request["system"][0]["text"]


def test_write_clip_needs_lines_and_passes_a_refusal_on() -> None:
    with pytest.raises(ValueError, match="subtitle line"):
        calls.write_clip(clip_context(lines=[]), client=FakeAnthropicClient())
    fake = FakeAnthropicClient({calls.P2: AnthropicRefusal("general_harms")})
    with pytest.raises(AnthropicRefusal):
        calls.write_clip(clip_context(now=NOW), client=fake)


def test_write_clip_includes_the_spike_verdict() -> None:
    from sixstars.ai.schemas import SpikeVerdict

    fake = FakeAnthropicClient({calls.P2: writing_reply()})
    verdict = SpikeVerdict.model_validate(verdict_reply())
    calls.write_clip(clip_context(now=NOW, verdict=verdict), client=fake)
    assert (
        "thinks happened: funny: Streamer jumps a bike onto a police car" in only_call(fake)["user_content"]
    )


# ── Budget guard ─────────────────────────────────────────────


def test_vod_calls_stop_over_budget_and_express_calls_continue(ai_settings: dict[str, Any]) -> None:
    ai_settings["_spent"] = 25.0
    fake = FakeAnthropicClient(
        {
            calls.P1: verdict_reply(),
            calls.P2: writing_reply(),
            calls.P7: {"is_takedown": False, "kind": "not_takedown"},
            calls.P6: {"tier": "auto", "evidence": None},
        }
    )
    with pytest.raises(BudgetExceededError):
        calls.confirm_spikes([spike()], client=fake)
    with pytest.raises(BudgetExceededError):
        calls.write_clip(clip_context(now=NOW), client=fake)
    with pytest.raises(BudgetExceededError):
        calls.pick_moments([TranscriptChunk(0, 30, "hello")], 3, 8, 20, 60, client=fake)
    with pytest.raises(BudgetExceededError):
        calls.detect_layout(frame(), 1920, 1080, client=fake)
    with pytest.raises(BudgetExceededError):
        calls.visual_check([frame()], client=fake)
    with pytest.raises(BudgetExceededError):
        calls.translate_segments([Segment(0, 1, "hola")], "es", client=fake)
    assert fake.calls == []

    assert calls.confirm_spikes([spike()], lane="express", client=fake)[0].is_moment
    assert calls.write_clip(clip_context(now=NOW), lane="express", client=fake).score == 8
    assert not calls.classify_takedown("a@b.test", "hi", "hello", client=fake).is_takedown
    # P6 is never budget-blocked (AI cost plan A7): a creator without a check cannot be clipped
    assert calls.read_permission("desc", "name", [], client=fake).tier == "auto"


def test_under_budget_vod_calls_run(ai_settings: dict[str, Any]) -> None:
    ai_settings["_spent"] = 24.99
    fake = FakeAnthropicClient({calls.P1: verdict_reply()})
    assert len(calls.confirm_spikes([spike()], client=fake)) == 1


# ── P3 ───────────────────────────────────────────────────────


def test_pick_moments_single_slice() -> None:
    fake = FakeAnthropicClient({calls.P3: {"moments": [moment(40, 70, 0.6), moment(300, 340, 0.9)]}})
    chunks = [TranscriptChunk(index * 30.0, index * 30.0 + 30.0, f"chunk {index}") for index in range(20)]
    moments = calls.pick_moments(chunks, 3, 8, 20, 60, title="Funny stream", creator="speedy", client=fake)
    assert [m.start_s for m in moments] == [300, 40]
    call = only_call(fake)
    assert call["model"] == WRITER
    assert "Pick between 3 and 8 moments, each between 20 and 60 seconds long." in call["user_content"]
    assert "[30-60] chunk 1" in call["user_content"]
    assert "covers 0 s to 600 s" in call["user_content"]


def test_pick_moments_slices_long_videos_and_merges() -> None:
    chunks = [TranscriptChunk(index * 30.0, index * 30.0 + 30.0, "talk") for index in range(130)]  # 65 min
    replies = [
        {"moments": [moment(100, 140, 0.5), moment(120, 160, 0.9), moment(900, 930, 0.7)]},
        {"moments": [moment(2000, 2040, 0.8)]},
        {"moments": []},
    ]
    fake = FakeAnthropicClient({calls.P3: replies})
    moments = calls.pick_moments(chunks, 3, 3, 20, 60, client=fake)
    assert len(fake.calls) == 3
    assert "Pick between 0 and 3" in fake.calls[0][1]["user_content"]
    assert "covers 1800 s to 3600 s" in fake.calls[1][1]["user_content"]
    assert [(m.start_s, m.confidence) for m in moments] == [(120, 0.9), (2000, 0.8), (900, 0.7)]


def test_pick_moments_keeps_the_other_slices_when_one_reply_is_unusable() -> None:
    chunks = [TranscriptChunk(index * 30.0, index * 30.0 + 30.0, "talk") for index in range(130)]  # 65 min
    replies = [
        {"moments": [moment(100, 140, 0.5)]},
        InvalidModelOutput("p3_vod_moment_picker: reply is not valid for MomentList: bad"),
        {"moments": [moment(3700, 3740, 0.8)]},
    ]
    fake = FakeAnthropicClient({calls.P3: replies})
    with capture_logs() as logs:
        moments = calls.pick_moments(chunks, 3, 3, 20, 60, ref_id="video-1", client=fake)
    assert [m.start_s for m in moments] == [3700, 100]
    assert len(fake.calls) == 3
    event = next(item for item in logs if item["event"] == "moment_slice_failed")
    assert (event["slice_start_s"], event["slice_end_s"], event["ref_id"]) == (1800, 3600, "video-1")
    assert "not valid for MomentList" in event["error"]


def test_pick_moments_raises_when_every_slice_fails() -> None:
    chunks = [TranscriptChunk(index * 30.0, index * 30.0 + 30.0, "talk") for index in range(130)]
    fake = FakeAnthropicClient({calls.P3: InvalidModelOutput("bad reply")})
    with pytest.raises(InvalidModelOutput, match="bad reply"):
        calls.pick_moments(chunks, 3, 3, 20, 60, client=fake)
    assert len(fake.calls) == 3


def test_pick_moments_does_not_hide_an_api_failure() -> None:
    chunks = [TranscriptChunk(index * 30.0, index * 30.0 + 30.0, "talk") for index in range(130)]
    fake = FakeAnthropicClient(
        {calls.P3: [{"moments": []}, AnthropicError("Anthropic API returned HTTP 529")]}
    )
    with pytest.raises(AnthropicError, match="529"):
        calls.pick_moments(chunks, 3, 3, 20, 60, client=fake)


def test_pick_moments_without_text_makes_no_call() -> None:
    fake = FakeAnthropicClient()
    assert calls.pick_moments([TranscriptChunk(0, 30, "  ")], 3, 8, 20, 60, client=fake) == []
    assert fake.calls == []


def test_pick_moments_out_of_range_moment_is_sent_back(scripted: Scripted) -> None:
    client, sdk = scripted(
        json.dumps({"moments": [moment(10, 15, 0.9)]}), json.dumps({"moments": [moment(10, 40, 0.9)]})
    )
    moments = calls.pick_moments([TranscriptChunk(0, 60, "wow no way")], 1, 3, 20, 60, client=client)
    assert moments[0].end_s == 40
    assert len(sdk.requests) == 2


def test_merge_moments_drops_overlaps_and_caps() -> None:
    items = [
        Moment.model_validate(moment(s, e, c)) for s, e, c in [(0, 30, 0.4), (20, 50, 0.8), (60, 90, 0.6)]
    ]
    assert [m.start_s for m in calls.merge_moments(items, 5)] == [20, 60]
    assert [m.start_s for m in calls.merge_moments(items, 1)] == [20]


# ── P4 ───────────────────────────────────────────────────────


def test_detect_layout_scales_boxes_back_to_the_original_frame() -> None:
    reply = {
        "facecam": {"x": 960, "y": 500, "w": 320, "h": 220},
        "gameplay": {"x": 0, "y": 0, "w": 1280, "h": 720},
        "confidence": 0.9,
    }
    fake = FakeAnthropicClient({calls.P4: reply})
    layout = calls.detect_layout(frame(1920, 1080), 1920, 1080, ref_id="creator-1", client=fake)
    assert layout.facecam is not None
    assert (layout.facecam.x, layout.facecam.y, layout.facecam.w, layout.facecam.h) == (1440, 750, 480, 330)
    assert (layout.gameplay.w, layout.gameplay.h) == (1920, 1080)
    call = only_call(fake)
    assert call["model"] == WRITER and call["images"] == 1 and call["ref_id"] == "creator-1"
    assert "The image is 1280 x 720 pixels" in call["user_content"]


def test_detect_layout_without_a_facecam() -> None:
    reply = {"facecam": None, "gameplay": {"x": 0, "y": 0, "w": 1280, "h": 720}, "confidence": 0.8}
    layout = calls.detect_layout(frame(1280, 720), 1280, 720, client=FakeAnthropicClient({calls.P4: reply}))
    assert layout.facecam is None


def test_detect_layout_box_outside_the_image_is_sent_back(scripted: Scripted) -> None:
    outside = {
        "facecam": {"x": 1100, "y": 500, "w": 320, "h": 220},
        "gameplay": {"x": 0, "y": 0, "w": 1280, "h": 720},
    }
    inside = {**outside, "facecam": {"x": 960, "y": 500, "w": 320, "h": 220}}
    client, sdk = scripted(
        json.dumps({**outside, "confidence": 0.9}), json.dumps({**inside, "confidence": 0.9})
    )
    layout = calls.detect_layout(frame(1280, 720), 1280, 720, client=client)
    assert layout.facecam is not None and layout.facecam.x == 960
    content = sdk.requests[0]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert "facecam box is outside" in sdk.requests[1]["messages"][0]["content"][-1]["text"]


def test_detect_layout_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="positive"):
        calls.detect_layout(frame(), 0, 1080, client=FakeAnthropicClient())
    with pytest.raises(ValueError, match="readable image"):
        calls.detect_layout(b"junk", 1920, 1080, client=FakeAnthropicClient())


# ── P5 ───────────────────────────────────────────────────────


def test_visual_check_sends_all_frames_to_the_fast_model() -> None:
    fake = FakeAnthropicClient({calls.P5: {**VISUAL_OK, "platform_logos": True, "notes": "TikTok logo"}})
    verdict = calls.visual_check([frame(1080, 1920)] * 4, lane="express", ref_id="clip-9", client=fake)
    assert verdict.problems() == ["platform_logos"]
    call = only_call(fake)
    assert call["model"] == FAST and call["images"] == 4
    assert "@6starsclipping" in call["system"]
    assert call["user_content"] == "These are 4 frames from one rendered clip, in time order."


def test_visual_check_frame_count_limits() -> None:
    fake = FakeAnthropicClient({calls.P5: VISUAL_OK})
    with pytest.raises(ValueError, match="at least one"):
        calls.visual_check([], client=fake)
    with pytest.raises(ValueError, match="at most"):
        calls.visual_check([frame(64, 64)] * 9, client=fake)


# ── P6, P7, P8 ───────────────────────────────────────────────


def test_read_permission() -> None:
    fake = FakeAnthropicClient({calls.P6: {"tier": "preferred", "evidence": "clippers welcome"}})
    verdict = calls.read_permission("Clippers welcome! DM me.", "Speedy", ["GTA RP day 4", " "], client=fake)
    assert (verdict.tier, verdict.evidence) == ("preferred", "clippers welcome")
    user = only_call(fake)["user_content"]
    assert "Channel name: Speedy" in user and "- GTA RP day 4" in user and "Clippers welcome! DM me." in user


def test_read_permission_bad_tier_twice_fails(scripted: Scripted) -> None:
    client, _ = scripted('{"tier": "maybe", "evidence": null}', '{"tier": "maybe", "evidence": null}')
    with pytest.raises(InvalidModelOutput):
        calls.read_permission("", "Speedy", [], client=client)


def test_classify_takedown_truncates_the_body() -> None:
    reply = {
        "is_takedown": True,
        "kind": "copyright",
        "creator_handles": ["@speedy"],
        "urls": ["https://www.tiktok.com/@6starsclipping/video/1"],
        "platforms": ["tiktok"],
        "summary": "Speedy asks to remove one clip.",
    }
    fake = FakeAnthropicClient({calls.P7: reply})
    verdict = calls.classify_takedown("speedy@mail.test", "Remove my clip", "x" * 5000, client=fake)
    assert verdict.is_takedown and verdict.creator_handles == ["speedy"]
    user = only_call(fake)["user_content"]
    assert "x" * 4000 in user and "x" * 4001 not in user
    assert only_call(fake)["model"] == FAST


def test_translate_segments_keeps_source_times() -> None:
    reply = {
        "segments": [{"start": 0.0, "end": 9.9, "en": "no way bro"}, {"start": 5, "end": 6, "en": "let's go"}]
    }
    fake = FakeAnthropicClient({calls.P8: reply})
    out = calls.translate_segments(
        [Segment(1.0, 2.5, "ni de coña tío"), Segment(3.0, 4.0, "vamos")], "es", client=fake
    )
    assert [(s.start, s.end, s.en) for s in out] == [(1.0, 2.5, "no way bro"), (3.0, 4.0, "let's go")]
    call = only_call(fake)
    assert call["model"] == WRITER
    assert "Source language: es" in call["user_content"]
    assert "1.00-2.50 | ni de coña tío" in call["user_content"]


def test_translate_segments_wrong_count_is_sent_back_and_empty_input_is_free(scripted: Scripted) -> None:
    one = json.dumps({"segments": [{"start": 1.0, "end": 2.5, "en": "no way"}]})
    two = json.dumps(
        {"segments": [{"start": 1.0, "end": 2.5, "en": "no way"}, {"start": 3, "end": 4, "en": "go"}]}
    )
    client, sdk = scripted(one, two)
    out = calls.translate_segments([Segment(1.0, 2.5, "a"), Segment(3.0, 4.0, "b")], "es", client=client)
    assert len(out) == 2 and len(sdk.requests) == 2
    assert calls.translate_segments([], "es", client=client) == []


def test_translate_segments_batches() -> None:
    def reply(count: int) -> dict[str, Any]:
        return {"segments": [{"start": 0, "end": 1, "en": "x"}] * count}

    fake = FakeAnthropicClient({calls.P8: [reply(60), reply(5)]})
    segments = [Segment(float(index), index + 0.5, "hola") for index in range(65)]
    assert len(calls.translate_segments(segments, "es", client=fake)) == 65
    assert len(fake.calls) == 2


def test_every_prompt_has_limits_and_effort() -> None:
    from sixstars.ai import prompt_files

    assert set(calls.MAX_TOKENS) == set(prompt_files.PROMPT_IDS) == set(calls.EFFORT)


def test_spoiler_status_compares_the_dates_in_code() -> None:
    block = datetime(2026, 12, 3, tzinfo=UTC)
    assert calls.spoiler_status(NOW, block).startswith("BLOCKED until 2026-12-03 00:00 UTC")
    assert calls.spoiler_status(block, block).startswith("allowed")
    assert calls.spoiler_status(NOW, None).startswith("allowed")


# ── P9 ───────────────────────────────────────────────────────


def scored(score: int, **parts: int) -> dict[str, Any]:
    return {"score": score, "score_parts": {"hook": 6, "clarity": 6, "payoff": 6, "rewatch": 6, **parts}}


def test_score_clip_asks_the_fast_model_and_takes_the_median() -> None:
    replies = [scored(7, payoff=7), scored(5, payoff=5), scored(6, payoff=8)]
    fake = FakeAnthropicClient({calls.P9: replies})
    writing = ClipWriting.model_validate(writing_reply(start_s=3.4, end_s=32.0))
    result = calls.score_clip(clip_context(), writing, 3, lane="express", client=fake)
    assert result.score == 6 and result.score_parts.payoff == 7  # each part and the score separately
    assert len(fake.calls) == 3
    call = fake.calls[0][1]
    assert call["model"] == FAST and call["ref_id"] == "clip-1"
    user = call["user_content"]
    assert "Hook on screen: He did NOT stick this landing" in user and "Clip length: 29 s" in user
    # Only the lines of the cut, in clip time: the line before the cut is not shown.
    assert "0.00-1.60 | I'm gonna jump it" in user and "okay watch this" not in user
    assert "First `score_parts`" in call["system"]


def test_median_score_of_an_even_number_is_the_lower_middle() -> None:
    items = [ClipScore.model_validate(scored(value)) for value in (7, 6, 8, 5)]
    assert calls.median_score(items).score == 6
    with pytest.raises(ValueError):
        calls.median_score([])


def test_write_clip_keeps_its_own_score_unless_score_samples_is_set(ai_settings: dict[str, Any]) -> None:
    fake = FakeAnthropicClient({calls.P2: writing_reply(), calls.P9: scored(6)})
    assert calls.write_clip(clip_context(now=NOW), client=fake).score == 8
    assert [call[1]["prompt_id"] for call in fake.calls] == [calls.P2]

    ai_settings["ai.score_samples"] = 3
    fake = FakeAnthropicClient(
        {calls.P2: writing_reply(), calls.P9: [scored(6), scored(7), scored(6, hook=4)]}
    )
    writing = calls.write_clip(clip_context(now=NOW), client=fake)
    assert writing.score == 6 and writing.score_parts.hook == 6
    assert writing.hook == "He did NOT stick this landing 😭"  # only the score changes
    assert [call[1]["prompt_id"] for call in fake.calls] == [calls.P2, calls.P9, calls.P9, calls.P9]


def test_write_clip_does_not_rescore_a_rejected_clip(ai_settings: dict[str, Any]) -> None:
    ai_settings["ai.score_samples"] = 3
    fake = FakeAnthropicClient({calls.P2: writing_reply(reject=True, reject_reason="no moment", score=3)})
    assert calls.write_clip(clip_context(now=NOW), client=fake).score == 3
    assert len(fake.calls) == 1


def test_write_clip_keeps_the_writers_score_when_the_scorer_fails(ai_settings: dict[str, Any]) -> None:
    ai_settings["ai.score_samples"] = 3
    failing = [scored(6), InvalidModelOutput("p9_clip_score: reply is not valid for ClipScore: bad")]
    fake = FakeAnthropicClient({calls.P2: writing_reply(), calls.P9: failing})
    with capture_logs() as logs:
        assert calls.write_clip(clip_context(now=NOW), client=fake).score == 8
    assert any(item["event"] == "clip_score_failed" for item in logs)
    # A cut with no subtitle line inside it cannot be scored either: the writer's score stays.
    fake = FakeAnthropicClient({calls.P2: writing_reply(start_s=8.9, end_s=29.0), calls.P9: scored(6)})
    lines = [line for line in clip_context().lines if line.index in (0, 4)]
    assert calls.write_clip(clip_context(now=NOW, lines=lines), client=fake).score == 8


# ── P9V ──────────────────────────────────────────────────────


def seen_score(score: int, **parts: int) -> dict[str, Any]:
    return {"seen": f"A fall, scored {score}.", **scored(score, **{"clarity": 7, "payoff": 7, **parts})}


def rendered(**overrides: Any) -> RenderedClip:
    chat = ChatReaction(
        msgs=20,
        uniq=18,
        reactions=15,
        tier="A",
        peak_s=12.0,
        lines=(ChatLine(-40.0, "old message"), ChatLine(-1.0, "LUL"), ChatLine(3.0, "no  parachute\nKEKW")),
    )
    values: dict[str, Any] = {
        "hook": "Parachute? Sure 😭",
        "length_s": 20.7,
        "lines": LINES[:2],
        "chat": chat,
        "clip_id": "clip-7",
    }
    return RenderedClip(**{**values, **overrides})


def test_score_clip_visual_sends_frames_lines_and_chat(ai_settings: dict[str, Any]) -> None:
    fake = FakeAnthropicClient({calls.P9V: seen_score(8, payoff=8)})
    frames = [frame(1080, 1920)] * 6
    times = [2.0, 7.0, 9.0, 12.0, 15.0, 17.5]
    result = calls.score_clip_visual(rendered(), frames, times, client=fake)

    assert isinstance(result, VisualClipScore) and result.score == 8 and result.seen.startswith("A fall")
    call = only_call(fake)
    assert call["prompt_id"] == "p9_visual_score" and call["ref_id"] == "clip-7"
    assert call["model"] == WRITER and call["images"] == 6 and call["schema"] is VisualClipScore
    user = call["user_content"]
    assert "Clip length: 21 s" in user and "Hook on screen: Parachute? Sure 😭" in user
    assert "6 frames from the rendered clip" in user and "2.0, 7.0, 9.0, 12.0, 15.0, 17.5" in user
    assert "2.00-3.10 | okay watch this" in user and "Commentary line on screen: (none)" in user
    assert "20 messages from 18 chatters in the peak window" in user
    assert "15 of them (75%) laughter or shock reactions" in user and "spike tier A" in user
    # chat near the peak only, in clip time, one message per line
    assert "11s  LUL" in user and "15s  no parachute KEKW" in user and "old message" not in user
    # the system prompt: the shared rubric, our overlay named, the transcript-only bias gone
    system = call["system"]
    assert "First `score_parts`" in system and "@6starsclipping" in system
    for ours in (
        "the hook:",
        "the subtitles:",
        "watermark",
        "creator credit",
        "progress bar",
        '"LIVE" badge',
    ):
        assert ours in system
    assert "from the frames and the chat reaction as well as the words" in system
    assert "give the lower one" not in system and "do not assume a visual payoff" not in system

    ai_settings["ai.visual_score_model"] = "fast"
    fast = FakeAnthropicClient({calls.P9V: seen_score(7)})
    calls.score_clip_visual(rendered(), frames, times, client=fast)
    assert only_call(fast)["model"] == FAST


def test_score_clip_visual_shrinks_the_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    real = calls.prepare_jpeg

    def spy(data: bytes, max_side: int = 1280, quality: int = 85) -> Any:
        prepared = real(data, max_side=max_side, quality=quality)
        sizes.append(max(prepared.width, prepared.height))
        return prepared

    monkeypatch.setattr(calls, "prepare_jpeg", spy)
    fake = FakeAnthropicClient({calls.P9V: seen_score(7)})
    calls.score_clip_visual(rendered(), [frame(1080, 1920)] * 2, [1.0, 2.0], client=fake)
    assert sizes == [768, 768]


def test_score_clip_visual_without_chat_or_speech() -> None:
    fake = FakeAnthropicClient({calls.P9V: seen_score(7)})
    calls.score_clip_visual(rendered(chat=None, lines=[]), [frame(64, 64)], [1.0], client=fake)
    user = only_call(fake)["user_content"]
    assert "(no chat data for this clip)" in user and "(nobody speaks)" in user
    # a live spike has no position in the clip: the messages are shown relative to the peak
    live = ChatReaction(msgs=5, lines=(ChatLine(2.0, "LMAO"),))
    assert "+2s  LMAO" in calls._reaction_block(live)
    assert calls.chat_summary(live) == "5 messages in the peak window."
    assert calls.chat_summary(ChatReaction()) == "(no chat counts for this clip)"


def test_score_clip_visual_takes_the_median_of_several_asks() -> None:
    replies = [seen_score(8, payoff=8), seen_score(6, payoff=6), seen_score(7)]
    fake = FakeAnthropicClient({calls.P9V: replies})
    result = calls.score_clip_visual(rendered(), [frame(64, 64)], [1.0], samples=3, client=fake)
    assert len(fake.calls) == 3
    assert result.score == 7 and result.score_parts.payoff == 7 and result.seen == "A fall, scored 7."


@pytest.mark.parametrize(
    ("score", "payoff", "clarity", "expected"),
    [
        (8, 8, 8, 8),
        (7, 7, 7, 7),
        (8, 6, 9, 6),  # not in the 7-8 band without a payoff of 7
        (7, 9, 6, 6),  # nor without a clarity of 7
        (9, 9, 7, 8),  # the 9-10 band needs both at 8
        (10, 8, 8, 9),  # never more than one above payoff
        (6, 3, 9, 4),
        (6, 9, 2, 4),  # never more than two above clarity
        (5, 5, 5, 5),
        (1, 1, 1, 1),
    ],
)
def test_bounded_score_enforces_the_rubric(score: int, payoff: int, clarity: int, expected: int) -> None:
    item = ClipScore.model_validate(scored(score, payoff=payoff, clarity=clarity))
    assert calls.bounded_score(item) == expected <= score


def test_the_models_score_is_bounded_before_it_is_returned() -> None:
    fake = FakeAnthropicClient({calls.P9V: seen_score(9, payoff=6)})
    assert calls.score_clip_visual(rendered(), [frame(64, 64)], [1.0], client=fake).score == 6


def test_score_clip_visual_refuses_bad_input_and_respects_the_budget(ai_settings: dict[str, Any]) -> None:
    fake = FakeAnthropicClient({calls.P9V: seen_score(7)})
    with pytest.raises(ValueError, match="at least one frame"):
        calls.score_clip_visual(rendered(), [], [], client=fake)
    with pytest.raises(ValueError, match="at most"):
        calls.score_clip_visual(rendered(), [frame(64, 64)] * 9, [1.0] * 9, client=fake)
    with pytest.raises(ValueError, match="one time"):
        calls.score_clip_visual(rendered(), [frame(64, 64)] * 2, [1.0], client=fake)
    with pytest.raises(ValueError, match="samples"):
        calls.score_clip_visual(rendered(), [frame(64, 64)], [1.0], samples=0, client=fake)
    ai_settings["ai.visual_score_model"] = "opus"
    with pytest.raises(ValueError, match="'fast' or 'writer'"):
        calls.score_clip_visual(rendered(), [frame(64, 64)], [1.0], client=fake)
    ai_settings["ai.visual_score_model"] = "writer"
    assert fake.calls == []

    ai_settings["_spent"] = 25.0
    with pytest.raises(BudgetExceededError):
        calls.score_clip_visual(rendered(), [frame(64, 64)], [1.0], client=fake)
    calls.score_clip_visual(rendered(), [frame(64, 64)], [1.0], lane="express", client=fake)
    assert len(fake.calls) == 1

    failing = FakeAnthropicClient({calls.P9V: AnthropicError("overloaded")})
    with pytest.raises(AnthropicError):
        calls.score_clip_visual(rendered(), [frame(64, 64)], [1.0], lane="express", client=failing)


def test_score_clip_visual_records_usage_under_its_own_operation(
    scripted: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[Any, ...]] = []
    monkeypatch.setattr(calls.anthropic_client.usage, "record", lambda *a, **k: recorded.append((a, k)))
    client, sdk = scripted(json.dumps(seen_score(7)))
    calls.score_clip_visual(rendered(), [frame(64, 64)] * 2, [1.0, 2.0], client=client)
    ((args, kwargs),) = recorded
    assert args == ("anthropic", "p9_visual_score") and kwargs["model"] == WRITER
    request = sdk.requests[0]
    content = request["messages"][0]["content"]
    assert [block["type"] for block in content] == ["image", "image", "text"]
    assert request["output_config"]["format"]["type"] == "json_schema"


# ── P10 ──────────────────────────────────────────────────────


def test_sentence_ends_numbers_the_words_and_returns_sorted_indexes() -> None:
    fake = FakeAnthropicClient({calls.P10: {"ends": [5, 1, 5]}})
    words = ["no", "way", "he", "did", "that\n now", "look"]
    assert calls.sentence_ends(words, lane="express", ref_id="clip-1", client=fake) == [1, 5]
    call = only_call(fake)
    assert call["model"] == FAST and call["ref_id"] == "clip-1"
    assert "6 words, numbered 0 to 5" in call["user_content"]
    assert "0:no 1:way 2:he 3:did 4:that now 5:look" in call["user_content"]
    assert calls.sentence_ends([], client=fake) == [] and len(fake.calls) == 1


def test_sentence_ends_outside_the_transcript_are_sent_back(scripted: Scripted) -> None:
    client, sdk = scripted(json.dumps({"ends": [1, 9]}), json.dumps({"ends": [1, 2]}))
    assert calls.sentence_ends(["a", "b", "c"], lane="express", client=client) == [1, 2]
    assert "word numbers go from 0 to 2" in sdk.requests[1]["messages"][0]["content"][-1]["text"]


def test_audience_evidence_replaces_the_chat_summary_only_without_a_chat_spike() -> None:
    evidence = AudienceEvidence(views=1749, age_h=14.3, title="he did\nnot have   a parachute " + "x" * 300)
    summary = calls.audience_summary(evidence)
    assert summary.startswith("No chat spike: this clip was not cut around a chat peak")
    assert "A viewer of the stream clipped this moment on Twitch by hand and named the clip" in summary
    assert "he did not have a parachute x" in summary and "x" * 141 not in summary  # one line, cut
    assert "That Twitch clip had 1749 views about 14 hours after it was made." in summary
    assert summary.endswith("not a score: judge what the frames and the lines show.")
    assert "1 hour after" in calls.audience_summary(AudienceEvidence(views=12, age_h=0.2))
    untitled = calls.audience_summary(AudienceEvidence())
    assert "by hand." in untitled and "views" not in untitled

    fake = FakeAnthropicClient({calls.P9V: seen_score(7)})
    calls.score_clip_visual(rendered(chat=None, audience=evidence), [frame(64, 64)], [1.0], client=fake)
    user = only_call(fake)["user_content"]
    assert "No chat spike" in user and "(no chat messages)" in user
    assert "(no chat data for this clip)" not in user
    # a clip cut around a chat spike keeps its chat summary whatever else is passed
    fake = FakeAnthropicClient({calls.P9V: seen_score(7)})
    calls.score_clip_visual(rendered(audience=evidence), [frame(64, 64)], [1.0], client=fake)
    user = only_call(fake)["user_content"]
    assert "20 messages from 18 chatters" in user and "No chat spike" not in user

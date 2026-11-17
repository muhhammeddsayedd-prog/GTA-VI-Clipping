"""Reply schemas: the limits of docs/15 and the per-call bounds."""

from typing import Any

import pytest
from ai_samples import verdict_reply, writing_reply
from pydantic import ValidationError

from sixstars.ai import schemas
from sixstars.ai.schemas import (
    ClipWriting,
    Layout,
    Moment,
    PermissionVerdict,
    PixelBox,
    SpikeVerdict,
    TakedownVerdict,
    VisualVerdict,
    clip_writing_schema,
    count_words,
    layout_schema,
    moment_list_schema,
    spike_batch_schema,
    translated_segments_schema,
)

FULL_FRAME = {"x": 0, "y": 0, "w": 1280, "h": 720}


def test_count_words_ignores_emoji_and_punctuation() -> None:
    assert count_words("He did NOT expect that 😭") == 5
    assert count_words("no - way !!") == 2
    assert count_words("") == 0


@pytest.mark.parametrize("confidence", [-0.1, 1.01])
def test_spike_confidence_must_be_between_0_and_1(confidence: float) -> None:
    with pytest.raises(ValidationError):
        SpikeVerdict.model_validate(verdict_reply(confidence=confidence))


def test_spike_verdict_accepts_the_doc_shape_and_ignores_extra_keys() -> None:
    verdict = SpikeVerdict.model_validate(verdict_reply(extra="ignored"))
    assert verdict.is_moment and verdict.moment_type == "funny"


def test_spike_one_line_is_at_most_15_words() -> None:
    with pytest.raises(ValidationError, match="one_line"):
        SpikeVerdict.model_validate(verdict_reply(one_line=" ".join(["word"] * 16)))


def test_spike_moment_type_must_be_known() -> None:
    with pytest.raises(ValidationError):
        SpikeVerdict.model_validate(verdict_reply(moment_type="epic"))


def test_spike_batch_needs_exactly_the_given_ids() -> None:
    schema = spike_batch_schema(["a", "b"])
    ok = schema.model_validate(
        {"results": [verdict_reply(spike_id="b"), verdict_reply(spike_id="a", is_moment=False)]}
    )
    assert [item.spike_id for item in ok.results] == ["b", "a"]
    with pytest.raises(ValidationError, match="spike_id"):
        schema.model_validate({"results": [verdict_reply(spike_id="a")]})
    with pytest.raises(ValidationError, match="spike_id"):
        schema.model_validate({"results": [verdict_reply(spike_id="a"), verdict_reply(spike_id="zzz")]})


def test_clip_writing_valid_reply() -> None:
    writing = ClipWriting.model_validate(writing_reply())
    assert writing.length_s == 30.0
    assert writing.keyword_map() == {2: ["no way"], 4: ["five", "grand"]}
    assert writing.safety.flagged() == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("hook", "one two three four five six seven eight nine", "hook has 9 words"),
        ("hook", "", "hook"),
        ("title", "x" * 71, "title has 71 characters"),
        ("title", "Best jump ever #gta", "hashtags"),
        ("caption", "y" * 121, "caption has 121 characters"),
        ("hashtags", ["one", "two"], "2 hashtags"),
        ("hashtags", ["a", "b", "c", "d", "e", "f"], "6 hashtags"),
        ("hashtags", ["bike jump", "two", "three"], "one lowercase word"),
        ("hashtags", ["same", "same", "#Same"], "1 hashtags"),
        ("score", 0, "score"),
        ("score", 11, "score"),
        ("score", 7.5, "score"),
        ("commentary", " ".join(["w"] * 13), "commentary has 13 words"),
        ("topic", "!!!", "topic"),
        ("end_s", 2.0, "start_s must be less than end_s"),
        ("score_parts", {"hook": 0, "clarity": 5, "payoff": 5, "rewatch": 5}, "score_parts"),
        ("keywords", [{"line": 0, "words": ["a", "b", "c"]}], "keywords"),
    ],
)
def test_clip_writing_limits(field: str, value: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ClipWriting.model_validate(writing_reply(**{field: value}))


def test_clip_writing_boundary_values_pass() -> None:
    writing = ClipWriting.model_validate(
        writing_reply(
            hook="one two three four five six seven eight 🔥",
            title="x" * 70,
            caption="y" * 120,
            hashtags=["a", "b", "c", "d", "e"],
            score=10,
        )
    )
    assert count_words(writing.hook) == schemas.HOOK_MAX_WORDS


def test_hashtags_are_tidied() -> None:
    writing = ClipWriting.model_validate(
        writing_reply(hashtags=["#BikeJump", "gtarp", " CopChase ", "gtarp"])
    )
    assert writing.hashtags == ["bikejump", "gtarp", "copchase"]


def test_topic_becomes_a_slug_and_empty_commentary_is_none() -> None:
    writing = ClipWriting.model_validate(writing_reply(topic="Police Chase: Boat crash!", commentary="  "))
    assert writing.topic == "police-chase-boat-crash"
    assert writing.commentary is None


def test_safety_flagged_lists_set_flags() -> None:
    reply = writing_reply()
    reply["safety"] = {**reply["safety"], "gambling": True, "sponsored": True}
    assert ClipWriting.model_validate(reply).safety.flagged() == ["gambling", "sponsored"]


def test_bound_clip_writing_checks_the_trim() -> None:
    schema = clip_writing_schema(60.0, 9.0, 20.0, 45.0, [0, 1, 2, 3])
    ok = schema.model_validate(writing_reply())
    assert [item.line for item in ok.keywords] == [2]  # line 4 is not a known line
    with pytest.raises(ValidationError, match="spike_floor_s"):
        schema.model_validate(writing_reply(start_s=12.0, end_s=40.0))
    with pytest.raises(ValidationError, match="window file"):
        schema.model_validate(writing_reply(start_s=8.0, end_s=61.0))
    with pytest.raises(ValidationError, match="clip length"):
        schema.model_validate(writing_reply(start_s=0.0, end_s=50.0))
    with pytest.raises(ValidationError, match="clip length"):
        schema.model_validate(writing_reply(start_s=5.0, end_s=15.0))


def test_bound_schemas_keep_the_json_schema_of_the_base() -> None:
    """The schema text is part of the cached system prompt, so it must not change per call."""
    one = clip_writing_schema(60.0, 9.0, 20.0, 45.0, [0]).model_json_schema()
    two = clip_writing_schema(90.0, 30.0, 15.0, 60.0, [0, 1]).model_json_schema()
    assert one == two
    assert one["title"] == "ClipWriting"
    assert layout_schema(1280, 720).model_json_schema() == layout_schema(640, 360).model_json_schema()


def test_moment_list_bounds() -> None:
    schema = moment_list_schema(2, 20.0, 60.0, 0.0, 1800.0)
    moment = {"start_s": 100.0, "end_s": 130.0, "moment_type": "fail", "one_line": "crash", "confidence": 0.7}
    assert len(schema.model_validate({"moments": [moment]}).moments) == 1
    assert schema.model_validate({"moments": []}).moments == []
    with pytest.raises(ValidationError, match="maximum is 2"):
        schema.model_validate({"moments": [moment, moment, moment]})
    with pytest.raises(ValidationError, match="long"):
        schema.model_validate({"moments": [{**moment, "end_s": 105.0}]})
    with pytest.raises(ValidationError, match="outside the transcript"):
        schema.model_validate({"moments": [{**moment, "start_s": 1790.0, "end_s": 1820.0}]})
    with pytest.raises(ValidationError):
        schema.model_validate({"moments": [{**moment, "confidence": 1.5}]})


def test_moment_overlap() -> None:
    def moment(start: float, end: float) -> Moment:
        return Moment(start_s=start, end_s=end, moment_type="funny", one_line="x", confidence=0.5)

    assert moment(0, 30).overlaps(moment(29, 50))
    assert not moment(0, 30).overlaps(moment(30, 50))


def test_layout_boxes_are_non_negative_and_inside_the_frame() -> None:
    schema = layout_schema(1280, 720)
    cam = {"x": 960, "y": 500, "w": 320, "h": 220}
    ok = schema.model_validate({"facecam": cam, "gameplay": FULL_FRAME, "confidence": 0.9})
    assert ok.facecam == PixelBox(x=960, y=500, w=320, h=220)
    assert schema.model_validate({"facecam": None, "gameplay": FULL_FRAME, "confidence": 0.4}).facecam is None
    with pytest.raises(ValidationError, match="facecam box is outside"):
        schema.model_validate({"facecam": {**cam, "x": 1000}, "gameplay": FULL_FRAME, "confidence": 0.9})
    with pytest.raises(ValidationError):
        Layout.model_validate({"facecam": {**cam, "x": -1}, "gameplay": FULL_FRAME, "confidence": 0.9})
    with pytest.raises(ValidationError):
        Layout.model_validate({"facecam": None, "gameplay": {**FULL_FRAME, "w": 0}, "confidence": 0.9})
    with pytest.raises(ValidationError):
        Layout.model_validate({"facecam": None, "gameplay": {**FULL_FRAME, "x": 0.5}, "confidence": 0.9})


def test_pixel_box_scaling_stays_inside_the_frame() -> None:
    box = PixelBox(x=960, y=540, w=320, h=180)
    assert box.scaled(1.5, 1.5, 1920, 1080) == PixelBox(x=1440, y=810, w=480, h=270)
    edge = PixelBox(x=1270, y=710, w=10, h=10).scaled(1.5, 1.5, 1900, 1070)
    assert edge == PixelBox(x=1899, y=1065, w=1, h=5)


def test_visual_verdict_problems() -> None:
    clean = VisualVerdict(
        other_watermark=False,
        platform_logos=False,
        menu_or_loading=False,
        pause_or_brb_screen=False,
        empty_facecam=False,
        readable_subtitles=True,
    )
    assert clean.problems() == []
    bad = clean.model_copy(update={"platform_logos": True, "readable_subtitles": False})
    assert bad.problems() == ["platform_logos", "unreadable_subtitles"]


def test_permission_verdict() -> None:
    assert PermissionVerdict.model_validate({"tier": "auto", "evidence": ""}).evidence is None
    with pytest.raises(ValidationError):
        PermissionVerdict.model_validate({"tier": "blocked", "evidence": None})
    with pytest.raises(ValidationError, match="evidence has 21 words"):
        PermissionVerdict.model_validate({"tier": "restricted", "evidence": " ".join(["w"] * 21)})


def test_takedown_verdict_is_normalised() -> None:
    verdict = TakedownVerdict.model_validate(
        {
            "is_takedown": True,
            "kind": "not_takedown",
            "creator_handles": ["@Speedy", "Speedy", " "],
            "urls": ["https://x.test/1", "https://x.test/1"],
            "platforms": ["TikTok", "facebook", "youtube"],
            "summary": "Asks to remove a clip.",
        }
    )
    assert verdict.kind == "other"
    assert verdict.creator_handles == ["Speedy"]
    assert verdict.urls == ["https://x.test/1"]
    assert verdict.platforms == ["tiktok", "youtube"]
    spam = TakedownVerdict.model_validate({"is_takedown": False, "kind": "copyright"})
    assert spam.kind == "not_takedown"


def test_translated_segments_count() -> None:
    schema = translated_segments_schema(2)
    segment = {"start": 0.0, "end": 1.0, "en": "no way"}
    assert len(schema.model_validate({"segments": [segment, segment]}).segments) == 2
    with pytest.raises(ValidationError, match="exactly 2"):
        schema.model_validate({"segments": [segment]})

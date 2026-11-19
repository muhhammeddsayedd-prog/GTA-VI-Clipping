"""Story cuts and one-word captions (development-plan/M18 Phase C): the story rules, the writer's story
trim, the wider live cut, the posting rules for a family of clips and the word-highlighted captions. No
network."""

from dataclasses import replace

import pytest

from sixstars.ai.schemas import clip_writing_schema
from sixstars.factory.director import AccountSlot, DirectorRules, clip_facts, plan_styles
from sixstars.factory.lines import Line, Word
from sixstars.factory.params import FactoryParams
from sixstars.factory.story import DEFAULT_STORY, StoryRules
from sixstars.factory.styles import DEFAULT_STYLES, StyleCatalog
from sixstars.factory.subtitles import SubtitleStyle, build_ass, caption_bottom, credit_bottom
from sixstars.live.session import Route, cut_range
from sixstars.spikes import DetectedSpike

STORY = StoryRules.from_setting(DEFAULT_STORY)
CATALOG = StyleCatalog.from_setting(DEFAULT_STYLES)
BASE_REPLY: dict[str, object] = {
    "start_s": 40.0,
    "end_s": 70.0,
    "hook": "Hook",
    "title": "Title",
    "caption": "Caption",
    "hashtags": ["a", "b", "c"],
    "topic": "x",
    "spoiler": False,
    "safety": {},
    "score_parts": {"hook": 5, "clarity": 5, "payoff": 5, "rewatch": 5},
    "score": 5,
    "reject": False,
}


# ── story rules ──────────────────────────────────────────────


def test_story_rules_apply_to_hourly_normal_renders_of_allowed_sources() -> None:
    assert STORY.enabled and (STORY.min_s, STORY.max_s) == (60.0, 150.0)
    assert STORY.applies(lane="hourly", mode="normal", source_type="vod")
    assert STORY.applies(lane="hourly", mode="normal", source_type="live")
    assert not STORY.applies(lane="spike", mode="express", source_type="live")
    assert not STORY.applies(lane="hourly", mode="express", source_type="vod")
    assert STORY.widen(100.0, 160.0) == (10.0, 190.0)
    off = StoryRules.from_setting(None)
    assert not off.enabled and off.widen(100.0, 160.0) == (100.0, 160.0)
    assert not off.applies(lane="hourly", mode="normal", source_type="vod")
    assert not StoryRules.from_setting({"enabled": True, "min_s": 200, "max_s": 100}).enabled
    vod_only = StoryRules.from_setting({**DEFAULT_STORY, "sources": ["vod", "nonsense"]})
    assert vod_only.sources == ("vod",) and not vod_only.applies(
        lane="hourly", mode="normal", source_type="live"
    )


# ── the writer's story trim ──────────────────────────────────


def schema(story_max: float = 150.0) -> type:
    return clip_writing_schema(200.0, 60.0, 15.0, 60.0, {0: "no way"}, 60.0, story_max)


@pytest.mark.parametrize(
    ("story", "kept"),
    [
        ((10.0, 120.0), True),  # 110 s, contains the short cut
        ((10.0, 30.0), False),  # too short and does not overlap the short cut
        ((0.0, 190.0), False),  # 190 s: too long
        ((80.0, 170.0), False),  # does not overlap 40..70
        ((-30.0, 60.0), False),  # starts before the window
        ((None, 120.0), False),  # half a trim
    ],
)
def test_a_story_trim_is_kept_only_when_it_fits(story: tuple[float | None, float | None], kept: bool) -> None:
    reply = schema().model_validate({**BASE_REPLY, "story_start_s": story[0], "story_end_s": story[1]})
    assert reply.has_story is kept
    if not kept:
        assert reply.story_start_s is None and reply.story_end_s is None


def test_no_story_is_kept_when_none_was_asked_for() -> None:
    reply = schema(story_max=0.0).model_validate({**BASE_REPLY, "story_start_s": 10.0, "story_end_s": 120.0})
    assert not reply.has_story


# ── the wider live cut ───────────────────────────────────────


def spike() -> DetectedSpike:
    t = 1_794_900_000.0
    return DetectedSpike(  # type: ignore[call-arg]
        mode="live",
        peak_t=t + 100,
        first_t=t + 95,
        score_z=6.2,
        parts={},
        msgs=1,
        uniq=1,
        reaction_count=0,
        clip_requests=0,
        tier="A",
        window_start=t + 75,
        window_end=t + 120,
        spike_floor=t + 95,
        pad_start=t + 60,
        pad_end=t + 135,
        chat_sample=[],
    )


def test_an_hourly_live_cut_gets_room_for_a_story_and_an_express_one_does_not() -> None:
    s = spike()
    normal = Route("normal", "clip.render", 60, "hourly", "normal")
    express = Route("express", "express.render", 100, "spike", "express")
    assert cut_range(s, normal, 6, STORY) == (s.pad_start - 90, s.pad_end + 30)
    assert cut_range(s, normal, 6) == (s.pad_start, s.pad_end)
    assert cut_range(s, express, 6, STORY) == (s.pad_start, s.window_end + 6)


# ── style director: a story favours the calm styles ──────────


def test_a_story_moment_leans_to_the_word_captions_and_the_title() -> None:
    accounts = [AccountSlot("instagram_main", "instagram", ("hourly",))]
    facts = clip_facts({}, has_facecam=True)
    rules = DirectorRules.from_setting(
        {"no_repeat_n": 0, "affinity": {"story": {"cam_top_wordcap": 1000, "cam_top_title": 1000}}}
    )
    for n in range(20):
        plan = plan_styles(f"s{n}", "hourly", accounts, CATALOG, rules, facts=facts, moment_type="story")
        assert plan.by_account["instagram_main"] in {"cam_top_wordcap", "cam_top_title"}


# ── one-word captions ────────────────────────────────────────


def test_word_captions_sit_on_the_facecam_and_light_up_the_spoken_word(params: FactoryParams) -> None:
    rules = CATALOG.rules_for("cam_top_wordcap")
    assert rules.facecam_top and rules.captions and not rules.show_title and rules.caption_y == 580
    style = SubtitleStyle.from_rules(rules)
    assert caption_bottom("C", params, style) == pytest.approx(580 + 92 / 2)
    unmoved = replace(style, centre_y=0)
    assert credit_bottom("C", params, style) == credit_bottom("C", params, unmoved), "the credit stays put"
    lines = [Line(0, (Word("gonna", 1.0, 1.4), Word("shoot", 1.4, 2.0)))]
    ass = build_ass(lines, layout="C", params=params, duration_s=5.0, style=style)
    captions = [line for line in ass.splitlines() if ",Caption," in line]
    assert len(captions) == 2, "one event per word of the chunk"
    yellow = "&H00D4FF&"
    assert yellow in captions[0].split("GONNA")[0] and yellow not in captions[0].split("GONNA")[1]
    assert yellow in captions[1].split("SHOOT")[0].split("GONNA")[1]
    assert "\\fscx80" in captions[0] and "\\fscx80" not in captions[1], "the pop only on the first word"

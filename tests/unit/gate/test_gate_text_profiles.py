"""Text helpers of the gate and the settings parser."""

from datetime import UTC, datetime
from typing import Any

import pytest
from gate_helpers import DEFAULT_VALUES

from sixstars.gate import text
from sixstars.gate.checks import effective_music_ratio
from sixstars.gate.profiles import GateSettingsError, settings_from_values


@pytest.mark.parametrize(
    ("value", "term", "found"),
    [
        ("The FINAL mission!", "final mission", True),
        ("final-mission", "final mission", True),
        ("final  \n mission", "Final Mission", True),
        ("the final big mission", "final mission", False),
        ("mission final", "final mission", False),
        ("spending", "ending", False),
        ("the ending.", "ending", True),
        ("He’s dead", "hes dead", True),
        ("", "ending", False),
        (None, "ending", False),
        ("ending", "", False),
        ("ending", "!!!", False),
    ],
)
def test_contains_term(value: str | None, term: str, found: bool) -> None:
    assert text.contains_term(value, term) is found


@pytest.mark.parametrize(
    ("tag", "term", "found"),
    [
        ("finalmission", "final mission", True),
        ("#FinalMission", "final mission", True),
        ("final_mission", "final mission", True),
        ("ending", "ending", True),
        ("trending", "ending", False),
        ("gtafinalmission", "final mission", True),
    ],
)
def test_hashtag_has_term(tag: str, term: str, found: bool) -> None:
    assert text.hashtag_has_term(tag, term) is found


def test_find_terms_names_the_field() -> None:
    hits = text.find_terms([("hook", "the ending"), ("title", "fine")], ["Ending", "gta"], ["ending", "boss"])
    assert hits == ["hook: ending", "hashtags: ending"]


@pytest.mark.parametrize(
    ("word", "stemmed"),
    [
        ("robs", "rob"),
        ("robbing", "rob"),
        ("jumps", "jump"),
        ("jumping", "jump"),
        ("downed", "down"),
        ("bridge", "bridge"),
        ("gas", "gas"),
        ("crashes", "crash"),
    ],
)
def test_stem(word: str, stemmed: str) -> None:
    assert text.stem(word) == stemmed


def test_topic_measures() -> None:
    a = text.topic_tokens("new-player-robs-man-on-ground")
    b = text.topic_tokens("player-caught-robbing-downed-player")
    assert a == {"new", "player", "rob", "man", "ground"} and b == {"player", "caught", "rob", "down"}
    assert text.overlap_coefficient(a, b) == 0.5
    assert text.jaccard(a, b) == pytest.approx(2 / 7)
    assert text.overlap_coefficient(a, frozenset()) == 0.0 and text.jaccard(frozenset(), b) == 0.0
    # One shared word is not enough between longer topics.
    assert text.overlap_coefficient(frozenset({"a1", "b1", "c1"}), frozenset({"a1", "x", "y"})) == 0.0
    assert text.same_topic("A-b_c", "a b c")
    assert not text.same_topic("", "") and not text.same_topic(None, None)


def test_transcript_overlap() -> None:
    base = "no way he actually landed that jump over the whole bridge"
    assert text.transcript_overlap(base, "chat " + base + " lets go") == 1.0
    assert (
        text.transcript_overlap(base, "completely different words are being said in this other clip") == 0.0
    )
    assert text.transcript_overlap("too short", base) == 0.0
    assert text.transcript_overlap(None, base) == 0.0


@pytest.mark.parametrize(
    ("ratio", "frames", "min_run", "expected"),
    [
        (None, (True, True), 2, None),
        (0.5, None, 2, 0.5),
        (0.5, (), 2, 0.5),
        (0.25, (True, False, False, False), 1, 0.25),
        (0.25, (True, False, False, False), 2, 0.0),
        (0.5, (True, True, False, False), 2, 0.5),
        (0.75, (True, True, False, True), 2, 0.5),
        (0.5, (False, False, True, True), 2, 0.5),
        (0.1, (True, True, True, True), 2, 0.1),  # never above the stored ratio
        (0.75, (True, True, True, False), 3, 0.75),
    ],
)
def test_effective_music_ratio(
    ratio: float | None, frames: tuple[bool, ...] | None, min_run: int, expected: float | None
) -> None:
    assert effective_music_ratio(ratio, frames, min_run) == expected


def test_settings_from_the_documented_values() -> None:
    settings = settings_from_values(DEFAULT_VALUES)
    assert (settings.hourly.min_len_s, settings.hourly.max_len_s) == (20.0, 60.0)
    assert (settings.express.min_len_s, settings.express.max_len_s) == (15.0, 45.0)
    assert (settings.hourly.max_music_ratio, settings.express.max_music_ratio) == (0.1, 0.0)
    assert (settings.hourly.min_score, settings.express.min_score) == (7.0, 8.0)
    assert (settings.hourly.topic_dedupe_h, settings.express.topic_dedupe_h) == (48.0, 6.0)
    assert settings.express.tiers == ("preferred", "auto_no_music") and settings.hourly.precheck_risky
    assert settings.spoiler_block_until == datetime(2026, 12, 3, tzinfo=UTC)
    assert settings.auto_creator_daily_cap == 4 and settings.end_card_s == 1.5
    assert (settings.max_black_s, settings.max_freeze_s, settings.freeze_noise) == (2.0, 2.0, 0.003)
    assert (settings.width, settings.height, settings.max_file_mb) == (1080, 1920, 100.0)
    assert settings.profile("hourly") is settings.hourly and settings.profile("express") is settings.express
    with pytest.raises(ValueError, match="vip"):
        settings.profile("vip")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("gate.hourly", None),
        ("gate.hourly", {"min_len": 20}),
        ("gate.hourly", {**DEFAULT_VALUES["gate.hourly"], "max_music_ratio": "0.1"}),
        ("gate.hourly", {**DEFAULT_VALUES["gate.hourly"], "max_music_ratio": 1.5}),
        ("gate.hourly", {**DEFAULT_VALUES["gate.hourly"], "min_len": 70}),
        ("gate.hourly", {**DEFAULT_VALUES["gate.hourly"], "precheck_risky": "yes"}),
        ("gate.express", {**DEFAULT_VALUES["gate.express"], "tiers": ["vip"]}),
        ("gate.express", {**DEFAULT_VALUES["gate.express"], "tiers": None}),
        ("gate.express", {**DEFAULT_VALUES["gate.express"], "min_score": True}),
        ("gate.spoiler_block_until", "next week"),
        ("gate.spoiler_block_until", 5),
        ("gate.spoiler_terms", "ending"),
        ("gate.reject_topics", [1, 2]),
        ("gate.auto_creator_daily_cap", -1),
        ("gate.music_min_run_frames", 0),
        ("gate.strict_max_music_ratio", 2),
        ("gate.checks", {}),
        ("gate.duplicate", []),
        ("factory.end_card_s", "1.5"),
    ],
)
def test_malformed_settings_are_refused(key: str, value: Any) -> None:
    with pytest.raises(GateSettingsError, match=key.split(".")[1]):
        settings_from_values({**DEFAULT_VALUES, key: value})


@pytest.mark.parametrize("key", sorted(DEFAULT_VALUES))
def test_a_missing_setting_is_refused(key: str) -> None:
    values = {name: value for name, value in DEFAULT_VALUES.items() if name != key}
    with pytest.raises(GateSettingsError):
        settings_from_values(values)


def test_null_block_date_and_naive_time() -> None:
    assert (
        settings_from_values({**DEFAULT_VALUES, "gate.spoiler_block_until": None}).spoiler_block_until is None
    )
    naive = settings_from_values({**DEFAULT_VALUES, "gate.spoiler_block_until": "2026-12-03T00:00:00"})
    assert naive.spoiler_block_until == datetime(2026, 12, 3, tzinfo=UTC)

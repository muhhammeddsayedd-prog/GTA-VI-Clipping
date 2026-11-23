"""Title writer v2: the code checks, choice, features and fallback (docs/30-titles-v2.md Part 2). Pure."""

import pytest

from sixstars.titles import checks
from sixstars.titles.facts import NameBook, Person, TitleFacts

BOOK = NameBook(
    (
        Person("IShowSpeed", ("ishowspeed", "speed"), "S"),
        Person("Kai Cenat", ("kai cenat", "kaicenat"), "S"),
        Person("Jynxzi", ("jynxzi",), "S"),
        Person("Vinesauce Vinny", ("vinesauce vinny", "vinny", "vargskelethor"), "B"),
        Person("Ludwig", ("ludwig",), "A"),
    )
)
FACTS = TitleFacts(
    streamer="Kai Cenat",
    streamer_tier="S",
    event="Kai argues with a cyclist who crashes into his car, then chases him down the street.",
    peak_line="bro you hit MY car",
    numbers={"hours_into_stream": 5.6},
    chat_words=("kekw", "cyclist"),
)


def failed(title: str, facts: TitleFacts = FACTS) -> list[str]:
    return checks.check_title(title, facts, BOOK)


def test_a_title_in_the_winning_shape_passes() -> None:
    assert failed("Kai Cenat's Argument With a Cyclist Gets Out of Hand in GTA 6 😭") == []
    assert failed("Kai Cenat Chases a Cyclist After 5 Hours in GTA 6 😭") == []


@pytest.mark.parametrize(
    ("title", "rule"),
    [
        ("A Cyclist Hits Kai Cenat's Car in GTA 6 😭", "name_first"),
        ("Kai Cenat Argues With a Cyclist on the Street 😭", "gta"),
        ("Kai Cenat Fights in GTA 6 😭", "length"),
        ("Kai Cenat Argues With a Cyclist and Chases Him Across the Whole City in GTA 6 😭", "length"),
        ("Kai Cenat's Cyclist Fight Has Chat Going Crazy in GTA 6 😭", "banned"),
        ("Kai Cenat Loses It at a Cyclist on the Street in GTA 6", "banned"),
        ("Kai Cenat 😭 Argues With a Cyclist on the Street in GTA 6", "emoji"),
        ("Kai Cenat Argues With a Cyclist on the Street in GTA 6 😭💀🔥", "emoji"),
        ("Kai Cenat CHASES a CYCLIST Down the Street in GTA 6 😭", "caps"),
        ("Kai Cenat vs a Cyclist: Who Wins in GTA 6? 😭", "format"),
        ("Kai Cenat Chases a Cyclist After 52 Hours in GTA 6 😭", "facts"),
        ("Kai Cenat Is the First to Fight a Cyclist in GTA 6 😭", "facts"),
        ("Kai Cenat and Jynxzi Chase a Cyclist Down the Street in GTA 6 😭", "names"),
    ],
)
def test_each_rule_rejects(title: str, rule: str) -> None:
    assert any(reason.startswith(rule) for reason in failed(title)), failed(title)


def test_a_quote_only_title_fails() -> None:
    reasons = failed('"Kai Cenat Says You Hit My Car in GTA 6 Bro"')
    assert any(reason.startswith("format: only a quote") for reason in reasons)


def test_rounded_and_whole_numbers_of_a_fact_are_allowed() -> None:
    assert failed("Kai Cenat Chases a Cyclist After 6 Hours in GTA 6 😭") == []
    assert failed("Kai Cenat Chases a Cyclist After 5.6 Hours in GTA 6 😭") == []


def test_money_in_k_matches_the_fact() -> None:
    facts = TitleFacts("Kai Cenat", "S", "Kai loses all his money", numbers={"money": 10000.0})
    assert failed("Kai Cenat LOSES His $10K Car Bet in GTA 6 😭", facts) == []


def test_gta_6_does_not_count_as_a_number() -> None:
    assert checks.title_numbers("Kai Cenat Gets Robbed in GTA 6 😭") == []
    assert checks.title_numbers("Speed Finished GTA VI After 52 Hours") == [52.0]


def test_a_guest_streamer_may_lead_and_be_named() -> None:
    facts = TitleFacts(
        "Jynxzi",
        "S",
        "Jynxzi watches IShowSpeed beat the game",
        other_streamers=("IShowSpeed",),
        other_tiers=("S",),
    )
    assert failed("Jynxzi Watches IShowSpeed Beat the Story in GTA 6 💀😭", facts) == []
    assert failed("IShowSpeed Gets Watched by Jynxzi All Night in GTA 6 😭", facts) == []


def test_an_alias_counts_as_the_name() -> None:
    facts = TitleFacts("IShowSpeed", "S", "Speed jumps a car off a pier")
    assert failed("Speed Jumps His Car Straight Off a Pier in GTA 6 😭", facts) == []
    assert failed("Speed's Car Jump Off a Pier Goes Wrong in GTA 6 😭", facts) == []


def test_allowed_acronyms_and_a_capitalised_name_are_not_caps_words() -> None:
    assert checks.caps_words("Kai Cenat Fights an NPC in GTA VI", ["Kai Cenat"]) == []
    assert checks.caps_words("LIRIK Finds a Glitch in GTA 6", ["LIRIK"]) == []
    assert checks.caps_words("Adin Ross LEAKS His $10K Code in GTA 6") == ["LEAKS"]


def test_spoiler_clips_may_not_hint_at_the_story() -> None:
    facts = TitleFacts("Kai Cenat", "S", "Kai reacts to a story scene", spoiler=True)
    assert any(
        r.startswith("spoiler") for r in failed("Kai Cenat Can't Believe the Ending of GTA 6 💔", facts)
    )
    assert failed("Kai Cenat Can't Hold Back His Tears Playing GTA 6 💔", facts) == []
    # The same word is fine for a clip that is not a story spoiler.
    plain = TitleFacts("Kai Cenat", "S", "Kai sees the ending")
    assert failed("Kai Cenat Can't Believe the Ending of GTA 6 💔", plain) == []


def test_emoji_and_word_counting() -> None:
    assert checks.emoji_count("Speed Cries in GTA 6 😭💀") == 2
    assert checks.emoji_count("Speed 👍🏽 GTA 6") == 1  # a skin tone is not a second emoji
    assert checks.words("Kai Cenat & IShowSpeed Got The Same GTA 6 Ending! 💔") == [
        "Kai", "Cenat", "IShowSpeed", "Got", "The", "Same", "GTA6", "Ending!",
    ]  # fmt: skip
    assert checks.emoji_only_at_end("Speed Cries in GTA 6 😭 💀")
    assert not checks.emoji_only_at_end("Speed 😭 Cries in GTA 6")


CANDIDATES = [
    {
        "pattern": "fail",
        "title": "Kai Cenat's Cyclist Chase Falls Apart in GTA 6 💀",
        "facts_used": ["event"],
    },
    {
        "pattern": "stakes",
        "title": "Kai Cenat Chases a Cyclist After 6 Hours in GTA 6 😭",
        "facts_used": ["numbers"],
    },
    {"pattern": "emotion", "title": "Chat Can't Believe Kai Cenat in GTA 6 😭", "facts_used": ["event"]},
    {
        "pattern": "discovery",
        "title": "Kai Cenat Learns Cyclists Fight Back in GTA 6 😳",
        "facts_used": ["event"],
    },
    {
        "pattern": "made_up",
        "title": "Kai Cenat Argues With a Cyclist on the Street in GTA 6 😭",
        "facts_used": [],
    },
]


def test_choose_takes_the_first_survivor_in_pattern_order() -> None:
    checked = checks.check_all(CANDIDATES, FACTS, BOOK)
    assert [item.passed for item in checked] == [True, True, False, True, False]
    assert any("pattern: unknown" in reason for reason in checked[4].failed)
    choice = checks.choose(checked, exploring=False)
    assert choice == checks.Choice(1, False)  # stakes before discovery before fail
    assert checks.choose(checked, exploring=True) == checks.Choice(3, True)  # the runner-up: discovery


def test_choose_returns_none_when_nothing_passes() -> None:
    checked = checks.check_all([CANDIDATES[2]], FACTS, BOOK)
    assert checks.choose(checked, exploring=False) is None
    assert "Chat Can't Believe" in checks.retry_note(checked)


def test_exploring_with_one_survivor_uses_it() -> None:
    checked = checks.check_all([CANDIDATES[0], CANDIDATES[2]], FACTS, BOOK)
    assert checks.choose(checked, exploring=True) == checks.Choice(0, False)


def test_explore_is_stable_and_near_the_share() -> None:
    assert checks.explore("clip-1", 0.2) == checks.explore("clip-1", 0.2)
    assert not checks.explore("clip-1", 0.0)
    share = sum(checks.explore(f"clip-{i}", 0.2) for i in range(2000)) / 2000
    assert 0.17 < share < 0.23


def test_fallback_title_passes_the_shape_checks() -> None:
    title = checks.fallback_title(FACTS, "streamer-argues-with-cyclist")
    assert title == "Kai Cenat Argues With Cyclist in GTA 6 😭"
    reasons = failed(title)
    assert not [r for r in reasons if r.split(":")[0] in ("name_first", "gta", "length", "emoji", "caps")]
    assert checks.fallback_title(FACTS, None) == "Kai Cenat in GTA 6 😭"


def test_caption_checks() -> None:
    assert checks.check_caption("kai really chased a cyclist down 😭", FACTS) == []
    assert checks.check_caption("chat when kai chased the cyclist", FACTS)
    assert checks.check_caption("x" * 81, FACTS)
    assert checks.check_caption("kai vs cyclist #gta6", FACTS)
    assert checks.check_caption("", FACTS) == ["caption: empty"]


def test_features_record_the_traits() -> None:
    title = "Kai Cenat Chases a Cyclist After 6 Hours in GTA 6 😭"
    feats = checks.features(title, "stakes", FACTS, BOOK, explored=True, source="v2").as_dict()
    assert feats == {
        "title_pattern": "stakes",
        "word_count": 10,
        "char_count": len(title),
        "has_number": True,
        "has_stake": True,
        "emoji_count": 1,
        "caps_words": 0,
        "name_tier": "S",
        "names": 1,
        "explored": True,
        "source": "v2",
    }


# Regressions found by the staging comparison on 2026-11-23.


def test_digits_inside_a_streamer_name_are_not_numbers() -> None:
    book = NameBook(
        (Person("Quin69", ("quin69", "quin"), "B"), Person("Rostislav 999", ("rostislav 999",), "C"))
    )
    facts = TitleFacts("Quin69", "B", "Quin gets shot on a rooftop", other_streamers=("Rostislav 999",))
    assert checks.check_title("Quin69 Walks Onto a Cartel Rooftop Unprepared in GTA 6 😭", facts, book) == []
    assert (
        checks.check_title("Quin69 and Rostislav 999 Storm a Cartel Rooftop in GTA 6 😭", facts, book) == []
    )
    reasons = checks.check_title("Quin69 Walks Onto a Rooftop After 52 Hours in GTA 6 😭", facts, book)
    assert any("number 52" in r for r in reasons)


def test_an_all_caps_name_of_two_words_is_not_a_caps_word() -> None:
    assert checks.caps_words("LRU ROSE Finds a Mess in the Sink in GTA 6", ["LRU ROSE"]) == []


def test_fallback_for_a_topic_that_is_not_about_the_streamer() -> None:
    assert (
        checks.fallback_title(FACTS, "police-chase-boat-crash")
        == "Kai Cenat's Police Chase Boat Crash in GTA 6 😭"
    )


def test_this_happens_is_banned() -> None:
    assert any(r.startswith("banned") for r in failed("Kai Cenat Is 6 Hours Into GTA 6 When This Happens 😭"))

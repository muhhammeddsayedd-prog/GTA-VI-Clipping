"""Title writer v2: the facts builder and the name book. Pure."""

import json

from sixstars.titles import facts
from sixstars.titles.facts import TimedLine

ROWS = [
    {"handle": "kaicenat", "display_name": "KaiCenat", "title_name": "Kai Cenat",
     "title_aliases": ["kai cenat", "kaicenat"], "follower_count": 21_998_769},
    {"handle": "vargskelethor", "display_name": "vargskelethor", "title_name": "Vinesauce Vinny",
     "title_aliases": ["vinesauce vinny", "vinny"], "follower_count": 400_000},
    {"handle": "joe_bartolozzi", "display_name": "Joe_Bartolozzi", "title_name": None,
     "title_aliases": [], "follower_count": 3_890_651},
    {"handle": "sasatikk", "display_name": "sasatikk", "title_name": None, "title_aliases": [],
     "follower_count": 50_000},
]  # fmt: skip
S_NAMES = ["Kai Cenat", "IShowSpeed", "N3on"]
TIERS = {"A": 1_000_000, "B": 200_000}


def book() -> facts.NameBook:
    return facts.book_from_rows(ROWS, {"N3on": ["n3on"]}, S_NAMES, TIERS)


def test_clean_name_never_uses_the_lowercase_login() -> None:
    assert facts.clean_name("Joe_Bartolozzi", "joe_bartolozzi") == "Joe Bartolozzi"
    assert facts.clean_name("sasatikk", "sasatikk") == "Sasatikk"
    assert facts.clean_name("caseoh_", "caseoh_") == "Caseoh"
    assert facts.clean_name("みつきさんです", "mitsuki_mitsuu") == "Mitsuki mitsuu"
    assert facts.clean_name(None, "lirik") == "Lirik"


def test_book_names_and_tiers() -> None:
    names = book()
    assert [p.name for p in names.people] == [
        "Kai Cenat",
        "Vinesauce Vinny",
        "Joe Bartolozzi",
        "Sasatikk",
        "N3on",
    ]
    assert names.tier_of("Kai Cenat") == "S"
    assert names.tier_of("Joe Bartolozzi") == "A"
    assert names.tier_of("Vinesauce Vinny") == "B"
    assert names.tier_of("Sasatikk") == "C"
    assert names.tier_of("N3on") == "S"
    assert "vinny" in names.aliases_of("Vinesauce Vinny")
    assert names.names_in("yo vinny and N3ON are here") == ["Vinesauce Vinny", "N3on"]
    assert names.names_in("kaine is not kai cenat") == ["Kai Cenat"]


def test_peak_line_prefers_the_longest_line_near_the_peak() -> None:
    lines = [
        TimedLine(0.0, 2.0, "okay let's go"),
        TimedLine(10.0, 12.0, "no no no no he took my car"),
        TimedLine(13.0, 14.0, "what"),
        TimedLine(25.0, 27.0, "this is the longest line of the whole clip by far really"),
    ]
    assert facts.peak_line(lines, 13.5) == "no no no no he took my car"
    assert facts.peak_line(lines, None) == "this is the longest line of the whole clip by far really"
    assert facts.peak_line([], 3.0) is None


def test_spoken_numbers() -> None:
    assert facts.spoken_numbers("I've played 52 hours and I'm 87% done, that cost me $10k") == {
        "hours_played": 52.0,
        "percent_complete": 87.0,
        "money": 10_000.0,
    }
    assert facts.spoken_numbers("five million dollars") == {}
    assert facts.spoken_numbers("I lost 2 million dollars") == {"money": 2_000_000.0}


def test_hours_into_stream_only_from_one_hour() -> None:
    assert facts.hours_into_stream(1800) is None
    assert facts.hours_into_stream(5 * 3600 + 1200) == 5.0
    assert facts.hours_into_stream(5 * 3600 + 2400) == 6.0
    assert facts.hours_into_stream(None) is None


def test_chat_words_counts_each_message_once() -> None:
    messages = ["KEKW KEKW KEKW", "kekw he crashed", "crashed lol", "LMAO", "one"]
    assert facts.chat_words(messages) == ("kekw", "crashed")


def test_build_finds_other_streamers_and_numbers() -> None:
    lines = [TimedLine(0.0, 3.0, "vinny is going to love this, 3 hours of progress gone")]
    built = facts.build(
        streamer="Kai Cenat",
        book=book(),
        event="Kai's car flips and he loses the mission.",
        lines=lines,
        peak_s=1.0,
        offset_s=4 * 3600,
        chat=["W", "LMAO"],
        gta_label="GTA 6",
        spoiler=False,
    )
    assert built.streamer_tier == "S"
    assert built.other_streamers == ("Vinesauce Vinny",)
    assert built.numbers == {"hours_into_stream": 4.0, "hours_played": 3.0}
    payload = json.loads(built.to_json())
    assert payload["other_streamers"] == [{"name": "Vinesauce Vinny", "tier": "B"}]
    assert payload["gta_label"] == "GTA 6"
    assert payload["campaign"] is None


def test_hashtags_are_the_streamer_then_the_tail() -> None:
    built = facts.TitleFacts("Kai Cenat", "S", "x")
    assert facts.hashtags(built, ["streamerclips"]) == ["kaicenat", "streamerclips"]

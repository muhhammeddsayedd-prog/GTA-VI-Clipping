"""Subtitle line grouping and profanity matching, masking and bleep spans."""

import pytest

from sixstars.factory.bleep import (
    ProfanityMatcher,
    Span,
    bleep_spans,
    mask_word,
    normalize_word,
    parse_profanity_list,
)
from sixstars.factory.lines import Line, LineRules, Word, clip_lines, group_lines, words_from_transcript
from sixstars.factory.window import ClipWindow


def spoken(text: str, *, gap_after: dict[int, float] | None = None, step: float = 0.3) -> list[Word]:
    """Words spoken back to back, `step` seconds each, with optional pauses after given word indices."""
    words, t = [], 0.0
    for i, token in enumerate(text.split()):
        words.append(Word(token, t, t + step))
        t += step + (gap_after or {}).get(i, 0.0)
    return words


def texts(lines: list[Line]) -> list[str]:
    return [line.text for line in lines]


GROUPING_CASES = [
    ("four words then break", "one two three four five six", None, ["one two three four", "five six"]),
    (
        "character limit",
        "absolutely unbelievable getaway driver",
        None,
        ["absolutely", "unbelievable getaway", "driver"],
    ),
    ("punctuation ends a line", "No way, he did it. Wow", None, ["No way,", "he did it.", "Wow"]),
    ("punctuation needs two words", "Wait, what is happening", None, ["Wait, what", "is happening"]),
    ("pause always breaks", "hold on now", {0: 0.5}, ["hold", "on now"]),
    ("short pause does not break", "hold on now", {0: 0.3}, ["hold on now"]),
    ("orphan is rebalanced", "one two three four five", None, ["one two three", "four five"]),
    (
        "closing quote after punctuation",
        'he said "no way." then left',
        None,
        ['he said "no way."', "then left"],
    ),
    (
        "one long word",
        "supercalifragilisticexpialidocious yes",
        None,
        ["supercalifragilisticexpialidocious", "yes"],
    ),
    ("empty", "", None, []),
]


@pytest.mark.parametrize(
    ("text", "gaps", "expected"),
    [case[1:] for case in GROUPING_CASES],
    ids=[case[0] for case in GROUPING_CASES],
)
def test_group_lines(text: str, gaps: dict[int, float] | None, expected: list[str]) -> None:
    lines = group_lines(spoken(text, gap_after=gaps))
    assert texts(lines) == expected
    assert [line.index for line in lines] == list(range(len(lines)))


def test_group_lines_respects_limits_on_a_real_transcript(transcript: dict[str, object]) -> None:
    lines = group_lines(words_from_transcript(transcript))
    assert texts(lines) == [
        "He did NOT",
        "expect that!",
        "No way,",
        "fifty thousand dollars",
        "this fucking cop just",
        "crashed into",
        "the helicopter",
        "and exploded",
        "everywhere",
    ]
    for line in lines:
        assert len(line.words) <= 4 and len(line.text) <= 22
        assert line.start_s == line.words[0].start_s and line.end_s == line.words[-1].end_s


def test_group_lines_with_custom_rules() -> None:
    lines = group_lines(
        spoken("a b c d e f"), LineRules(min_words=1, max_words=2, max_chars=10, pause_s=0.35)
    )
    assert texts(lines) == ["a b", "c d", "e f"]


def test_words_from_transcript_cleans_input() -> None:
    words = words_from_transcript(
        {
            "language": "en",
            "words": [
                {"w": " way ", "s": 12.51, "e": 12.8},
                {"w": "", "s": 1, "e": 2},
                {"w": "no", "s": 12.34, "e": 12.0},
            ],
        }
    )
    assert words == [Word("no", 12.34, 12.34), Word("way", 12.51, 12.8)]


@pytest.mark.parametrize(
    "transcript",
    [
        {},
        {"words": "nope"},
        {"words": [5]},
        {"words": [{"w": "x", "s": "0", "e": 1}]},
        {"words": [{"w": 1, "s": 0, "e": 1}]},
        {"words": [{"w": "x", "s": float("nan"), "e": 1}]},
    ],
)
def test_malformed_transcripts_are_rejected(transcript: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        words_from_transcript(transcript)


def test_clip_lines_shifts_trims_and_keeps_line_numbers() -> None:
    lines = [
        Line(0, (Word("before", 1.0, 1.5), Word("cut", 4.8, 5.2))),
        Line(1, (Word("in", 5.2, 5.6), Word("side", 6.0, 6.4))),
        Line(2, (Word("last", 9.8, 10.6), Word("gone", 10.0, 10.2))),
        Line(3, (Word("after", 11.0, 11.5),)),
    ]
    clipped = clip_lines(lines, ClipWindow(5.0, 10.0))
    assert [line.index for line in clipped] == [1, 2]
    assert clipped[0].words == (
        Word("in", pytest.approx(0.2), pytest.approx(0.6)),
        Word("side", 1.0, pytest.approx(1.4)),
    )
    assert clipped[1].words == (Word("last", pytest.approx(4.8), 5.0),)


def test_parse_profanity_list_skips_comments_and_normalises() -> None:
    terms = parse_profanity_list(["# header", "", "Fuck", "  shit  # inline", "son of a  bitch", "#only"])
    assert terms == frozenset({"fuck", "shit", "son of a bitch"})


def test_repo_profanity_list_loads() -> None:
    from pathlib import Path

    from sixstars.factory.bleep import load_profanity_list

    terms = load_profanity_list(Path(__file__).resolve().parents[3] / "assets" / "profanity.txt")
    assert {"fuck", "shit", "bitch"} <= terms and all(
        term == term.lower() and "#" not in term for term in terms
    )


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("fuck", True),
        ("Fuck!", True),
        ("FUCKING", True),
        ("fuckin'", True),
        ("fucked.", True),
        ("fucks", True),
        ("fucker", True),
        ("shitting", True),
        ("shitted", True),
        ("asses", True),
        ('"shit"', True),
        ("shit's", True),
        ("assume", False),
        ("class", False),
        ("shiitake", False),
        ("Scunthorpe", False),
        ("bass", False),
        ("fuchsia", False),
        ("", False),
        ("!!!", False),
    ],
)
def test_is_profane(profanity_terms: frozenset[str], word: str, expected: bool) -> None:
    assert ProfanityMatcher(profanity_terms).is_profane(word) is expected


def test_find_marks_single_words_and_whole_phrases(profanity_terms: frozenset[str]) -> None:
    matcher = ProfanityMatcher(profanity_terms)
    words = ["you", "Son", "of", "a", "BITCH,", "that", "is", "fucking", "wild", "son", "of", "mine"]
    assert matcher.find(words) == [1, 2, 3, 4, 7]
    assert matcher.find([]) == []


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("fuck", "f***"),
        ("Fucking!", "F******!"),
        ('"shit,"', '"s***,"'),
        ("a", "a"),
        ("...", "..."),
        ("", ""),
        ("sh*t", "s***"),
    ],
)
def test_mask_word(word: str, expected: str) -> None:
    assert mask_word(word) == expected


def test_normalize_word() -> None:
    assert normalize_word("  “Hello!”") == "hello"
    assert normalize_word("Dog’s") == "dog"


def test_bleep_spans_pad_clamp_and_merge() -> None:
    words = [
        Word("a", 0.02, 0.3),
        Word("b", 0.36, 0.6),
        Word("c", 2.0, 2.5),
        Word("d", 9.8, 10.4),
        Word("e", 5.0, 5.0),
    ]
    spans = bleep_spans(words, [3, 0, 1, 2], clip_duration_s=10.0)
    assert spans == [
        Span(0.0, pytest.approx(0.65)),
        Span(pytest.approx(1.95), pytest.approx(2.55)),
        Span(pytest.approx(9.75), 10.0),
    ]
    assert bleep_spans(words, [], clip_duration_s=10.0) == []
    assert bleep_spans(words, [4], clip_duration_s=10.0, pad_s=0.0) == []

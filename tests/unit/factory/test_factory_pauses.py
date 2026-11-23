"""The snap-to-pause rule: phrase boundaries from word gaps and punctuation, the start and end moves and
their limits, and the repair of words that Whisper stretched across a silence. Pure functions, no I/O."""

import random
from typing import Any

import pytest

from sixstars.factory import transcript as transcripts
from sixstars.factory.lines import Word
from sixstars.factory.pauses import (
    Break,
    Silence,
    SnapRules,
    find_breaks,
    snap_to_pauses,
    tighten_words,
)
from sixstars.factory.window import ClipWindow

RULES = SnapRules(pause_s=0.4, start_back_s=4.0, start_forward_s=2.0, end_forward_s=3.0, end_back_s=2.0)


def speech(spec: str, start_s: float = 0.0, word_s: float = 0.3) -> list[Word]:
    """Words spoken back to back, `word_s` each. A token like `<0.5>` is a pause of that many seconds."""
    words: list[Word] = []
    cursor = start_s
    for token in spec.split():
        if token.startswith("<") and token.endswith(">"):
            cursor += float(token[1:-1])
            continue
        words.append(Word(token, round(cursor, 3), round(cursor + word_s, 3)))
        cursor += word_s
    return words


def at(words: list[Word], text: str) -> Word:
    return next(word for word in words if word.text == text)


def snap(window: ClipWindow, words: list[Word], rules: SnapRules = RULES, **limits: Any) -> ClipWindow:
    values: dict[str, Any] = {"floor_s": window.start_s, "min_len_s": 1.0, "max_len_s": 60.0, "limit_s": 90.0}
    values.update(limits)
    return snap_to_pauses(window, words, rules, **values)


# ── Phrase boundaries ────────────────────────────────────────


def test_breaks_come_from_pauses_and_sentence_marks() -> None:
    words = speech("<1.0> a b <0.5> c d. e f <0.2> g")
    found = find_breaks(words, 0.4, limit_s=10.0)
    assert [(item.index, item.kind) for item in found] == [
        (0, "pause"),  # silence before the first word
        (2, "pause"),
        (4, "sentence"),
        (7, "pause"),  # the file goes on for more than a pause after the last word
    ]
    assert found[1] == Break(2, "pause", end_s=1.6, start_s=2.1)


def test_no_breaks_without_a_threshold_or_words() -> None:
    assert find_breaks([], 0.4) == []
    assert find_breaks(speech("a <2.0> b"), 0.0) == []


def test_last_word_is_a_boundary_only_at_a_sentence_end_or_before_silence() -> None:
    assert [item.index for item in find_breaks(speech("a b"), 0.4, limit_s=0.7)] == []
    assert [item.kind for item in find_breaks(speech("a b."), 0.4, limit_s=0.7)] == ["sentence"]
    assert [item.kind for item in find_breaks(speech("a b"), 0.4, limit_s=1.0)] == ["pause"]


@pytest.mark.parametrize(
    ("gap_s", "is_break"),
    [(0.39, False), (0.4, True), (0.41, True)],
)
def test_a_gap_exactly_at_the_threshold_counts(gap_s: float, is_break: bool) -> None:
    words = speech(f"a b <{gap_s}> c d")
    assert bool(find_breaks(words, 0.4)) is is_break


# ── The start ────────────────────────────────────────────────

# "one two three" is the end of an earlier phrase; the clip was cut to start on "five".
TALK = speech("one two three <0.6> four five six seven eight nine ten <0.5> eleven twelve")


def test_start_moves_back_to_the_first_word_after_the_nearest_preceding_pause() -> None:
    window = ClipWindow(at(TALK, "five").start_s, 30.0)
    assert snap(window, TALK).start_s == at(TALK, "four").start_s


def test_start_already_on_a_phrase_is_left_alone() -> None:
    window = ClipWindow(at(TALK, "four").start_s, 30.0)
    assert snap(window, TALK) == window
    # A start inside the pause belongs to the phrase after it.
    inside_pause = ClipWindow(at(TALK, "four").start_s - 0.3, 30.0)
    assert snap(inside_pause, TALK).start_s == inside_pause.start_s


def test_no_gaps_available_leaves_the_window_unchanged() -> None:
    words = speech("a b c d e f g h i j k l m n o p")
    window = ClipWindow(at(words, "c").start_s, at(words, "m").start_s)
    assert snap(window, words, limit_s=at(words, "p").end_s) == window


def test_start_does_not_move_back_further_than_the_range() -> None:
    words = speech("a b <1.0> " + " ".join(f"w{i}" for i in range(20)))
    window = ClipWindow(at(words, "w17").start_s, 30.0)  # the pause is 5.1 s before the start
    assert snap(window, words, limit_s=window.end_s).start_s == window.start_s
    wider = SnapRules(pause_s=0.4, start_back_s=6.0)
    assert snap(window, words, wider, limit_s=window.end_s).start_s == at(words, "w0").start_s


def test_start_never_makes_the_clip_longer_than_the_maximum() -> None:
    start = at(TALK, "five").start_s
    window = ClipWindow(start, start + 20.0)
    rules = SnapRules(pause_s=0.4, start_back_s=4.0, start_forward_s=3.0)
    back = at(TALK, "four").start_s
    assert snap(window, TALK, rules, max_len_s=window.end_s - back, limit_s=window.end_s).start_s == back
    # Moving back to "four" would make the clip 20.3 s long: the pause ahead, before "eleven", is used.
    moved = snap(window, TALK, rules, max_len_s=20.0, floor_s=10.0, limit_s=window.end_s)
    assert moved.start_s == at(TALK, "eleven").start_s
    # With no room ahead either (the spike floor), the start stays where it was.
    assert snap(window, TALK, rules, max_len_s=20.0, limit_s=window.end_s).start_s == start


def test_start_moves_forward_only_up_to_the_spike_floor() -> None:
    words = speech(" ".join(f"w{i}" for i in range(20)) + " <0.5> x y z")
    window = ClipWindow(at(words, "w16").start_s, 30.0)  # no pause within 4 s before, one 1.7 s after
    target = at(words, "x").start_s
    assert snap(window, words, floor_s=target, limit_s=window.end_s).start_s == target
    assert snap(window, words, floor_s=target - 0.1, limit_s=window.end_s).start_s == window.start_s
    # The default floor of the helper is the start itself: no forward move at all.
    assert snap(window, words, limit_s=window.end_s).start_s == window.start_s


def test_start_moves_forward_only_within_the_forward_range() -> None:
    words = speech(" ".join(f"w{i}" for i in range(20)) + " <0.5> x y z")
    window = ClipWindow(at(words, "w12").start_s, 30.0)  # the pause is 2.9 s after the start
    assert snap(window, words, floor_s=20.0, limit_s=window.end_s).start_s == window.start_s


def test_start_never_makes_the_clip_shorter_than_the_minimum() -> None:
    words = speech(" ".join(f"w{i}" for i in range(20)) + " <0.5> x y z")
    start = at(words, "w16").start_s
    target = at(words, "x").start_s
    window = ClipWindow(start, start + 15.0)
    assert snap(window, words, floor_s=20.0, min_len_s=15.0, limit_s=window.end_s).start_s == start
    roomy = snap(window, words, floor_s=20.0, min_len_s=15.0 - (target - start), limit_s=window.end_s)
    assert roomy.start_s == target


def test_sentence_start_is_preferred_to_a_nearer_bare_pause() -> None:
    words = speech("Stop it. Now look at <0.5> this thing right here")
    window = ClipWindow(at(words, "thing").start_s, 30.0)
    # The pause before "this" is nearer, but "Now" begins the sentence.
    assert snap(window, words, limit_s=window.end_s).start_s == at(words, "Now").start_s
    unpunctuated = speech("Stop it Now look at <0.5> this thing right here")
    assert snap(window, unpunctuated, limit_s=window.end_s).start_s == at(unpunctuated, "this").start_s


# ── The end ──────────────────────────────────────────────────


def test_end_moves_forward_to_the_last_word_before_the_nearest_following_pause() -> None:
    window = ClipWindow(at(TALK, "four").start_s, at(TALK, "eight").start_s)  # cuts after "seven"
    assert snap(window, TALK).end_s == at(TALK, "ten").end_s


def test_end_already_on_a_phrase_is_left_alone() -> None:
    window = ClipWindow(at(TALK, "four").start_s, at(TALK, "ten").end_s + 0.2)
    assert snap(window, TALK) == window


def test_end_stays_inside_the_file_and_the_forward_range() -> None:
    words = speech("a b c d e f g h i j k l m n o p <0.6> q")
    window = ClipWindow(0.0, at(words, "c").start_s)  # the pause is 4.2 s after the end
    assert snap(window, words).end_s == window.end_s
    near = ClipWindow(0.0, at(words, "n").start_s)
    assert snap(near, words).end_s == at(words, "p").end_s
    assert snap(near, words, limit_s=at(words, "p").end_s - 0.1).end_s == near.end_s


def test_end_never_makes_the_clip_longer_than_the_maximum() -> None:
    window = ClipWindow(at(TALK, "four").start_s, at(TALK, "eight").start_s)
    target = at(TALK, "ten").end_s
    assert snap(window, TALK, max_len_s=target - window.start_s).end_s == target
    assert snap(window, TALK, max_len_s=target - window.start_s - 0.1).end_s == window.end_s


def test_end_moves_back_when_nothing_is_ahead_and_respects_the_minimum() -> None:
    words = speech("a b c d <0.5> e f g h i j k l m n o p q r s t u v w x y z")
    window = ClipWindow(0.0, at(words, "h").start_s)  # nothing ahead within 3 s; the pause is 1.4 s back
    assert snap(window, words, limit_s=at(words, "z").end_s).end_s == at(words, "d").end_s
    kept = snap(window, words, limit_s=at(words, "z").end_s, min_len_s=at(words, "d").end_s + 0.1)
    assert kept.end_s == window.end_s


def test_sentence_end_is_preferred_to_a_nearer_bare_pause() -> None:
    words = speech("It went up <0.5> and then came down. Anyway what else is there")
    window = ClipWindow(0.0, at(words, "went").end_s)  # cuts after "went"
    assert snap(window, words, limit_s=10.0).end_s == at(words, "down.").end_s
    unpunctuated = speech("It went up <0.5> and then came down Anyway what else is there")
    assert (
        snap(window, unpunctuated, limit_s=at(unpunctuated, "there").end_s).end_s
        == at(unpunctuated, "up").end_s
    )


# ── Both ends, switches and invariants ───────────────────────


def test_rule_switched_off_or_no_words_changes_nothing() -> None:
    window = ClipWindow(at(TALK, "five").start_s, 30.0)
    assert snap(window, TALK, SnapRules()) == window
    assert snap(window, []) == window
    assert not SnapRules().enabled and RULES.enabled


def test_rules_from_the_setting() -> None:
    raw = {"pause_s": 0.4, "start_back_s": 4, "start_forward_s": 2.0, "end_forward_s": 3.0, "vad": True}
    rules = SnapRules.from_setting(raw)
    assert rules == SnapRules(0.4, 4.0, 2.0, 3.0, 0.0, True)
    # A missing setting (database before migration 0011) or a malformed one switches the rule off.
    assert SnapRules.from_setting(None) == SnapRules()
    assert SnapRules.from_setting({"pause_s": "0.4", "vad": 1}) == SnapRules()
    assert SnapRules.from_setting({"pause_s": True, "start_back_s": -3}) == SnapRules()


def test_limits_hold_for_random_transcripts() -> None:
    rng = random.Random(11)
    for _ in range(400):
        tokens: list[str] = []
        for index in range(rng.randint(5, 120)):
            if rng.random() < 0.15:
                tokens.append(f"<{rng.choice([0.2, 0.4, 0.7, 1.5])}>")
            tokens.append(f"w{index}" + ("." if rng.random() < 0.1 else ""))
        words = speech(" ".join(tokens), word_s=rng.choice([0.2, 0.3, 0.45]))
        limit_s = words[-1].end_s + rng.choice([0.0, 0.3, 2.0])
        min_len, max_len = 5.0, 20.0
        start = rng.uniform(0.0, max(0.0, limit_s - min_len))
        end = min(limit_s, start + rng.uniform(min_len, max_len))
        if end - start < min_len:
            continue
        floor = start + rng.choice([0.0, 1.0, 5.0])
        window = ClipWindow(start, end)
        moved = snap_to_pauses(
            window, words, RULES, floor_s=floor, min_len_s=min_len, max_len_s=max_len, limit_s=limit_s
        )
        assert moved.start_s <= floor + 1e-6
        assert moved.end_s <= limit_s + 1e-6
        assert min_len - 1e-6 <= moved.duration_s <= max_len + 1e-6
        assert window.start_s - RULES.start_back_s - 1e-6 <= moved.start_s
        assert moved.start_s <= window.start_s + RULES.start_forward_s + 1e-6
        assert window.end_s - RULES.end_back_s - 1e-6 <= moved.end_s
        assert moved.end_s <= window.end_s + RULES.end_forward_s + 1e-6


# ── Words stretched across a silence ─────────────────────────


@pytest.mark.parametrize(
    ("word", "silence", "expected"),
    [
        # The silence covers the start of the word: the word begins when speech resumes.
        ((4.8, 6.7), (4.5, 6.5), (6.5, 6.7)),
        # The silence covers the end of the word.
        ((1.5, 3.5), (1.8, 3.6), (1.5, 1.8)),
        # The silence is inside the word: it stays on its longer spoken side.
        ((1.5, 3.5), (1.7, 3.2), (3.2, 3.5)),
        ((1.5, 3.5), (1.9, 3.3), (1.5, 1.9)),
        # Equal sides: the later one (Whisper tends to start the word after a pause too early).
        ((1.0, 3.0), (1.2, 2.8), (2.8, 3.0)),
        # No overlap, and a word wholly inside a silence (speech the detector missed), stay as they are.
        ((1.0, 1.3), (1.3, 2.0), (1.0, 1.3)),
        ((1.0, 1.3), (0.5, 2.0), (1.0, 1.3)),
        # Tightening would leave less than the minimum word length: unchanged.
        ((1.0, 3.0), (1.02, 2.99), (1.0, 3.0)),
    ],
)
def test_tighten_words(
    word: tuple[float, float], silence: tuple[float, float], expected: tuple[float, float]
) -> None:
    tight = tighten_words([Word("w", *word)], [Silence(*silence)], min_word_s=0.05)
    assert (tight[0].start_s, tight[0].end_s) == pytest.approx(expected)
    assert tight[0].text == "w"


def test_tightened_words_show_the_pause_as_a_gap() -> None:
    # Whisper output without punctuation: no gap anywhere, the pause hides inside "day" and "I".
    words = [
        Word("another", 0.0, 0.4),
        Word("day", 0.4, 2.4),
        Word("I", 2.4, 4.4),
        Word("love", 4.4, 4.8),
        Word("running", 4.8, 5.3),
    ]
    assert find_breaks(words, 0.4) == []
    tight = tighten_words(words, [Silence(0.7, 2.3), Silence(2.6, 4.35)], min_word_s=0.05)
    assert [(word.start_s, word.end_s) for word in tight[1:3]] == [(0.4, 0.7), (2.4, 2.6)]
    assert [item.index for item in find_breaks(tight, 0.4)] == [2, 3]


def test_silences_between_speech_spans() -> None:
    found = transcripts.silences_from_speech([(0.5, 2.0), (2.2, 5.0), (6.0, 8.0)], 9.0, 0.4)
    assert found == [Silence(0.0, 0.5), Silence(5.0, 6.0), Silence(8.0, 9.0)]
    assert transcripts.silences_from_speech([], 3.0, 0.4) == [Silence(0.0, 3.0)]
    assert transcripts.silences_from_speech([(0.0, 3.0)], 3.0, 0.4) == []


def test_pause_detection_failure_means_no_silences(tmp_path: Any) -> None:
    missing = tmp_path / "nothing.wav"
    assert transcripts.speech_silences(missing, 0.4) == []
    assert transcripts.speech_silences(missing, 0.0) == []


# ── Wiring: writer, transcript, config ───────────────────────


def test_final_window_snaps_the_writers_cut_to_a_pause() -> None:
    from sixstars.ai.schemas import ClipWriting
    from sixstars.factory import writer
    from sixstars.factory.config import FactoryConfig
    from sixstars.factory.lines import group_lines

    spoken = speech(
        "<2.0> we only have half the map to go <0.6> why am I doing this to myself this is torture "
        "I need a vehicle right now <0.8> hello yes I need a ride please"
    )
    reply = ClipWriting.model_validate(
        {
            "start_s": at(spoken, "map").start_s,  # mid-sentence: "the map to go"
            "end_s": at(spoken, "now").start_s,  # cuts "right now" in two
            "hook": "He just wants a ride",
            "title": "t",
            "caption": "c",
            "hashtags": ["a", "b", "c"],
            "topic": "runner-wants-ride",
            "spoiler": False,
            "safety": {},
            "score_parts": {"hook": 5, "clarity": 5, "payoff": 5, "rewatch": 5},
            "score": 5,
            "reject": False,
        }
    )
    base: dict[str, Any] = {
        "creator": "c",
        "platform": "youtube",
        "stream_title": "s",
        "lines": group_lines(spoken),
        "window_s": 20.0,
        "proposed": ClipWindow(2.0, 12.0),
        "spike_floor_s": 6.0,
        "min_len_s": 2.0,
        "max_len_s": 30.0,
    }
    plain = writer.final_window(reply, writer.WriterInput(**base))
    assert plain.start_s == pytest.approx(at(spoken, "map").start_s)  # no rule: the ragged cut stays
    cfg = FactoryConfig.offline("hourly")
    assert cfg.snap == SnapRules(0.4, 4.0, 2.0, 3.0, 2.0, True, 0.02)
    snapped = writer.final_window(reply, writer.WriterInput(**base, snap=cfg.snap))
    # Opens on "we only have half the map to go" and closes after "right now", with the usual lead and tail.
    assert snapped.start_s == pytest.approx(at(spoken, "we").start_s - writer.CUT_LEAD_S)
    assert snapped.end_s == pytest.approx(at(spoken, "now").end_s + writer.CUT_TAIL_S)


def test_config_without_the_setting_switches_the_rule_off() -> None:
    from sixstars.factory.config import OFFLINE_DEFAULTS, FactoryConfig

    values = {key: value for key, value in OFFLINE_DEFAULTS.items() if key != "factory.cut_snap"}
    assert FactoryConfig.from_mapping(values, "hourly").snap == SnapRules()


def test_transcribe_window_tightens_words_only_when_asked(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sixstars.clients.fakes import FakeTranscriber
    from sixstars.clients.transcribe import Transcript
    from sixstars.clients.transcribe import Word as ClientWord

    def fake_extract(source: Any, window: ClipWindow, destination: Any, ffmpeg: str = "ffmpeg") -> Any:
        destination.write_bytes(b"")
        return destination

    asked: list[float] = []

    def fake_silences(wav: Any, min_silence_s: float) -> list[Silence]:
        asked.append(min_silence_s)
        return [Silence(0.6, 2.9)]

    monkeypatch.setattr(transcripts, "extract_wav", fake_extract)
    monkeypatch.setattr(transcripts, "speech_silences", fake_silences)
    stretched = Transcript(language="en", words=(ClientWord("day", 0.2, 3.0), ClientWord("I", 3.0, 3.2)))

    def run(pause_s: float) -> list[tuple[float, float]]:
        result = transcripts.transcribe_window(
            tmp_path / "source.mp4",
            ClipWindow(0.0, 10.0),
            modes=("local",),
            scratch_dir=tmp_path,
            transcriber=FakeTranscriber(stretched),
            pause_s=pause_s,
        )
        return [(word.start_s, word.end_s) for word in result.words]

    assert run(0.0) == [(0.2, 3.0), (3.0, 3.2)] and asked == []
    assert run(0.4) == [(0.2, 0.6), (3.0, 3.2)] and asked == [0.4]


# ── Sentence ends for a transcript without punctuation ───────


def test_mark_sentence_ends_writes_full_stops_like_a_punctuated_transcript() -> None:
    from sixstars.factory.pauses import mark_sentence_ends, sentence_end_share

    words = speech("its still a crime so clearly my suspect here yeah exactly. he said 'no'")
    assert sentence_end_share(words) == pytest.approx(1 / 14)
    marked = mark_sentence_ends(words, [3, 8, 10, 40])
    # An end that is already marked and a number outside the transcript change nothing.
    assert " ".join(word.text for word in marked) == (
        "its still a crime. so clearly my suspect here. yeah exactly. he said 'no'"
    )
    assert [(word.start_s, word.end_s) for word in marked] == [(word.start_s, word.end_s) for word in words]
    assert sentence_end_share([]) == 0.0
    # The restored ends are sentence boundaries for the snap rule.
    assert [item.kind for item in find_breaks(marked, 0.4)] == ["sentence"] * 3


def test_snap_uses_restored_sentence_ends_where_nobody_pauses() -> None:
    from sixstars.factory.pauses import mark_sentence_ends

    talk = speech("it can either be assault or attempted murder by the way its still a crime so clearly my")
    cut = ClipWindow(at(talk, "attempted").start_s - 0.3, at(talk, "crime").start_s)  # "or attempted murder"
    assert snap(cut, talk, limit_s=talk[-1].end_s) == cut  # no pause, no punctuation: nothing to snap to
    marked = mark_sentence_ends(talk, [7, 14])
    moved = snap(cut, marked, floor_s=10.0, limit_s=talk[-1].end_s)
    assert moved.start_s == at(talk, "by").start_s
    assert moved.end_s == at(talk, "crime").end_s


def punctuation_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, spoken: str, client: Any, **options: Any
) -> list[str]:
    from sixstars.clients.fakes import FakeTranscriber
    from sixstars.clients.transcribe import Transcript
    from sixstars.clients.transcribe import Word as ClientWord

    def fake_extract(source: Any, window: ClipWindow, destination: Any, ffmpeg: str = "ffmpeg") -> Any:
        destination.write_bytes(b"")
        return destination

    monkeypatch.setattr(transcripts, "extract_wav", fake_extract)
    heard = tuple(ClientWord(word.text, word.start_s, word.end_s) for word in speech(spoken))
    result = transcripts.transcribe_window(
        tmp_path / "source.mp4",
        ClipWindow(0.0, 30.0),
        modes=("local",),
        scratch_dir=tmp_path,
        transcriber=FakeTranscriber(Transcript(language="en", words=heard)),
        ai_client=client,
        ref_id="clip-9",
        **options,
    )
    return [word.text for word in result.words]


def test_transcript_without_punctuation_gets_its_sentence_ends_from_p10(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sixstars import settings
    from sixstars.clients.anthropic_client import InvalidModelOutput
    from sixstars.clients.fakes import FakeAnthropicClient

    monkeypatch.setattr(
        settings, "_all", lambda: {"ai.model_fast": "claude-haiku-5-5", "ai.daily_budget_usd": 25}
    )
    monkeypatch.setattr(transcripts.calls.anthropic_client, "budget_ok", lambda kind="vod": True)
    monkeypatch.setattr(transcripts.calls, "system_values", lambda: {"game": "GTA V", "watermark": "@x"})
    spoken = "no way he did that look at him go"
    fake = FakeAnthropicClient({"p10_sentence_ends": {"ends": [1, 4]}})
    words = punctuation_run(tmp_path, monkeypatch, spoken, fake, punctuate_below=0.02)
    assert words == ["no", "way.", "he", "did", "that.", "look", "at", "him", "go"]
    call = fake.calls[0][1]
    assert call["model"] == "claude-haiku-5-5" and call["ref_id"] == "clip-9"
    assert "9 words, numbered 0 to 8" in call["user_content"] and "0:no 1:way 2:he" in call["user_content"]

    # Switched off, or a transcript that already has punctuation: the model is not asked.
    fake = FakeAnthropicClient({"p10_sentence_ends": {"ends": [1]}})
    assert punctuation_run(tmp_path, monkeypatch, spoken, fake) == spoken.split()
    punctuated = "no way. he did that. look at him go"
    assert (
        punctuation_run(tmp_path, monkeypatch, punctuated, fake, punctuate_below=0.02) == punctuated.split()
    )
    assert fake.calls == []

    # The call failing never costs the clip: the words are used as they are.
    failing = FakeAnthropicClient({"p10_sentence_ends": InvalidModelOutput("bad")})
    assert punctuation_run(tmp_path, monkeypatch, spoken, failing, punctuate_below=0.02) == spoken.split()

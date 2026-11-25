"""M20 L3: locating a clip in its re-downloaded footage and choosing the full moment's cut."""

from sixstars.factory.lines import Word
from sixstars.longform.align import CutRules, align, choose_cut, locate_clip, pauses, stored_words, words_in

RULES = CutRules()
SENTENCE = "no way he actually jumped the bridge with the car what a legend".split()  # noqa: SIM905


def speech(offset: float, words: list[str], step: float = 0.4, gap_every: int = 0) -> list[Word]:
    out, t = [], offset
    for i, w in enumerate(words):
        out.append(Word(w, round(t, 3), round(t + step * 0.8, 3)))
        t += step + (1.0 if gap_every and (i + 1) % gap_every == 0 else 0.0)
    return out


def test_locate_finds_a_drifted_live_clip() -> None:
    clip = speech(0.0, SENTENCE)
    # the file has 6 s of other talk, then the clip 4 s later than the nominal 5 s (live drift)
    file_words = speech(0.0, ["hello", "chat", "welcome", "back"]) + speech(9.0, SENTENCE)
    start, matched = locate_clip(file_words, clip, nominal_s=5.0, rules=RULES)
    assert abs(start - 9.0) < 0.3 and matched >= RULES.min_matches


def test_locate_falls_back_to_nominal_without_speech() -> None:
    start, matched = locate_clip([], speech(0.0, SENTENCE), nominal_s=5.0, rules=RULES)
    assert (start, matched) == (5.0, 0)


def test_pauses_are_the_middle_of_gaps() -> None:
    words = [Word("a", 0.0, 0.5), Word("b", 1.5, 2.0), Word("c", 2.1, 2.4)]
    assert pauses(words, 0.4) == [1.0]


def test_cut_widens_mostly_before_and_snaps_to_pauses() -> None:
    words = speech(0.0, SENTENCE * 6, gap_every=5)
    start, end = choose_cut(
        words,
        clip_start_s=20.0,
        clip_len_s=15.0,
        target_s=30.0,
        file_len_s=60.0,
        rules=RULES,
        min_s=15.0,
        max_s=45.0,
    )
    assert start < 20.0 and end > 35.0
    assert 15.0 <= end - start <= 45.0
    gaps = pauses(words, RULES.pause_s)
    assert any(abs(start - g) < 1e-6 for g in gaps) or start == 20.0 - 15.0 * RULES.lead_share


def test_cut_is_clamped_to_the_file_and_max() -> None:
    start, end = choose_cut(
        [],
        clip_start_s=1.0,
        clip_len_s=60.0,
        target_s=80.0,
        file_len_s=50.0,
        rules=RULES,
        min_s=15.0,
        max_s=45.0,
    )
    assert start == 0.0 and end - start <= 45.0


def test_align_returns_the_cut_and_match_count() -> None:
    file_words = speech(0.0, ["intro"] * 3) + speech(8.0, SENTENCE)
    cut = align(
        file_words,
        speech(0.0, SENTENCE),
        nominal_s=5.0,
        clip_len_s=5.0,
        target_s=20.0,
        file_len_s=30.0,
        rules=RULES,
        min_s=15.0,
        max_s=45.0,
    )
    assert cut.matched >= 3 and 15.0 <= cut.duration_s <= 45.0
    assert cut.start_s <= cut.clip_start_s


def test_words_in_shifts_to_segment_time() -> None:
    got = words_in([Word("a", 9.0, 9.5), Word("b", 10.2, 10.6), Word("c", 30.0, 30.5)], 10.0, 20.0)
    assert [(w.text, w.start_s) for w in got] == [("b", 0.2)]


def test_stored_words_reads_the_clip_transcript() -> None:
    assert stored_words({"words": [{"w": "hi", "s": 0.1, "e": 0.3}, {"bad": 1}]}) == [Word("hi", 0.1, 0.3)]
    assert stored_words(None) == []

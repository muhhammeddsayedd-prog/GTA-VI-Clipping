"""Pure time maths of the VOD lane: clipped ranges of a live session, and the caption track as a
transcript."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sixstars.vod import captions, ranges
from sixstars.vod.captions import Word
from sixstars.vod.ranges import Range
from sixstars.vod.ytdlp import Access, DownloadBlockedError

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "vod"
STARTED = datetime(2026, 11, 17, 18, 0, 0, tzinfo=UTC)

# ── clipped ranges ───────────────────────────────────────────


def test_wall_clock_ranges_become_offsets_from_the_stream_start() -> None:
    clipped = [
        {"start_wall": "2026-11-17T19:30:10Z", "end_wall": "2026-11-17T19:30:55Z", "clip_id": "b"},
        {"start_wall": "2026-11-17T18:05:00+00:00", "end_wall": "2026-11-17T18:05:40+00:00", "clip_id": "a"},
    ]
    assert ranges.clipped_offsets(clipped, STARTED) == [Range(300.0, 340.0), Range(5410.0, 5455.0)]


def test_other_time_zones_and_naive_times_are_read_as_the_same_instant() -> None:
    clipped = [{"start_wall": "2026-11-17T20:00:00+02:00", "end_wall": "2026-11-17T18:01:00"}]
    assert ranges.clipped_offsets(clipped, STARTED) == [Range(0.0, 60.0)]


def test_unreadable_ranges_are_left_out() -> None:
    clipped = [
        {"start_wall": "not a time", "end_wall": "2026-11-17T18:05:40Z"},
        {"start_wall": "2026-11-17T18:05:40Z"},
        {"start_wall": "2026-11-17T18:06:00Z", "end_wall": "2026-11-17T18:05:00Z"},
    ]
    assert ranges.clipped_offsets(clipped, STARTED) == []


def test_without_a_stream_start_nothing_can_be_converted() -> None:
    clipped = [{"start_wall": "2026-11-17T18:05:00Z", "end_wall": "2026-11-17T18:05:40Z"}]
    assert ranges.clipped_offsets(clipped, None) == []


@pytest.mark.parametrize(
    ("start_s", "end_s", "margin_s", "expected"),
    [
        (310.0, 330.0, 0.0, True),  # inside
        (250.0, 301.0, 0.0, True),  # touches the start
        (340.0, 400.0, 0.0, False),  # begins exactly where the range ends
        (369.0, 420.0, 30.0, True),  # within the margin after the range
        (370.0, 420.0, 30.0, False),  # just outside the margin
        (200.0, 270.0, 30.0, False),  # ends exactly at the margin before the range
        (200.0, 271.0, 30.0, True),
    ],
)
def test_overlap_with_margin(start_s: float, end_s: float, margin_s: float, expected: bool) -> None:
    assert ranges.overlaps(start_s, end_s, [Range(300.0, 340.0)], margin_s) is expected


def test_spikes_on_clipped_ranges_are_split_off() -> None:
    windows = [(100.0, 150.0), (320.0, 380.0), (5380.0, 5400.0), (9000.0, 9050.0)]
    kept, dropped = ranges.split_clipped(
        windows, [Range(300.0, 340.0), Range(5410.0, 5455.0)], 30.0, window=lambda item: item
    )
    assert kept == [(100.0, 150.0), (9000.0, 9050.0)]
    assert dropped == [(320.0, 380.0), (5380.0, 5400.0)]
    assert ranges.split_clipped(windows, [], 30.0, window=lambda item: item) == (windows, [])


# ── captions ─────────────────────────────────────────────────


def automatic_track() -> dict[str, object]:
    """The shape of a real automatic caption track (json3), trimmed."""
    return json.loads((FIXTURES / "captions_auto.en.json3").read_text(encoding="utf-8"))


def test_automatic_captions_give_a_time_per_word() -> None:
    words = captions.parse_json3(automatic_track())
    assert [word.text for word in words[:5]] == ["dude", "get", "my", "uh", "little"]
    assert words[0].start_s == pytest.approx(4417.44)
    assert words[1].start_s == pytest.approx(4418.4)  # event start + tOffsetMs
    assert words[0].end_s == pytest.approx(4418.4)  # a word ends where the next begins
    assert all(word.end_s - word.start_s <= captions.MAX_WORD_S for word in words)
    assert all(a.start_s <= b.start_s for a, b in zip(words, words[1:], strict=False))
    assert "\n" not in "".join(word.text for word in words)


def test_written_captions_spread_their_words_over_the_line() -> None:
    data = {"events": [{"tStartMs": 10000, "dDurationMs": 4000, "segs": [{"utf8": "no way he landed"}]}]}
    words = captions.parse_json3(data)
    assert [(word.text, word.start_s) for word in words] == [
        ("no", 10.0),
        ("way", 11.0),
        ("he", 12.0),
        ("landed", 13.0),
    ]


def test_events_without_words_or_times_are_ignored() -> None:
    data = {"events": [{"tStartMs": 0, "dDurationMs": 5}, {"segs": [{"utf8": "x"}]}, "junk"]}
    assert captions.parse_json3(data) == []
    assert captions.parse_json3({}) == []


def test_chunks_are_blocks_on_the_video_clock() -> None:
    words = [Word("a", 1.0, 1.5), Word("b", 29.9, 30.5), Word("c", 30.0, 30.4), Word("d", 95.0, 95.5)]
    chunks = captions.chunk_words(words, 30.0)
    assert [(chunk.start_s, chunk.end_s, chunk.text) for chunk in chunks] == [
        (0.0, 30.0, "a b"),
        (30.0, 60.0, "c"),
        (90.0, 120.0, "d"),
    ]
    with pytest.raises(ValueError):
        captions.chunk_words(words, 0)


def test_text_between_cuts_at_a_word() -> None:
    words = [
        Word(text, float(index), float(index) + 0.5) for index, text in enumerate(["a", "bb", "ccc", "dddd"])
    ]
    assert captions.text_between(words, 1.0, 3.0) == "bb ccc"
    assert captions.text_between(words, 0.0, 10.0, max_chars=8) == "a bb …"
    assert captions.text_between(words, 50.0, 60.0) == ""


def test_arguments_ask_for_captions_only(tmp_path: Path) -> None:
    arguments = captions.build_arguments("https://youtu.be/x", tmp_path, "captions", ["en", "en-orig"])
    assert "--skip-download" in arguments and "--write-auto-subs" in arguments and "--write-subs" in arguments
    assert arguments[arguments.index("--sub-langs") + 1] == "en,en-orig"
    assert arguments[arguments.index("--sub-format") + 1] == "json3"


def test_fetch_reads_the_first_language_that_has_words(tmp_path: Path) -> None:
    def runner(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        (tmp_path / "captions.en-orig.json3").write_text(json.dumps(automatic_track()), encoding="utf-8")
        (tmp_path / "captions.en.json3").write_text("{}", encoding="utf-8")
        return 0, "", ""

    track = captions.fetch(
        "https://youtu.be/x", tmp_path, access=Access(), langs=["en", "en-orig"], timeout_s=30, runner=runner
    )
    assert track is not None and track.language == "en" and track.source == "captions"
    assert track.words[0].text == "dude" and track.end_s > 4417


def test_the_track_in_the_spoken_language_is_found_by_pattern(tmp_path: Path) -> None:
    def runner(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        (tmp_path / "captions.hi-orig.json3").write_text(json.dumps(automatic_track()), encoding="utf-8")
        # The machine-translated English track is refused; yt-dlp then ends with an error.
        return 1, "", "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"

    track = captions.fetch(
        "u", tmp_path, access=Access(), langs=[".*-orig", "en"], timeout_s=30, runner=runner
    )
    assert track is not None and track.language == "hi" and len(track.words) > 50


def test_a_video_without_captions_gives_none(tmp_path: Path) -> None:
    def runner(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        return 0, "", "WARNING: There are no subtitles for the requested languages"

    assert captions.fetch("u", tmp_path, access=Access(), langs=["en"], timeout_s=30, runner=runner) is None
    assert captions.fetch("u", tmp_path, access=Access(), langs=[], timeout_s=30, runner=runner) is None


def test_a_rate_limited_caption_file_is_not_a_block(tmp_path: Path) -> None:
    def runner(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        return 1, "", "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"

    assert captions.fetch("u", tmp_path, access=Access(), langs=["en"], timeout_s=30, runner=runner) is None


def test_the_bot_check_on_the_video_is_a_block(tmp_path: Path) -> None:
    def runner(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        return 1, "", "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies"

    with pytest.raises(DownloadBlockedError):
        captions.fetch("u", tmp_path, access=Access(), langs=["en"], timeout_s=30, runner=runner)

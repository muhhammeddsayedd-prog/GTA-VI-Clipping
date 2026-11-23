"""Transcript handling (language, translation spread, coarse timings, mode fallback) and the clip writer
glue (context clamping, trim, cut points, keyword times, clip columns). No network, no ffmpeg."""

import wave
from pathlib import Path

import numpy as np
import pytest

from sixstars.ai.schemas import ClipWriting, TranslatedSegment
from sixstars.clients.fakes import FakeAnthropicClient, FakeTranscriber
from sixstars.clients.transcribe import Transcript
from sixstars.clients.transcribe import Word as ClientWord
from sixstars.factory import transcript as transcripts
from sixstars.factory import writer
from sixstars.factory.bleep import ProfanityMatcher
from sixstars.factory.lines import Line, Word, clip_lines, group_lines
from sixstars.factory.window import ClipWindow


def words(*items: tuple[str, float, float]) -> list[Word]:
    return [Word(text, start, end) for text, start, end in items]


# ── transcript ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("language", "code", "english"),
    [
        ("en", "en", True),
        ("English", "en", True),
        (None, None, True),
        ("", None, True),
        ("spanish", "es", False),
        ("ES", "es", False),
        ("klingon", "klingon", False),
    ],
)
def test_language_code(language: str | None, code: str | None, english: bool) -> None:
    assert transcripts.language_code(language) == code
    assert transcripts.is_english(language) is english


def test_words_from_client_sorts_clamps_and_drops() -> None:
    client = Transcript(
        language="en",
        words=(
            ClientWord("  way ", 1.0, 1.4),
            ClientWord("no", 0.2, 0.5),
            ClientWord("", 2.0, 2.2),
            ClientWord("late", 9.9, 12.0),
            ClientWord("gone", 10.5, 11.0),
            ClientWord("odd", 3.0, 2.5),
        ),
    )
    assert transcripts.words_from_client(client, 10.0) == words(
        ("no", 0.2, 0.5), ("way", 1.0, 1.4), ("odd", 3.0, 3.0), ("late", 9.9, 10.0)
    )


def test_tidy_word_times_spreads_shared_starts_and_removes_overlaps() -> None:
    coarse = words(("I'm", 1.83, 1.93), ("going", 1.83, 1.93), ("here", 1.93, 2.4), ("Yeah", 2.27, 2.5))
    tidy = transcripts.tidy_word_times(coarse)
    assert [word.text for word in tidy] == ["I'm", "going", "here", "Yeah"]
    assert tidy[0].start_s == pytest.approx(1.83) and tidy[1].start_s == pytest.approx(1.88)
    assert tidy[1].end_s <= tidy[2].start_s + 1e-9
    assert tidy[2].end_s == pytest.approx(2.27), "a word must not run into the next one"
    starts = [word.start_s for word in tidy]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


def test_tidy_word_times_caps_the_step_of_a_shared_start() -> None:
    tidy = transcripts.tidy_word_times(
        words(("crash", 16.17, 16.5), ("into", 16.17, 16.6), ("what", 20.0, 20.3))
    )
    assert tidy[1].start_s - tidy[0].start_s == pytest.approx(transcripts.SHARED_START_STEP_S)


def test_segments_break_on_pause_punctuation_and_length() -> None:
    spoken = words(
        ("Hola", 0.0, 0.3),
        ("amigo.", 0.3, 0.7),
        ("Que", 0.8, 1.0),
        ("pasa", 1.0, 1.3),
        ("aqui", 2.5, 2.9),
    )
    segments = transcripts.segments_from_words(spoken)
    assert [(s.start, s.end, s.text) for s in segments] == [
        (0.0, 0.7, "Hola amigo."),
        (0.8, 1.3, "Que pasa"),
        (2.5, 2.9, "aqui"),
    ]
    long = [Word(f"w{i}", i * 0.2, i * 0.2 + 0.15) for i in range(40)]
    assert all(
        len(s.text.split()) <= transcripts.SEGMENT_MAX_WORDS for s in transcripts.segments_from_words(long)
    )


def test_spread_words_covers_the_span_in_order() -> None:
    spread = transcripts.spread_words(
        [
            TranslatedSegment(start=2.0, end=4.0, en="no way he actually did"),
            TranslatedSegment(start=5.0, end=5.0, en="ignored empty span"),
            TranslatedSegment(start=6.0, end=7.0, en="  "),
        ]
    )
    assert [word.text for word in spread] == ["no", "way", "he", "actually", "did"]
    assert spread[0].start_s == pytest.approx(2.0) and spread[-1].end_s == pytest.approx(4.0)
    for earlier, later in zip(spread, spread[1:], strict=False):
        assert earlier.end_s == pytest.approx(later.start_s)
    durations = {word.text: word.end_s - word.start_s for word in spread}
    assert durations["actually"] > durations["no"], "longer words stay on screen longer"


def test_stored_transcript_format() -> None:
    assert transcripts.stored_transcript(words(("no", 0.12345, 0.5), ("way", 0.5, 0.80001))) == {
        "language": "en",
        "words": [{"w": "no", "s": 0.123, "e": 0.5}, {"w": "way", "s": 0.5, "e": 0.8}],
    }


def test_read_wav_samples(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(np.array([0, 16384, -32768], dtype=np.int16).tobytes())
    samples = transcripts.read_wav_samples(path)
    assert samples.dtype == np.float32 and samples.tolist() == [0.0, 0.5, -1.0]
    stereo = tmp_path / "b.wav"
    with wave.open(str(stereo), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00" * 8)
    with pytest.raises(ValueError):
        transcripts.read_wav_samples(stereo)


def test_samples_model_passes_an_array_instead_of_a_path(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(np.zeros(160, dtype=np.int16).tobytes())
    seen: list[object] = []

    class Model:
        def transcribe(self, audio: object, **options: object) -> str:
            seen.append((type(audio).__name__, options))
            return "ok"

    wrapped = transcripts._SamplesModel(Model())
    assert wrapped.transcribe(str(path), word_timestamps=True) == "ok"
    assert seen == [("ndarray", {"word_timestamps": True})]


@pytest.fixture
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """`transcribe_window` without ffmpeg: the WAV step writes an empty file."""

    def fake_extract(source: Path, window: ClipWindow, destination: Path, ffmpeg: str = "ffmpeg") -> Path:
        destination.write_bytes(b"")
        return destination

    monkeypatch.setattr(transcripts, "extract_wav", fake_extract)


class OneModeTranscriber(FakeTranscriber):
    """Fails in the listed modes; reports no hosted provider when `hosted` is False."""

    def __init__(self, transcript: Transcript, fail: tuple[str, ...] = (), hosted: bool = True) -> None:
        super().__init__(transcript)
        self.fail = fail
        self.hosted = hosted

    def configured_providers(self) -> set[str]:
        return {"openai"} if self.hosted else set()

    def transcribe(self, audio_path: Path, mode: str = "local", language: str | None = None) -> Transcript:  # type: ignore[override]
        self._record("transcribe", audio_path=audio_path, mode=mode, language=language)
        if mode in self.fail:
            raise RuntimeError(f"{mode} is down")
        return self.transcript


ENGLISH = Transcript(language="english", words=(ClientWord("no", 0.2, 0.5), ClientWord("way", 0.5, 0.9)))


def test_transcribe_window_uses_the_first_mode_that_works(
    no_ffmpeg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcripts.transcribe_client, "budget_ok", lambda: True)
    transcriber = OneModeTranscriber(ENGLISH, fail=("hosted",))
    result = transcripts.transcribe_window(
        tmp_path / "source.mp4",
        ClipWindow(5.0, 25.0),
        modes=("hosted", "local"),
        scratch_dir=tmp_path,
        transcriber=transcriber,
    )
    assert [call["mode"] for call in transcriber.calls_to("transcribe")] == ["hosted", "local"]
    assert result.language == "en" and result.original_language == "en" and not result.translated
    assert [word.text for word in result.words] == ["no", "way"]


def test_transcribe_window_skips_hosted_without_key_or_budget(
    no_ffmpeg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcriber = OneModeTranscriber(ENGLISH, hosted=False)
    transcripts.transcribe_window(
        tmp_path / "s.mp4",
        ClipWindow(0, 10),
        modes=("hosted", "local"),
        scratch_dir=tmp_path,
        transcriber=transcriber,
    )
    assert [call["mode"] for call in transcriber.calls_to("transcribe")] == ["local"]

    monkeypatch.setattr(transcripts.transcribe_client, "budget_ok", lambda: False)
    with_key = OneModeTranscriber(ENGLISH)
    transcripts.transcribe_window(
        tmp_path / "s.mp4",
        ClipWindow(0, 10),
        modes=("hosted", "local"),
        scratch_dir=tmp_path,
        transcriber=with_key,
    )
    assert [call["mode"] for call in with_key.calls_to("transcribe")] == ["local"]


def test_transcribe_window_without_a_database_never_uses_the_paid_mode(
    no_ffmpeg: None, tmp_path: Path
) -> None:
    # budget_ok() needs the settings table; with no DATABASE_URL it raises and hosted is skipped
    transcriber = OneModeTranscriber(ENGLISH)
    transcripts.transcribe_window(
        tmp_path / "s.mp4",
        ClipWindow(0, 10),
        modes=("hosted", "local"),
        scratch_dir=tmp_path,
        transcriber=transcriber,
    )
    assert [call["mode"] for call in transcriber.calls_to("transcribe")] == ["local"]


def test_transcribe_window_raises_when_every_mode_fails(no_ffmpeg: None, tmp_path: Path) -> None:
    transcriber = OneModeTranscriber(ENGLISH, fail=("local",), hosted=False)
    with pytest.raises(transcripts.TranscriptionFailedError):
        transcripts.transcribe_window(
            tmp_path / "s.mp4",
            ClipWindow(0, 10),
            modes=("local", "hosted"),
            scratch_dir=tmp_path,
            transcriber=transcriber,
        )


def test_transcribe_window_translates_other_languages(
    no_ffmpeg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spanish = Transcript(
        language="es",
        words=(
            ClientWord("No", 1.0, 1.2),
            ClientWord("puede", 1.2, 1.6),
            ClientWord("ser.", 1.6, 2.0),
            ClientWord("Mira", 3.0, 3.4),
            ClientWord("eso", 3.4, 3.8),
        ),
    )
    monkeypatch.setattr(transcripts.calls, "_guard", lambda prompt_id, lane: None)
    monkeypatch.setattr(transcripts.calls, "model_for", lambda prompt_id: "fake-model")
    monkeypatch.setattr(transcripts.calls, "_system_values", lambda: {"game": "GTA", "watermark": "@x"})
    client = FakeAnthropicClient(
        {
            "p8_translate_lines": {
                "segments": [
                    {"start": 0.0, "end": 0.0, "en": "No way."},
                    {"start": 0.0, "end": 0.0, "en": "Look at that"},
                ]
            }
        }
    )
    result = transcripts.transcribe_window(
        tmp_path / "s.mp4",
        ClipWindow(0, 10),
        modes=("local",),
        scratch_dir=tmp_path,
        transcriber=OneModeTranscriber(spanish),
        ai_client=client,
        ref_id="clip-1",
    )
    assert result.translated and result.language == "en" and result.original_language == "es"
    assert [word.text for word in result.words] == ["No", "way.", "Look", "at", "that"]
    # the source segment times drive the sync, whatever the model echoed
    assert result.words[0].start_s == pytest.approx(1.0) and result.words[1].end_s == pytest.approx(2.0)
    assert result.words[2].start_s == pytest.approx(3.0) and result.words[-1].end_s == pytest.approx(3.8)
    assert client.calls_to("complete_json")[0]["ref_id"] == "clip-1"


# ── writer ───────────────────────────────────────────────────


def make_lines() -> list[Line]:
    spoken = [
        ("He", 10.0, 10.2),
        ("did", 10.2, 10.4),
        ("NOT", 10.4, 10.9),
        ("expect", 11.0, 11.4),
        ("that!", 11.4, 11.9),
        ("No", 13.0, 13.2),
        ("way,", 13.2, 13.6),
        ("this", 20.0, 20.2),
        ("fucking", 20.2, 20.7),
        ("cop", 20.7, 21.0),
        ("crashed", 34.0, 34.5),
        ("again", 34.5, 35.0),
    ]
    return group_lines(words(*spoken))


def writer_input(**overrides: object) -> writer.WriterInput:
    values: dict[str, object] = {
        "creator": "speedy",
        "platform": "twitch",
        "stream_title": "GTA RP",
        "lines": make_lines(),
        "window_s": 60.0,
        "proposed": ClipWindow(12.0, 40.0),
        "spike_floor_s": 18.0,
        "min_len_s": 15.0,
        "max_len_s": 30.0,
    }
    values.update(overrides)
    return writer.WriterInput(**values)  # type: ignore[arg-type]


def writing(start_s: float, end_s: float, **overrides: object) -> ClipWriting:
    values: dict[str, object] = {
        "start_s": start_s,
        "end_s": end_s,
        "hook": "He did NOT expect that",
        "title": "A title",
        "caption": "A caption",
        "hashtags": ["gta", "rp", "fail"],
        "topic": "cop crashes again",
        "keywords": [{"line": 0, "words": ["NOT"]}, {"line": 2, "words": ["no way"]}],
        "spoiler": False,
        "safety": {},
        "score": 8,
        "score_parts": {"hook": 8, "clarity": 7, "payoff": 9, "rewatch": 8},
        "reject": False,
    }
    values.update(overrides)
    return ClipWriting.model_validate(values)


def test_build_context_passes_the_window_and_limits() -> None:
    ctx = writer.build_context(writer_input(clip_id="c1", profanity_hint=("fucking",)))
    assert (ctx.window_s, ctx.proposed_start_s, ctx.proposed_end_s, ctx.spike_floor_s) == (
        60.0,
        12.0,
        40.0,
        18.0,
    )
    assert (ctx.min_len_s, ctx.max_len_s, ctx.clip_id, ctx.profanity_hint) == (15.0, 30.0, "c1", ("fucking",))
    assert [line.index for line in ctx.lines] == list(range(len(ctx.lines)))


def test_build_context_keeps_the_rules_satisfiable() -> None:
    # a window file shorter than the minimum: the whole file is the clip
    short = writer.build_context(
        writer_input(window_s=9.0, proposed=ClipWindow(2.0, 20.0), spike_floor_s=8.0)
    )
    assert short.min_len_s == 9.0 and short.spike_floor_s == 0.0
    assert (short.proposed_start_s, short.proposed_end_s) == (0.0, 9.0)
    # a spike floor so late that no clip of minimum length fits after it is pulled back
    late = writer.build_context(writer_input(spike_floor_s=58.0))
    assert late.spike_floor_s == 45.0
    # a negative floor (spike before the file start) becomes zero
    assert writer.build_context(writer_input(spike_floor_s=-3.0)).spike_floor_s == 0.0


def test_profanity_hint_lists_distinct_words() -> None:
    matcher = ProfanityMatcher(frozenset({"fuck", "fucking", "shit"}))
    spoken = words(("Fucking", 0, 1), ("cop", 1, 2), ("fucking!", 2, 3), ("shit", 3, 4))
    assert writer.profanity_hint(spoken, matcher) == ["fucking", "shit"]


def test_snap_to_words_gives_room_but_stays_out_of_neighbours() -> None:
    spoken = words(("a", 9.0, 9.95), ("b", 10.0, 10.4), ("c", 14.0, 14.9), ("d", 15.0, 15.5))
    snapped = writer.snap_to_words(ClipWindow(10.0, 14.9), spoken, limit_s=60.0, max_len_s=30.0)
    assert snapped.start_s == pytest.approx(9.95), "the lead stops at the end of the previous word"
    assert snapped.end_s == pytest.approx(15.0), "the tail stops at the start of the next word"
    free = writer.snap_to_words(ClipWindow(10.0, 14.5), spoken[1:3], limit_s=60.0, max_len_s=30.0)
    assert free.start_s == pytest.approx(10.0 - writer.CUT_LEAD_S)
    assert free.end_s == pytest.approx(14.9 + writer.CUT_TAIL_S), "a word cut by the end is kept whole"


def test_snap_to_words_respects_file_end_max_length_and_empty_windows() -> None:
    spoken = words(("b", 10.0, 10.4), ("c", 14.0, 14.95))
    assert writer.snap_to_words(ClipWindow(10.0, 14.9), spoken, limit_s=15.0, max_len_s=30.0).end_s == 15.0
    capped = writer.snap_to_words(ClipWindow(10.0, 14.9), spoken, limit_s=60.0, max_len_s=5.0)
    assert capped.duration_s == pytest.approx(5.0)
    silent = ClipWindow(30.0, 40.0)
    assert writer.snap_to_words(silent, spoken, limit_s=60.0, max_len_s=30.0) == silent
    long_silence = writer.snap_to_words(ClipWindow(10.0, 20.0), spoken, limit_s=60.0, max_len_s=30.0)
    assert long_silence.end_s == 20.0, "silence the writer kept at the end is not cut"


def test_final_window_applies_the_trim_rules_then_moves_off_the_words() -> None:
    item = writer_input()
    # the writer starts after the spike floor and runs past the maximum length
    window = writer.final_window(writing(19.0, 59.0), item)
    assert window.start_s <= 18.0 and window.duration_s <= 30.0 + 1e-9
    # a normal trim: starts on "No" (13.0) with a lead, ends after "again" (35.0) with a tail
    window = writer.final_window(writing(13.0, 35.0), item)
    assert window.start_s == pytest.approx(13.0 - writer.CUT_LEAD_S)
    assert window.end_s == pytest.approx(35.0 + writer.CUT_TAIL_S)
    # too short: extended to the minimum length
    assert writer.final_window(writing(13.0, 16.0), item).duration_s >= 15.0


def test_keyword_times_one_pop_per_keyword_thinned() -> None:
    lines = clip_lines(make_lines(), ClipWindow(10.0, 40.0))
    keywords = {0: ["NOT"], 2: ["no way"], 99: ["missing"]}
    assert writer.keyword_times(lines, keywords) == (0.4, 3.0)
    # two keywords spoken 0.2 s apart: only the first pops
    close = [Line(0, tuple(words(("big", 1.0, 1.2), ("win", 1.2, 1.5))))]
    assert writer.keyword_times(close, {0: ["big", "win"]}) == (1.0,)
    assert writer.keyword_times(lines, {}) == ()


def test_outside_spans_drops_pops_under_a_bleep() -> None:
    from sixstars.factory.bleep import Span

    spans = [Span(14.21, 14.83), Span(20.0, 20.5)]
    assert writer.outside_spans((5.18, 14.26, 14.83, 17.32, 20.6), spans) == (5.18, 17.32, 20.6)
    assert writer.outside_spans((1.0,), []) == (1.0,)


def test_clip_columns_maps_every_p2_output() -> None:
    columns = writer.clip_columns(writing(13.0, 35.0, commentary=None, spoiler=True))
    assert columns["hook"] == "He did NOT expect that" and columns["title"] == "A title"
    assert columns["hashtags"] == ["gta", "rp", "fail"] and columns["topic"] == "cop-crashes-again"
    assert columns["spoiler"] is True and columns["score"] == 8 and columns["commentary"] is None
    assert columns["keywords"].obj == {"0": ["NOT"], "2": ["no way"]}  # type: ignore[attr-defined]
    assert columns["safety_flags"].obj["gambling"] is False  # type: ignore[attr-defined]
    assert columns["score_parts"].obj == {"hook": 8, "clarity": 7, "payoff": 9, "rewatch": 8}  # type: ignore[attr-defined]
    assert set(columns) == {
        "hook", "title", "caption", "hashtags", "commentary", "topic", "keywords", "spoiler", "safety_flags",
        "score", "score_parts", "moment_type", "style_texts",
    }  # fmt: skip
    assert columns["moment_type"] is None and columns["style_texts"].obj == {}  # type: ignore[attr-defined]

"""Builds the word-level transcript for a clip window from the transcription client's output. Implemented in
M06.

The padded window of the source file is cut to a 16 kHz mono WAV and transcribed: local faster-whisper for
normal renders, the hosted API for express renders, each falling back to the other. Speech in another
language is translated to English segment by segment (prompt P8) and the English words are spread over
each segment's time span. Every time this module returns is in seconds from the start of the window it was
given ("window time").

Whisper stretches a word across the silence next to it, so a pause between two phrases does not show as a
gap between two words. When asked to (`pause_s`), the window is also run through the voice activity
detector that ships with faster-whisper (Silero VAD, a small ONNX model, well under a second for a
90-second window) and each stretched word is pulled back onto the speech (`pauses.tighten_words`). The
subtitle lines then break at real pauses and the factory can snap the cut to one.

When Whisper returns English with (almost) no punctuation (`punctuate_below`), the fast model is asked
where the sentences end (prompt P10) and a full stop is written after those words, so the lines, the clip
writer and the cut all see sentences again. If that call cannot run the words are used as they are.
"""

import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from sixstars.ai import calls
from sixstars.ai.calls import Segment
from sixstars.ai.schemas import TranslatedSegment
from sixstars.clients import transcribe as transcribe_client
from sixstars.clients.anthropic_client import AnthropicClient, BudgetKind
from sixstars.clients.transcribe import Mode, Transcriber, Transcript
from sixstars.factory import tools
from sixstars.factory.lines import Word
from sixstars.factory.pauses import Silence, mark_sentence_ends, sentence_end_share, tighten_words
from sixstars.factory.window import ClipWindow
from sixstars.logging import get_logger

WAV_NAME = "window.wav"
# How transcript words are grouped into segments for translation (not settings: no key exists).
SEGMENT_PAUSE_S = 0.6
SEGMENT_MAX_S = 8.0
SEGMENT_MAX_WORDS = 18
# Word starts closer than this count as the same start; such words are spread out, this far apart at most.
MIN_WORD_STEP_S = 0.03
SHARED_START_STEP_S = 0.3
_SENTENCE_END = (".", "!", "?", "…")
# Voice activity detection for the pauses (detector tuning, not settings): speech is padded by this much
# so a soft word ending is not counted as silence, and a tightened word keeps at least this length.
VAD_SPEECH_PAD_MS = 30
VAD_MIN_SPEECH_MS = 100
MIN_TIGHT_WORD_S = 0.05
# The hosted Whisper API names the language in English; faster-whisper gives the ISO 639-1 code.
_LANGUAGE_CODES = {
    "english": "en",
    "spanish": "es",
    "portuguese": "pt",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "russian": "ru",
    "arabic": "ar",
    "turkish": "tr",
    "polish": "pl",
    "dutch": "nl",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh",
    "hindi": "hi",
    "indonesian": "id",
    "swedish": "sv",
}


class TranscriptionFailedError(RuntimeError):
    """No transcription mode produced a result."""


@dataclass(frozen=True, slots=True)
class WindowTranscript:
    """English words with timings for one window. `original_language` is what was spoken."""

    language: str
    original_language: str | None
    words: tuple[Word, ...]
    provider: str = ""
    model: str = ""
    translated: bool = False


def language_code(language: str | None) -> str | None:
    """`"english"` or `"EN"` to `"en"`; an unknown name is returned in lower case; None stays None."""
    if not language:
        return None
    name = language.strip().lower()
    return _LANGUAGE_CODES.get(name, name) or None


def is_english(language: str | None) -> bool:
    """True for English and for an unknown language (the transcript is then used as it is)."""
    code = language_code(language)
    return code is None or code == "en"


def extract_wav(source: Path, window: ClipWindow, destination: Path, ffmpeg: str = "ffmpeg") -> Path:
    """Cut `window` of the source file's audio to a 16 kHz mono WAV file."""
    return tools.extract_audio(
        source,
        destination,
        start_s=window.start_s,
        duration_s=window.duration_s,
        sample_rate=transcribe_client.AUDIO_SAMPLE_RATE,
        ffmpeg=ffmpeg,
    )


def read_wav_samples(path: Path) -> "np.ndarray[Any, np.dtype[np.float32]]":
    """The samples of a 16-bit mono WAV file as floats in [-1, 1]."""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
            raise ValueError(f"{path.name} is not 16-bit mono PCM")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class _SamplesModel:
    """A faster-whisper model that is given samples instead of a file path.

    faster-whisper 1.2.1 decodes files with PyAV and calls `av.open(..., metadata_errors=...)`, an argument
    PyAV 19 no longer has, so every path fails with a TypeError. Passing the samples skips that decoder.
    """

    def __init__(self, model: Any) -> None:
        self._model = model

    def transcribe(self, audio: Any, **options: Any) -> Any:
        if isinstance(audio, str | Path):
            audio = read_wav_samples(Path(audio))
        return self._model.transcribe(audio, **options)


def whisper_factory(name: str) -> Any:
    """Load a faster-whisper model on the CPU (int8), wrapped so it reads our WAV files itself."""
    from faster_whisper import WhisperModel

    model = WhisperModel(name, device="cpu", compute_type=transcribe_client.LOCAL_COMPUTE_TYPE)
    return _SamplesModel(model)


def default_transcriber(whisper_model: str | None = None) -> Transcriber:
    """The transcriber the factory uses: `vod.whisper_model` (or the given model) locally, hosted by
    the `transcribe.express_provider` setting."""
    return Transcriber(whisper_model=whisper_model, model_factory=whisper_factory)


def silences_from_speech(
    speech: Sequence[tuple[float, float]], duration_s: float, min_silence_s: float
) -> list[Silence]:
    """The stretches of at least `min_silence_s` between the given speech spans, from time zero to
    `duration_s`."""
    silences: list[Silence] = []
    cursor = 0.0
    for start, end in sorted(speech):
        if start - cursor >= min_silence_s:
            silences.append(Silence(cursor, start))
        cursor = max(cursor, end)
    if duration_s - cursor >= min_silence_s:
        silences.append(Silence(cursor, duration_s))
    return silences


def speech_silences(wav: Path, min_silence_s: float) -> list[Silence]:
    """Where nobody speaks in a 16 kHz mono WAV file, for at least `min_silence_s` (window time).

    Uses the Silero voice activity detector bundled with faster-whisper, which hears speech over game
    sound. Returns an empty list when the detector cannot run: the words are then used as they are.
    """
    if min_silence_s <= 0:
        return []
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        samples = read_wav_samples(wav)
        options = VadOptions(
            min_silence_duration_ms=int(min_silence_s * 1000),
            speech_pad_ms=VAD_SPEECH_PAD_MS,
            min_speech_duration_ms=VAD_MIN_SPEECH_MS,
        )
        rate = transcribe_client.AUDIO_SAMPLE_RATE
        speech = [
            (item["start"] / rate, item["end"] / rate) for item in get_speech_timestamps(samples, options)
        ]
    except Exception as exc:
        get_logger("factory").warning("pause_detection_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return []
    return silences_from_speech(speech, len(samples) / rate, min_silence_s)


def words_from_client(transcript: Transcript, window_s: float) -> list[Word]:
    """The client's words as factory words: sorted, inside the window, ends never before starts."""
    words: list[Word] = []
    for item in sorted(transcript.words, key=lambda word: word.s):
        text = " ".join(item.w.split())
        if not text or item.s >= window_s:
            continue
        start = max(0.0, item.s)
        words.append(Word(text, start, min(max(start, item.e), window_s)))
    return tidy_word_times(words)


def tidy_word_times(words: Sequence[Word]) -> list[Word]:
    """Repairs coarse word timings (the hosted API often gives several words the same start).

    Words that share a start time are spread evenly up to the start of the next word (at most
    `SHARED_START_STEP_S` each), and no word ends after the next one begins.
    """
    tidy = list(words)
    i = 0
    while i < len(tidy):
        j = i + 1
        while j < len(tidy) and tidy[j].start_s - tidy[i].start_s < MIN_WORD_STEP_S:
            j += 1
        if j - i > 1:
            run_end = tidy[j].start_s if j < len(tidy) else max(word.end_s for word in tidy[i:j])
            step = min(SHARED_START_STEP_S, max(0.0, run_end - tidy[i].start_s) / (j - i))
            if step >= MIN_WORD_STEP_S:
                base = tidy[i].start_s
                for k in range(i, j):
                    start = base + step * (k - i)
                    tidy[k] = Word(tidy[k].text, start, start + step)
        i = j
    for k in range(len(tidy) - 1):
        if tidy[k].end_s > tidy[k + 1].start_s:
            tidy[k] = Word(tidy[k].text, tidy[k].start_s, max(tidy[k].start_s, tidy[k + 1].start_s))
    return tidy


def segments_from_words(words: Sequence[Word]) -> list[Segment]:
    """Groups words into sentence-like segments for translation.

    A segment ends at a pause longer than `SEGMENT_PAUSE_S`, after sentence punctuation, or when it would
    grow past `SEGMENT_MAX_S` seconds or `SEGMENT_MAX_WORDS` words.
    """
    segments: list[Segment] = []
    current: list[Word] = []

    def flush() -> None:
        if current:
            segments.append(Segment(current[0].start_s, current[-1].end_s, " ".join(w.text for w in current)))
            current.clear()

    for word in words:
        if current:
            pause = word.start_s - current[-1].end_s > SEGMENT_PAUSE_S
            too_long = word.end_s - current[0].start_s > SEGMENT_MAX_S or len(current) >= SEGMENT_MAX_WORDS
            if pause or too_long or current[-1].text.rstrip("\"')").endswith(_SENTENCE_END):
                flush()
        current.append(word)
    flush()
    return segments


def spread_words(segments: Sequence[TranslatedSegment]) -> list[Word]:
    """English words for translated segments, spread over each segment's time span.

    Each word gets a share of the span in proportion to its length, so long words stay on screen longer
    than short ones.
    """
    words: list[Word] = []
    for segment in segments:
        tokens = segment.en.split()
        span = segment.end - segment.start
        if not tokens or span <= 0:
            continue
        weights = [len(token) + 1 for token in tokens]
        total = float(sum(weights))
        cursor = segment.start
        for token, weight in zip(tokens, weights, strict=True):
            end = cursor + span * weight / total
            words.append(Word(token, cursor, end))
            cursor = end
    return words


def stored_transcript(words: Sequence[Word], language: str = "en") -> dict[str, object]:
    """The `clips.transcript` format of docs/09 section 2: `{"language": "en", "words": [{w, s, e}]}`."""
    return {
        "language": language,
        "words": [{"w": word.text, "s": round(word.start_s, 3), "e": round(word.end_s, 3)} for word in words],
    }


def _usable(mode: Mode, transcriber: Transcriber) -> bool:
    if mode == "local":
        return True
    if not transcriber.is_configured():
        return False
    try:
        return transcribe_client.budget_ok()
    except Exception:
        # No database (offline render) or an unreadable setting: the budget cannot be checked, so the
        # paid mode is not used.
        return False


def transcribe_window(
    source: Path,
    window: ClipWindow,
    *,
    modes: Sequence[Mode],
    scratch_dir: Path,
    transcriber: Transcriber,
    ai_lane: BudgetKind = "vod",
    ai_client: AnthropicClient | None = None,
    ref_id: str | None = None,
    translate: bool = True,
    ffmpeg: str = "ffmpeg",
    pause_s: float = 0.0,
    punctuate_below: float = 0.0,
) -> WindowTranscript:
    """Transcribe `window` of `source`; words come back in English, in window time.

    With `pause_s` above zero, words stretched across a silence of at least that length are pulled back
    onto the speech (see the module docstring), so pauses show as gaps between words. With
    `punctuate_below` above zero, an English transcript in which a smaller share of words than that ends
    a sentence gets its sentence ends from prompt P10.

    `modes` are tried in order (`("local", "hosted")` for a normal render, `("hosted", "local")` for an
    express one); the hosted mode is skipped when no provider has a key or today's budget is used up.
    Raises `TranscriptionFailedError` when every mode failed.
    """
    log = get_logger("factory")
    wav = extract_wav(source, window, scratch_dir / WAV_NAME, ffmpeg)
    result: Transcript | None = None
    last_error: Exception | None = None
    for mode in modes:
        if not _usable(mode, transcriber):
            log.info("transcribe_mode_skipped", mode=mode)
            continue
        try:
            result = transcriber.transcribe(wav, mode=mode)
            break
        except Exception as exc:
            last_error = exc
            log.warning("transcribe_mode_failed", mode=mode, error=type(exc).__name__)
    if result is None:
        raise TranscriptionFailedError(f"transcription failed in modes {list(modes)}") from last_error
    words = words_from_client(result, window.duration_s)
    silences = speech_silences(wav, pause_s) if pause_s > 0 and words else []
    if silences:
        words = tighten_words(words, silences, MIN_TIGHT_WORD_S)
    original = language_code(result.language)
    translated = False
    if translate and words and not is_english(original):
        segments = segments_from_words(words)
        english = calls.translate_segments(
            segments, original or "unknown", lane=ai_lane, ref_id=ref_id, client=ai_client
        )
        words = spread_words(english)
        translated = True
    punctuated = False
    if punctuate_below > 0 and words and not translated and sentence_end_share(words) < punctuate_below:
        try:
            ends = calls.sentence_ends(
                [word.text for word in words], lane=ai_lane, ref_id=ref_id, client=ai_client
            )
        except Exception as exc:
            # Budget used up, no key, no database (offline render) or an unusable reply: the cut then
            # relies on the pauses alone.
            log.warning("sentence_ends_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        else:
            words = mark_sentence_ends(words, ends)
            punctuated = bool(ends)
    log.info(
        "window_transcribed",
        provider=result.provider,
        model=result.model,
        language=original,
        translated=translated,
        words=len(words),
        window_s=round(window.duration_s, 2),
        silences=len(silences),
        punctuated=punctuated,
    )
    return WindowTranscript(
        language="en" if translated or is_english(original) else (original or "en"),
        original_language=original,
        words=tuple(words),
        provider=result.provider,
        model=result.model,
        translated=translated,
    )

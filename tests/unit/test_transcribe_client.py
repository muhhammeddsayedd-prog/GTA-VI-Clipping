"""Transcription: provider choice and fallback, the OpenAI-style response, costs and the local model."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from sixstars.clients import transcribe
from sixstars.clients._http import ApiError, NotConfiguredError
from sixstars.clients.transcribe import Transcriber, TranscriptionError

VERBOSE = {
    "task": "transcribe",
    "language": "english",
    "duration": 5.0,
    "text": "No way he landed that",
    "words": [
        {"word": "No", "start": 0.0, "end": 0.3},
        {"word": " way", "start": 0.3, "end": 0.62},
        {"word": "he", "start": 0.9, "end": 1.0},
        {"word": "landed", "start": 1.0, "end": 1.4},
        {"word": "that", "start": 1.4, "end": 1.7},
    ],
}


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "clip.flac"
    path.write_bytes(b"fLaC-not-really")
    return path


@pytest.fixture(autouse=True)
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Account alerts sent (no database in unit tests); the cooldown starts empty for every test."""
    sent: list[tuple[str, int]] = []
    monkeypatch.setattr(
        transcribe, "account_alert", lambda provider, exc: sent.append((provider, exc.status))
    )
    monkeypatch.setattr(transcribe, "_account_blocked_until", {})
    return sent


@pytest.fixture
def usage_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        transcribe.usage,
        "record",
        lambda provider, operation, **kw: rows.append({"provider": provider, **kw}),
    )
    return rows


def test_provider_order() -> None:
    assert transcribe.provider_order("groq", {"openai", "groq"}) == ["groq", "openai"]
    assert transcribe.provider_order("openai", {"openai", "groq"}) == ["openai", "groq"]
    assert transcribe.provider_order("groq", {"openai"}) == ["openai"]
    assert transcribe.provider_order("groq", set()) == []


@respx.mock
def test_openai_is_used_when_groq_is_preferred_but_has_no_key(
    clip: Path, usage_rows: list[dict[str, Any]]
) -> None:
    openai = respx.post(transcribe.OPENAI_URL).mock(return_value=httpx.Response(200, json=VERBOSE))
    groq = respx.post(transcribe.GROQ_URL).mock(return_value=httpx.Response(500))
    transcriber = Transcriber(openai_api_key="sk-openai", express_provider="groq")
    assert transcriber.configured_providers() == {"openai"}

    transcript = transcriber.transcribe(clip, mode="hosted", language="en")

    assert groq.call_count == 0
    assert (
        transcript.provider == "openai" and transcript.language == "english" and transcript.duration_s == 5.0
    )
    assert [word.w for word in transcript.words] == ["No", "way", "he", "landed", "that"]
    assert transcript.words[1].s == 0.3 and transcript.words[1].e == 0.62
    assert transcript.text == "No way he landed that"
    request = openai.calls[0].request
    assert request.headers["Authorization"] == "Bearer sk-openai"
    body = request.content
    assert b'name="model"\r\n\r\nwhisper-1' in body
    assert b'name="response_format"\r\n\r\nverbose_json' in body
    assert b'name="timestamp_granularities[]"\r\n\r\nword' in body
    assert b'name="language"\r\n\r\nen' in body
    assert b"fLaC-not-really" in body
    assert usage_rows == [
        {"provider": "openai", "model": "whisper-1", "units": 5.0, "cost_usd": pytest.approx(5 / 60 * 0.006)}
    ]


@respx.mock
def test_groq_is_used_when_preferred_and_configured(clip: Path, usage_rows: list[dict[str, Any]]) -> None:
    groq = respx.post(transcribe.GROQ_URL).mock(return_value=httpx.Response(200, json=VERBOSE))
    openai = respx.post(transcribe.OPENAI_URL).mock(return_value=httpx.Response(200, json=VERBOSE))
    transcript = Transcriber(openai_api_key="o", groq_api_key="g", express_provider="groq").transcribe(
        clip, mode="hosted"
    )
    assert transcript.provider == "groq" and transcript.model == "whisper-large-v3-turbo"
    assert openai.call_count == 0
    assert b"whisper-large-v3-turbo" in groq.calls[0].request.content
    assert b'name="language"' not in groq.calls[0].request.content
    # Groq bills at least 10 seconds per request.
    assert usage_rows[0]["cost_usd"] == pytest.approx(10 / 3600 * 0.04)


@respx.mock
def test_falls_back_to_the_other_provider_when_the_first_fails(clip: Path, sleeps: list[float]) -> None:
    groq = respx.post(transcribe.GROQ_URL).mock(return_value=httpx.Response(503))
    respx.post(transcribe.OPENAI_URL).mock(return_value=httpx.Response(200, json=VERBOSE))
    transcript = Transcriber(openai_api_key="o", groq_api_key="g", express_provider="groq").transcribe(
        clip, mode="hosted"
    )
    assert groq.call_count == 3 and transcript.provider == "openai"


@respx.mock
def test_every_provider_failing_raises(clip: Path) -> None:
    respx.post(transcribe.OPENAI_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(TranscriptionError, match="openai"):
        Transcriber(openai_api_key="o", express_provider="openai").transcribe(clip, mode="hosted")


def test_no_hosted_provider_configured(clip: Path) -> None:
    transcriber = Transcriber(express_provider="openai")
    assert transcriber.is_configured() is False
    with pytest.raises(NotConfiguredError):
        transcriber.transcribe(clip, mode="hosted")


def test_parse_verbose_json_without_duration_or_words() -> None:
    empty = transcribe.parse_verbose_json({"text": ""}, "openai", "whisper-1")
    assert empty.words == () and empty.duration_s == 0.0 and empty.language is None
    no_duration = transcribe.parse_verbose_json(
        {"words": [{"word": "hi", "start": 0.1, "end": 0.4}]}, "openai", "whisper-1"
    )
    assert no_duration.duration_s == 0.4


def test_hosted_cost() -> None:
    assert transcribe.hosted_cost_usd("openai", 60) == pytest.approx(0.006)
    assert transcribe.hosted_cost_usd("groq", 3600) == pytest.approx(0.04)
    assert transcribe.hosted_cost_usd("groq", 2) == pytest.approx(10 / 3600 * 0.04)


def test_local_mode_loads_the_model_once(clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe, "_models", {})
    monkeypatch.setattr(transcribe, "decode_samples", lambda path: f"samples of {path.name}")
    loads: list[str] = []
    calls: list[dict[str, Any]] = []

    class FakeWhisper:
        def transcribe(self, path: str, **kwargs: Any) -> tuple[Any, Any]:
            calls.append({"path": path, **kwargs})
            segment = SimpleNamespace(
                words=[
                    SimpleNamespace(word=" clip", start=0.5, end=0.9),
                    SimpleNamespace(word=" it", start=0.9, end=1.1),
                ]
            )
            return iter([segment, SimpleNamespace(words=None)]), SimpleNamespace(language="en", duration=5.0)

    def factory(name: str) -> FakeWhisper:
        loads.append(name)
        return FakeWhisper()

    transcriber = Transcriber(whisper_model="medium", model_factory=factory)
    first = transcriber.transcribe(clip)
    transcriber.transcribe(clip, mode="local", language="en")

    assert loads == ["medium"]
    assert calls[0]["path"] == "samples of clip.flac"
    assert first.provider == "local" and first.language == "en" and first.duration_s == 5.0
    assert [(word.w, word.s, word.e) for word in first.words] == [("clip", 0.5, 0.9), ("it", 0.9, 1.1)]
    assert (
        calls[0]["word_timestamps"] is True
        and calls[0]["vad_filter"] is True
        and calls[0]["language"] is None
    )
    assert calls[1]["language"] == "en"


def test_too_large_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe, "HOSTED_MAX_BYTES", 4)
    big = tmp_path / "big.flac"
    big.write_bytes(b"12345")
    with pytest.raises(TranscriptionError, match="25 MB"):
        Transcriber(openai_api_key="o", express_provider="openai").transcribe(big, mode="hosted")


@pytest.mark.ffmpeg
def test_local_mode_gives_the_model_samples_not_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """faster-whisper 1.2.1 opens a path with `av.open(metadata_errors=...)`, which PyAV 19 rejects, so the
    local path must decode the file itself and hand over 16 kHz mono float samples."""
    import shutil
    import wave

    import numpy as np

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    source = tmp_path / "tone.wav"
    rate, seconds = 8000, 2
    tone = (np.sin(np.arange(rate * seconds) * 2 * np.pi * 440 / rate) * 12000).astype(np.int16)
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.repeat(tone, 2).tobytes())
    monkeypatch.setattr(transcribe, "_models", {})
    seen: list[Any] = []

    class PathRejectingWhisper:
        def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, Any]:
            if isinstance(audio, str | Path):
                raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
            seen.append(audio)
            return iter([]), SimpleNamespace(language="en", duration=float(len(audio)) / 16000)

    transcriber = Transcriber(whisper_model="tiny", model_factory=lambda name: PathRejectingWhisper())
    result = transcriber.transcribe(source, mode="local")

    samples = seen[0]
    assert samples.dtype == np.float32 and samples.ndim == 1
    assert abs(len(samples) - transcribe.AUDIO_SAMPLE_RATE * seconds) <= 16
    assert 0.2 < float(np.abs(samples).max()) <= 1.0
    assert result.duration_s == pytest.approx(seconds, abs=0.01) and result.words == ()


def test_decode_samples_reports_an_unreadable_file(tmp_path: Path) -> None:
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio")
    with pytest.raises(TranscriptionError, match="could not decode broken.wav"):
        transcribe.decode_samples(broken)


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (429, "You have no credits remaining. Add credits to continue using the API", True),
        (429, "You exceeded your current quota", True),
        (429, "Rate limit reached for requests", False),
        (401, "Incorrect API key provided", True),
        (403, "forbidden", True),
        (500, "server error", False),
    ],
)
def test_is_account_problem(status: int, message: str, expected: bool) -> None:
    exc = ApiError("openai", "POST /v1/audio/transcriptions", status, message)
    assert transcribe.is_account_problem(exc) is expected
    assert transcribe.is_account_problem(ValueError("x")) is False


@respx.mock
def test_out_of_credit_alerts_once_and_skips_the_provider_for_a_while(
    clip: Path, alerts: list[tuple[str, int]], sleeps: list[float]
) -> None:
    openai = respx.post(transcribe.OPENAI_URL).mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "You have no credits remaining.", "code": "credit_balance_exhausted"}},
        )
    )
    groq = respx.post(transcribe.GROQ_URL).mock(return_value=httpx.Response(200, json=VERBOSE))
    transcriber = Transcriber(openai_api_key="o", groq_api_key="g", express_provider="openai")

    first = transcriber.transcribe(clip, mode="hosted")
    calls_after_first = openai.call_count
    second = transcriber.transcribe(clip, mode="hosted")

    assert first.provider == "groq" and second.provider == "groq"
    assert openai.call_count == calls_after_first  # cooling down: not called again
    assert groq.call_count == 2
    assert alerts == [("openai", 429)]


@respx.mock
def test_every_provider_cooling_down_raises(clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe, "_account_blocked_until", {"openai": float("inf")})
    openai = respx.post(transcribe.OPENAI_URL).mock(return_value=httpx.Response(200, json=VERBOSE))
    with pytest.raises(TranscriptionError, match="openai"):
        Transcriber(openai_api_key="o", express_provider="openai").transcribe(clip, mode="hosted")
    assert openai.call_count == 0

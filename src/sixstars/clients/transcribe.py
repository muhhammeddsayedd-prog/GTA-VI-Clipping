"""Speech-to-text behind one interface: local faster-whisper for VOD windows and a hosted Whisper API for the
express (live spike) path. Implemented in M02.

Sources (checked 2026-11-17):
- OpenAI: https://developers.openai.com/api/docs/guides/speech-to-text (word timestamps exist only on
  `whisper-1`, with `response_format=verbose_json` and `timestamp_granularities[]=word`; 25 MB file limit;
  `whisper-1` shuts down on 2027-02-26), price $0.006 per minute
  (https://developers.openai.com/api/docs/pricing)
- Groq: https://console.groq.com/docs/speech-to-text (`whisper-large-v3-turbo`, $0.04 per audio hour,
  at least 10 seconds billed per request, same request and response shape as OpenAI)
- faster-whisper 1.2: `WhisperModel(...).transcribe(..., word_timestamps=True, vad_filter=True)`

The hosted provider comes from the `transcribe.express_provider` setting; when that provider has no key
or fails, the other configured provider is used. Send 16 kHz mono audio (see `extract_audio`).
"""

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from sixstars import settings, usage
from sixstars.clients import _http
from sixstars.clients._http import ApiError, ClientError, NotConfiguredError
from sixstars.config import env
from sixstars.logging import get_logger

SERVICE = "transcribe"
Mode = Literal["local", "hosted"]
Provider = Literal["openai", "groq"]

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MODEL = "whisper-1"
OPENAI_USD_PER_MINUTE = 0.006
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_USD_PER_HOUR = 0.04
GROQ_MIN_BILLED_S = 10.0
HOSTED_MAX_BYTES = 25 * 1024 * 1024
PROVIDERS: tuple[Provider, ...] = ("openai", "groq")
AUDIO_SAMPLE_RATE = 16000
LOCAL_COMPUTE_TYPE = "int8"
DECODE_TIMEOUT_S = 600


class TranscriptionError(ClientError):
    """Transcription failed with every provider that could be tried."""


@dataclass(frozen=True)
class Word:
    """One spoken word with start `s` and end `e` in seconds from the start of the audio."""

    w: str
    s: float
    e: float


@dataclass(frozen=True)
class Transcript:
    """Result of a transcription. `language` is what the provider reports (`en`, or `english` from the
    hosted Whisper API), lower case; None when unknown."""

    language: str | None
    words: tuple[Word, ...]
    duration_s: float = 0.0
    provider: str = ""
    model: str = ""

    @property
    def text(self) -> str:
        return " ".join(word.w for word in self.words)


def extract_audio(source: Path, destination: Path, ffmpeg: str = "ffmpeg") -> Path:
    """Write the audio of `source` to `destination` as 16 kHz mono (FLAC or WAV by file extension)."""
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
    ]
    subprocess.run([*command, str(destination)], check=True, capture_output=True, timeout=300)
    return destination


def decode_samples(source: Path, ffmpeg: str = "ffmpeg") -> Any:
    """Decode the audio of `source` to 16 kHz mono float samples in [-1, 1] (a numpy array).

    The local model is given these samples instead of the path: faster-whisper 1.2.1 decodes a path with
    PyAV and calls `av.open(..., metadata_errors=...)`, which PyAV 19 rejects with a `TypeError`.
    """
    import numpy as np

    command = [ffmpeg, "-nostdin", "-v", "error", "-i", str(source), "-vn", "-ac", "1"]
    command += ["-ar", str(AUDIO_SAMPLE_RATE), "-f", "f32le", "-"]
    try:
        done = subprocess.run(
            command, check=False, capture_output=True, timeout=DECODE_TIMEOUT_S, stdin=subprocess.DEVNULL
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise TranscriptionError(f"could not decode {source.name}: {type(exc).__name__}") from exc
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", errors="replace")[-300:]
        raise TranscriptionError(f"could not decode {source.name} (exit code {done.returncode}): {tail}")
    return np.frombuffer(done.stdout, dtype=np.float32)


def hosted_cost_usd(provider: Provider, seconds: float) -> float:
    """Cost estimate of one hosted request for `seconds` of audio."""
    if provider == "groq":
        return max(seconds, GROQ_MIN_BILLED_S) / 3600.0 * GROQ_USD_PER_HOUR
    return seconds / 60.0 * OPENAI_USD_PER_MINUTE


def parse_verbose_json(body: dict[str, Any], provider: str, model: str) -> Transcript:
    """Build a `Transcript` from an OpenAI-style `verbose_json` response with word timestamps."""
    words = tuple(
        Word(w=str(item["word"]).strip(), s=float(item["start"]), e=float(item["end"]))
        for item in body.get("words") or []
        if str(item.get("word", "")).strip()
    )
    duration = body.get("duration")
    seconds = float(duration) if isinstance(duration, int | float | str) and duration != "" else 0.0
    if seconds <= 0 and words:
        seconds = words[-1].e
    language = body.get("language")
    return Transcript(
        language=str(language).lower() if language else None,
        words=words,
        duration_s=seconds,
        provider=provider,
        model=model,
    )


def provider_order(preferred: str, configured: set[str]) -> list[Provider]:
    """Providers to try, the preferred one first, limited to those that have a key."""
    ordered = sorted(PROVIDERS, key=lambda name: name != preferred)
    return [name for name in ordered if name in configured]


def budget_ok() -> bool:
    """False when today's hosted transcription spend has reached `transcribe.daily_budget_usd`."""
    return usage.spent_today(PROVIDERS) < settings.get_float("transcribe.daily_budget_usd")


class Transcriber:
    """Transcription with word timestamps, locally or through a hosted API."""

    def __init__(
        self,
        openai_api_key: str | None = None,
        groq_api_key: str | None = None,
        http: httpx.Client | None = None,
        express_provider: str | None = None,
        whisper_model: str | None = None,
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._openai_key = openai_api_key
        self._groq_key = groq_api_key
        self._http = http or _http.new_client(_http.UPLOAD_TIMEOUT)
        self._express_provider = express_provider
        self._whisper_model = whisper_model
        self._model_factory = model_factory or _load_whisper
        self._log = get_logger(SERVICE)

    # ── configuration ────────────────────────────────────────

    def _key(self, provider: Provider) -> str | None:
        settings_env = env()
        explicit = self._openai_key if provider == "openai" else self._groq_key
        secret = settings_env.OPENAI_API_KEY if provider == "openai" else settings_env.GROQ_API_KEY
        return explicit or (secret.get_secret_value() if secret is not None else None)

    def configured_providers(self) -> set[str]:
        """Hosted providers that have an API key."""
        return {name for name in PROVIDERS if self._key(name)}

    def is_configured(self) -> bool:
        """True when at least one hosted provider can be used."""
        return bool(self.configured_providers())

    # ── public interface ─────────────────────────────────────

    def transcribe(self, audio_path: Path, mode: Mode = "local", language: str | None = None) -> Transcript:
        """Transcribe an audio file (16 kHz mono) and return words with timestamps.

        `mode="local"` runs faster-whisper in this process; `mode="hosted"` calls the provider named by
        `transcribe.express_provider` and falls back to the other configured provider. `language` is an
        ISO 639-1 code; None lets the model detect it.
        """
        if mode == "local":
            return self._local(audio_path, language)
        return self._hosted(audio_path, language)

    # ── hosted ───────────────────────────────────────────────

    def _hosted(self, audio_path: Path, language: str | None) -> Transcript:
        preferred = self._express_provider or settings.get_str("transcribe.express_provider")
        order = provider_order(preferred, self.configured_providers())
        if not order:
            raise NotConfiguredError(
                "no hosted transcription provider is configured (OPENAI_API_KEY or GROQ_API_KEY)"
            )
        if audio_path.stat().st_size > HOSTED_MAX_BYTES:
            raise TranscriptionError(f"{audio_path.name} is larger than the 25 MB hosted transcription limit")
        last_error: Exception | None = None
        for provider in order:
            try:
                return self._hosted_call(provider, audio_path, language)
            except (ApiError, httpx.TransportError, ValueError, KeyError) as exc:
                last_error = exc
                self._log.warning("transcribe_provider_failed", provider=provider, error=type(exc).__name__)
        raise TranscriptionError(f"hosted transcription failed with {', '.join(order)}") from last_error

    def _hosted_call(self, provider: Provider, audio_path: Path, language: str | None) -> Transcript:
        url, model = (OPENAI_URL, OPENAI_MODEL) if provider == "openai" else (GROQ_URL, GROQ_MODEL)
        endpoint = "POST /v1/audio/transcriptions"
        data: dict[str, str] = {
            "model": model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        if language:
            data["language"] = language
        headers = {"Authorization": f"Bearer {self._key(provider)}"}

        def call() -> httpx.Response:
            with audio_path.open("rb") as handle:
                files = {"file": (audio_path.name, handle, "application/octet-stream")}
                return self._http.post(url, data=data, files=files, headers=headers)

        response = _http.send(provider, endpoint, call)
        _http.raise_for_status(provider, endpoint, response)
        transcript = parse_verbose_json(response.json(), provider, model)
        usage.record(
            provider,
            "transcribe",
            model=model,
            units=transcript.duration_s,
            cost_usd=hosted_cost_usd(provider, transcript.duration_s),
        )
        return transcript

    # ── local ────────────────────────────────────────────────

    def _local(self, audio_path: Path, language: str | None) -> Transcript:
        name = self._whisper_model or settings.get_str("vod.whisper_model")
        model = _cached_model(name, self._model_factory)
        started = time.monotonic()
        segments, info = model.transcribe(
            decode_samples(audio_path), language=language, word_timestamps=True, vad_filter=True
        )
        words = tuple(
            Word(w=str(word.word).strip(), s=float(word.start), e=float(word.end))
            for segment in segments
            for word in (segment.words or [])
            if str(word.word).strip()
        )
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        detected = getattr(info, "language", None)
        self._log.info(
            "local_transcription",
            model=name,
            audio_s=round(duration, 1),
            words=len(words),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        usage.record("local", "transcribe", model=f"faster-whisper-{name}", units=duration, cost_usd=0.0)
        return Transcript(
            language=str(detected).lower() if detected else None,
            words=words,
            duration_s=duration,
            provider="local",
            model=name,
        )


_models: dict[str, Any] = {}
_models_lock = threading.Lock()


def _load_whisper(name: str) -> Any:
    """Load a faster-whisper model on the CPU with int8 weights. Imported lazily: the library is heavy."""
    from faster_whisper import WhisperModel

    return WhisperModel(name, device="cpu", compute_type=LOCAL_COMPUTE_TYPE)


def _cached_model(name: str, factory: Callable[[str], Any]) -> Any:
    """Return the model for `name`, loading it once per process."""
    with _models_lock:
        if name not in _models:
            _models[name] = factory(name)
        return _models[name]


def transcribe(audio_path: Path, mode: Mode = "local", language: str | None = None) -> Transcript:
    """Module-level shortcut for `Transcriber().transcribe(...)` with configuration from the environment."""
    return Transcriber().transcribe(audio_path, mode=mode, language=language)

"""Transcript-based moment picking for videos that have no usable chat replay. Implemented in M08.

Order of work, cheapest first:

1. **Caption track.** YouTube's own captions (creator-written or automatic) fetched with yt-dlp, no media:
   a few MB, seconds, no fee. `process` fetches the track and hands it in.
2. **Own transcription**, only when there is no caption track: the smallest audio stream of the video is
   downloaded (about 20 MB per hour, never the picture) and at most `vod.fallback_transcribe_max_s`
   seconds of it are transcribed, within `vod.fallback_transcribe_daily_s` per UTC day. Local
   faster-whisper costs no fee but several minutes of CPU per ten minutes of audio; `hosted` costs the
   provider's price per minute and also obeys `transcribe.daily_budget_usd`.

Either way the transcript goes to the moment picker (P3) in blocks of `vod.fallback_chunk_s` seconds and
the moments are stored as spikes of mode `transcript`: `confirmed` when the picker's confidence reaches
`vod.fallback_min_confidence`, else `rejected`. Their windows are then downloaded like chat spikes; the
whole video is never downloaded.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sixstars import events, settings, usage
from sixstars.ai import calls
from sixstars.ai.calls import TranscriptChunk
from sixstars.ai.schemas import Moment
from sixstars.clients import transcribe as transcribe_client
from sixstars.clients.anthropic_client import InvalidModelOutput
from sixstars.clients.transcribe import Transcriber
from sixstars.factory import tools
from sixstars.factory.pipeline import next_budget_reset
from sixstars.logging import get_logger
from sixstars.vod import captions, store, ytdlp
from sixstars.vod.captions import Track, Word
from sixstars.vod.common import DeferredError, Deps
from sixstars.vod.config import VodConfig
from sixstars.vod.ranges import Range, overlaps
from sixstars.vod.ytdlp import Access

AUDIO_NAME: Final = "audio"
# Smallest audio stream first: speech recognition does not need more.
AUDIO_FORMAT: Final = "wa[acodec!=none]/ba/b"
USAGE_PROVIDER: Final = "vod"


@dataclass(frozen=True, slots=True)
class FallbackResult:
    """What the fallback did for one video. `source` is `captions`, `local`, `hosted` or `none`."""

    source: str
    reason: str = ""
    transcript_s: float = 0.0
    transcribed_s: float = 0.0
    moments: int = 0
    kept: int = 0


@dataclass(frozen=True, slots=True)
class MomentSpike:
    """A picked moment as the lane stores it."""

    start_s: float
    end_s: float
    confidence: float
    reason: str
    tier: str
    status: str
    pad_start_s: float
    pad_end_s: float
    spike_floor_s: float


def transcribe_range(duration_s: float | None, limit_s: float, warmup_s: float) -> tuple[float, float]:
    """Which part of a video to transcribe when only `limit_s` seconds are allowed. Pure.

    A video that fits is taken whole. A longer one loses its first `warmup_s` (greetings and set-up)
    when enough remains after them.
    """
    if limit_s <= 0:
        return 0.0, 0.0
    if duration_s is None or duration_s <= 0:
        return 0.0, limit_s
    if duration_s <= limit_s:
        return 0.0, duration_s
    start = warmup_s if duration_s - warmup_s >= limit_s else max(0.0, duration_s - limit_s)
    return start, start + limit_s


def moment_spike(
    moment: Moment, cfg: VodConfig, *, a_min_confidence: float, video_duration_s: float | None
) -> MomentSpike:
    """Turn a picked moment into a spike: status and tier from its confidence, the window padded by
    `spikes.context_pad_s` for the clip writer, the latest allowed clip start at the middle. Pure."""
    confirmed = moment.confidence >= cfg.fallback_min_confidence
    end_limit = video_duration_s if video_duration_s and video_duration_s > 0 else float("inf")
    return MomentSpike(
        start_s=moment.start_s,
        end_s=moment.end_s,
        confidence=moment.confidence,
        reason=f"{moment.moment_type}: {moment.one_line}",
        tier="A" if moment.confidence >= a_min_confidence else "B",
        status="confirmed" if confirmed else "rejected",
        pad_start_s=max(0.0, moment.start_s - cfg.context_pad_s),
        pad_end_s=min(end_limit, moment.end_s + cfg.context_pad_s),
        spike_floor_s=(moment.start_s + moment.end_s) / 2,
    )


def limit_chunks(chunks: Sequence[TranscriptChunk], max_s: float, warmup_s: float) -> list[TranscriptChunk]:
    """The part of a transcript the moment picker is paid to read: at most `max_s` seconds of it
    (0 = all). A longer transcript loses its first `warmup_s` when enough remains after them. Pure."""
    ordered = sorted(chunks, key=lambda chunk: chunk.start_s)
    if not ordered or max_s <= 0 or ordered[-1].end_s - ordered[0].start_s <= max_s:
        return ordered
    start_s, end_s = transcribe_range(ordered[-1].end_s, max_s, warmup_s)
    return [chunk for chunk in ordered if chunk.start_s >= start_s and chunk.end_s <= end_s]


def pick(
    video: store.Video, chunks: Sequence[TranscriptChunk], cfg: VodConfig, deps: Deps
) -> tuple[list[Moment], int]:
    """Ask the moment picker (P3) about each 30-minute part of the transcript on its own.

    Returns the merged moments (strongest first, at most the maximum of `vod.fallback_moments`) and the
    number of parts whose answer was still invalid after the client's repair retry. Such a part is left
    out instead of failing the video: the other parts are already paid for. `BudgetExceededError` and
    API failures are raised.
    """
    log = get_logger("vod")
    n_min, n_max = cfg.fallback_moments
    parts = calls.slice_chunks(chunks)
    found: list[Moment] = []
    failed = 0
    for part in parts:
        try:
            found.extend(
                calls.pick_moments(
                    part,
                    n_min if len(parts) == 1 else 0,
                    n_max,
                    cfg.min_len_s,
                    cfg.max_len_s,
                    title=video.title,
                    creator=video.creator_handle,
                    ref_id=video.id,
                    client=deps.ai_client,
                )
            )
        except InvalidModelOutput as exc:
            failed += 1
            log.warning("fallback_part_invalid", start_s=part[0].start_s, error=str(exc)[:300])
    return calls.merge_moments(found, n_max), failed


def download_audio(url: str, work: Path, *, access: Access, timeout_s: float, deps: Deps) -> Path:
    """Download the smallest audio stream of the video into `work` and return the file."""
    work.mkdir(parents=True, exist_ok=True)
    arguments = ["-f", AUDIO_FORMAT, "--force-overwrites", "--no-part"]
    arguments += ["--output", str(work / f"{AUDIO_NAME}.%(ext)s"), url]
    ytdlp.run(arguments, access=access, timeout_s=timeout_s, what="audio", runner=deps.runner)
    files = [path for path in work.glob(f"{AUDIO_NAME}.*") if path.is_file()]
    if not files:
        raise ytdlp.YtDlpError(ytdlp.Failure("error", "yt-dlp finished but wrote no audio file"))
    return max(files, key=lambda path: path.stat().st_size)


def transcribe_audio(
    audio: Path,
    start_s: float,
    end_s: float,
    cfg: VodConfig,
    work: Path,
    deps: Deps,
    on_progress: Callable[[], None] | None = None,
) -> tuple[list[Word], float, str]:
    """Transcribe `[start_s, end_s]` of an audio file in blocks. Returns the words on the video's clock,
    the seconds transcribed and the language. A hosted run stops early when the daily budget is used up."""
    log = get_logger("vod")
    mode: transcribe_client.Mode = "local" if cfg.fallback_transcribe_mode == "local" else "hosted"
    transcriber = deps.transcriber or Transcriber(
        whisper_model=cfg.fallback_whisper_model or cfg.whisper_model
    )
    words: list[Word] = []
    language = ""
    done_s = 0.0
    position = start_s
    while position < end_s:
        length = min(cfg.fallback_transcribe_block_s, end_s - position)
        if mode == "hosted" and not transcribe_client.budget_ok():
            log.warning("fallback_transcribe_budget_reached", transcribed_s=done_s)
            break
        wav = work / f"block_{int(position)}.wav"
        tools.extract_audio(
            audio,
            wav,
            start_s=position,
            duration_s=length,
            sample_rate=transcribe_client.AUDIO_SAMPLE_RATE,
            ffmpeg=deps.ffmpeg,
        )
        result = transcriber.transcribe(wav, mode=mode)
        wav.unlink(missing_ok=True)
        words.extend(Word(word.w, position + word.s, position + word.e) for word in result.words)
        language = language or (result.language or "")
        usage.record(USAGE_PROVIDER, store.USAGE_FALLBACK_TRANSCRIBE, model=result.model, units=length)
        done_s += length
        position += length
        if on_progress is not None:
            on_progress()
    return words, done_s, language


def own_transcript(
    video: store.Video,
    cfg: VodConfig,
    work: Path,
    access: Access,
    deps: Deps,
    on_progress: Callable[[], None] | None = None,
) -> tuple[Track | None, float, str]:
    """Transcribe the video ourselves, within the per-video and per-day limits.

    Returns `(track, seconds transcribed, why not)`. Raises `DeferredError` when today's allowance is
    used up (the video waits for the next UTC day).
    """
    if cfg.fallback_transcribe_max_s <= 0:
        return None, 0.0, "no caption track and own transcription is switched off"
    left_today = cfg.fallback_transcribe_daily_s - store.transcribed_today_s()
    if left_today <= 0:
        raise DeferredError(
            "transcribe_budget",
            "vod.fallback_transcribe_daily_s is used up for today",
            next_budget_reset(deps.now()),
        )
    limit = min(cfg.fallback_transcribe_max_s, left_today)
    start_s, end_s = transcribe_range(video.duration_s, limit, settings.get_float("spikes.warmup_s"))
    audio = download_audio(video.url, work, access=access, timeout_s=cfg.download_timeout_s, deps=deps)
    try:
        if video.duration_s is None:
            end_s = min(end_s, tools.probe(audio, deps.ffprobe).duration_s)
        words, done_s, language = transcribe_audio(audio, start_s, end_s, cfg, work, deps, on_progress)
    finally:
        audio.unlink(missing_ok=True)
    if not words:
        return None, done_s, "the transcription found no speech"
    return Track(language or "en", tuple(words), source=cfg.fallback_transcribe_mode), done_s, ""


def run(
    video: store.Video,
    cfg: VodConfig,
    *,
    track: Track | None,
    taken: Sequence[Range],
    access: Access,
    deps: Deps,
    on_progress: Callable[[], None] | None = None,
) -> FallbackResult:
    """Pick moments from a transcript and store them as spikes. Runs once per video.

    `track` is the caption track when the video has one. `taken` are windows that already have a spike
    (a moment that overlaps one is dropped). `BudgetExceededError` from the picker and `DeferredError`
    are raised to the caller.
    """
    log = get_logger("vod")
    if video.duration_s is not None and video.duration_s > cfg.fallback_max_duration_s:
        return _finish(video, FallbackResult("none", "longer than vod.fallback_max_duration_s"))
    transcribed_s = 0.0
    if track is None:
        track, transcribed_s, why_not = own_transcript(
            video, cfg, store.work_dir(video.id), access, deps, on_progress
        )
        if track is None:
            return _finish(video, FallbackResult("none", why_not, transcribed_s=transcribed_s))
    chunks = limit_chunks(
        captions.chunk_words(track.words, cfg.fallback_chunk_s),
        cfg.fallback_max_transcript_s,
        settings.get_float("spikes.warmup_s"),
    )
    moments, failed_slices = pick(video, chunks, cfg, deps)
    a_min = settings.get_float("spikes.s_min_confidence")
    kept = 0
    occupied = list(taken)
    for moment in moments:
        if overlaps(moment.start_s, moment.end_s, occupied):
            log.info("fallback_moment_overlaps_spike", start_s=moment.start_s, end_s=moment.end_s)
            continue
        spike = moment_spike(moment, cfg, a_min_confidence=a_min, video_duration_s=video.duration_s)
        store.insert_transcript_spike(
            video,
            start_s=spike.start_s,
            end_s=spike.end_s,
            confidence=spike.confidence,
            reason=spike.reason,
            tier=spike.tier,
            status=spike.status,
            parts={
                "first_t": spike.start_s,
                "spike_floor": spike.spike_floor_s,
                "pad_start": spike.pad_start_s,
                "pad_end": spike.pad_end_s,
                "source": track.source,
            },
        )
        occupied.append(Range(spike.start_s, spike.end_s))
        kept += spike.status == "confirmed"
    span = (chunks[-1].end_s - chunks[0].start_s) if chunks else 0.0
    return _finish(
        video,
        FallbackResult(
            track.source,
            reason=f"{failed_slices} part(s) of the transcript gave no valid answer" if failed_slices else "",
            transcript_s=span,
            transcribed_s=transcribed_s,
            moments=len(moments),
            kept=kept,
        ),
    )


def _finish(video: store.Video, result: FallbackResult) -> FallbackResult:
    """Record that the fallback ran for this video, so a retried job does not pay for it again."""
    events.emit(
        store.EVENT_FALLBACK,
        entity_type="video",
        entity_id=video.id,
        message=result.reason or f"{result.kept} of {result.moments} moments kept",
        data={
            "source": result.source,
            "transcript_s": round(result.transcript_s, 1),
            "transcribed_s": round(result.transcribed_s, 1),
            "moments": result.moments,
            "kept": result.kept,
        },
        service=store.SERVICE,
    )
    get_logger("vod").info(
        "fallback_done",
        source=result.source,
        reason=result.reason,
        moments=result.moments,
        kept=result.kept,
        transcribed_s=round(result.transcribed_s, 1),
    )
    return result

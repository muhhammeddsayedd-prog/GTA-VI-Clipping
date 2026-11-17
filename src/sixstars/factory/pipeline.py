"""The clip factory pipeline (docs/09 section 1): one source window in, one finished clip out.

`render_clip(RenderInput) -> clip_id` runs: transcribe the padded window, clip writer (P2), trim, music
detection, profanity, layout (cache or P4), subtitles and overlays, ffmpeg render with loudness
normalisation, then the clip row goes from `rendering` to `gating`. The job handlers `clip.render` and
`express.render` call it with the job payload.

The steps that need no database live in `produce` and `render_file`, so `sixstars clip render-file` can
run them on a local video for design work.

Times: `RenderInput` positions are seconds in the source file ("file time"); the transcript and the clip
writer work in seconds from the start of the padded window ("window time"); subtitles, bleeps and the
stored transcript are in seconds from the first frame of the clip ("clip time").
"""

import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from psycopg.types.json import Jsonb

from sixstars import db, events, jobs, notify
from sixstars import storage as clip_storage
from sixstars.ai.calls import BudgetExceededError, ChatLine
from sixstars.ai.schemas import ClipWriting, SpikeVerdict
from sixstars.clients.anthropic_client import AnthropicClient, BudgetKind
from sixstars.clients.transcribe import Mode, Transcriber
from sixstars.config import env
from sixstars.factory import facecam, music, tools, writer
from sixstars.factory import transcript as transcripts
from sixstars.factory.assets import FactoryAssets, load_assets
from sixstars.factory.audio import parse_loudnorm_report
from sixstars.factory.bleep import Span, bleep_spans
from sixstars.factory.config import FactoryConfig
from sixstars.factory.layout import Layout
from sixstars.factory.lines import Line, clip_lines, group_lines
from sixstars.factory.params import FactoryParams
from sixstars.factory.presets import RenderMode, uses_nvenc
from sixstars.factory.render import (
    RenderSpec,
    SourceInfo,
    build_loudnorm_measure_command,
    build_render_command,
)
from sixstars.factory.subtitles import build_ass
from sixstars.factory.textfit import TextMeasurer, estimate_text_width, pillow_measurer
from sixstars.factory.window import ClipWindow, padded_window
from sixstars.logging import get_logger

JOB_CLIP_RENDER = "clip.render"
JOB_EXPRESS_RENDER = "express.render"
ASS_NAME = "clip.ass"
SCRATCH_OUTPUT = "render.mp4"
# The finished file may differ from the planned length by this much (container rounding).
DURATION_TOLERANCE_S = 0.2
# A job deferred because the daily AI budget is used up runs again this long after 00:00 UTC.
BUDGET_RETRY_MARGIN_S = 120
LANES = ("spike", "hourly")
MODES = ("normal", "express")
SOURCE_TYPES = ("live", "vod")
PLATFORMS = ("twitch", "youtube")


class ClipSkippedError(RuntimeError):
    """The clip will not be rendered (no speech, or the clip writer rejected it). Not a failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RenderInput:
    """One clip to make. Positions are seconds in `source_path` (the section file or the live buffer cut).

    `spike_floor_s` is the latest allowed clip start. `pad_start_s` / `pad_end_s` bound what is transcribed
    and offered to the clip writer; when missing, the window is padded by `spikes.context_pad_s`.
    `source_offset_s` is where the file starts in the original video or stream, so `clips.start_s` /
    `clips.end_s` are positions in the original. `chat` times are seconds in the file. `clip_id` (a uuid
    chosen by the caller) or `spike_id` makes the render idempotent: a second call finds the same row.
    """

    source_path: Path
    window_start_s: float
    window_end_s: float
    creator: str
    spike_floor_s: float | None = None
    creator_id: str | None = None
    platform: str = "youtube"
    source_id: str = ""
    source_url: str = ""
    source_type: str = "vod"
    lane: str = "hourly"
    mode: str = "normal"
    stream_title: str = ""
    spike_id: str | None = None
    clip_id: str | None = None
    pad_start_s: float | None = None
    pad_end_s: float | None = None
    source_offset_s: float = 0.0
    verdict: SpikeVerdict | None = None
    chat: tuple[ChatLine, ...] = ()
    upload: bool = False

    def __post_init__(self) -> None:
        if self.lane not in LANES:
            raise ValueError(f"lane must be one of {LANES}, got {self.lane!r}")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {SOURCE_TYPES}, got {self.source_type!r}")
        if self.platform not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}, got {self.platform!r}")
        if self.window_start_s < 0 or self.window_end_s <= self.window_start_s:
            raise ValueError(f"invalid window {self.window_start_s}..{self.window_end_s}")

    def to_payload(self) -> dict[str, Any]:
        """The job payload for `clip.render` / `express.render` (JSON values only)."""
        return {
            "source_path": str(self.source_path),
            "window_start_s": self.window_start_s,
            "window_end_s": self.window_end_s,
            "spike_floor_s": self.spike_floor_s,
            "creator": self.creator,
            "creator_id": self.creator_id,
            "platform": self.platform,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "lane": self.lane,
            "mode": self.mode,
            "stream_title": self.stream_title,
            "spike_id": self.spike_id,
            "clip_id": self.clip_id,
            "pad_start_s": self.pad_start_s,
            "pad_end_s": self.pad_end_s,
            "source_offset_s": self.source_offset_s,
            "verdict": self.verdict.model_dump() if self.verdict is not None else None,
            "chat": [{"t_s": line.t_s, "text": line.text, "user": line.user} for line in self.chat],
            "upload": self.upload,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], default_mode: str = "normal") -> "RenderInput":
        """Build the input from a job payload. Unknown keys are ignored; `mode` defaults per job type."""

        def number(key: str) -> float | None:
            value = payload.get(key)
            return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

        def text(key: str, default: str = "") -> str:
            value = payload.get(key)
            return str(value) if value is not None else default

        def optional(key: str) -> str | None:
            value = payload.get(key)
            return str(value) if value else None

        start, end = number("window_start_s"), number("window_end_s")
        if not payload.get("source_path") or start is None or end is None:
            raise ValueError("render payload needs source_path, window_start_s and window_end_s")
        verdict = payload.get("verdict")
        chat = payload.get("chat") or []
        return cls(
            source_path=Path(str(payload["source_path"])),
            window_start_s=start,
            window_end_s=end,
            spike_floor_s=number("spike_floor_s"),
            creator=text("creator"),
            creator_id=optional("creator_id"),
            platform=text("platform", "youtube"),
            source_id=text("source_id"),
            source_url=text("source_url"),
            source_type=text("source_type", "vod"),
            lane=text("lane", "spike" if default_mode == "express" else "hourly"),
            mode=text("mode", default_mode),
            stream_title=text("stream_title"),
            spike_id=optional("spike_id"),
            clip_id=optional("clip_id"),
            pad_start_s=number("pad_start_s"),
            pad_end_s=number("pad_end_s"),
            source_offset_s=number("source_offset_s") or 0.0,
            verdict=SpikeVerdict.model_validate(verdict) if isinstance(verdict, Mapping) else None,
            chat=tuple(
                ChatLine(t_s=float(line["t_s"]), text=str(line["text"]), user=line.get("user"))
                for line in chat
                if isinstance(line, Mapping) and "t_s" in line and "text" in line
            ),
            upload=bool(payload.get("upload", False)),
        )


@dataclass(slots=True)
class Deps:
    """Replaceable collaborators (tests pass fakes). None means the real one, built on first use."""

    transcriber: Transcriber | None = None
    ai_client: AnthropicClient | None = None
    music_detector: music.MusicDetector | None = None
    storage: clip_storage.ClipStorage | None = None
    assets: FactoryAssets | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    skip_music: bool = False


@dataclass(frozen=True, slots=True)
class RenderJob:
    """Everything `render_file` needs: no database, no AI."""

    source_path: Path
    media: tools.MediaInfo
    window: ClipWindow
    lines: Sequence[Line]
    layout: Layout
    params: FactoryParams
    assets: FactoryAssets
    output_path: Path
    scratch_dir: Path
    series_no: int
    mode: RenderMode = "normal"
    lane: str = "hourly"
    creator: str = ""
    hook: str | None = None
    keywords: Mapping[int, Sequence[str]] = field(default_factory=dict)
    duck: Sequence[Span] = ()


@dataclass(frozen=True, slots=True)
class RenderOutcome:
    """What one ffmpeg render produced. `render_ms` covers the loudness pass and the encode."""

    output_path: Path
    media: tools.MediaInfo
    clip_duration_s: float
    render_ms: int
    measure_ms: int
    file_size_bytes: int
    bleeps: tuple[Span, ...]
    profanity_count: int
    sfx_times_s: tuple[float, ...]
    nvenc: bool


@dataclass(frozen=True, slots=True)
class Produced:
    """The result of `produce`: the decisions taken on the way and the render outcome."""

    transcript: transcripts.WindowTranscript
    writing: ClipWriting
    window: ClipWindow
    lines: tuple[Line, ...]
    layout: Layout
    music: music.MusicAnalysis | None
    outcome: RenderOutcome
    timings_ms: dict[str, int]


WritingChooser = Callable[[writer.WriterInput], ClipWriting]
LayoutChooser = Callable[[tools.MediaInfo, float], Layout]
WritingHook = Callable[[ClipWriting, transcripts.WindowTranscript], None]


def _measurer(assets: FactoryAssets) -> TextMeasurer:
    return pillow_measurer(assets.font_file) if assets.font_file is not None else estimate_text_width


def _move_into_place(finished: Path, destination: Path) -> None:
    """Move a finished file to its final path so that the path never holds a partial file.

    A rename is atomic on one file system. Across file systems the file is first copied next to the
    destination under a temporary name, then renamed.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(finished, destination)
    except OSError:
        partial = destination.with_name(destination.name + ".part")
        shutil.copyfile(finished, partial)
        os.replace(partial, destination)
        finished.unlink(missing_ok=True)


def render_file(job: RenderJob, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> RenderOutcome:
    """Write the subtitle file and render the clip with ffmpeg.

    The loudness is measured first and fed to the real pass (two-pass loudnorm) in both modes: the
    audio-only pass takes two to three seconds, and single-pass results landed up to 1 LU off target.
    The clip is rendered into the scratch directory and moved to `output_path` only when ffprobe confirms
    it, so a failed render never leaves a half-written file there.
    """
    log = get_logger("factory")
    params = job.params
    words = [word for line in job.lines for word in line.words]
    matcher = job.assets.profanity()
    profane = matcher.find([word.text for word in words])
    spec = RenderSpec(
        source=SourceInfo(job.source_path, job.media.width, job.media.height, job.media.has_audio),
        window=job.window,
        layout=job.layout,
        ass_path=job.scratch_dir / ASS_NAME,
        output_path=job.scratch_dir / SCRATCH_OUTPUT,
        series_no=job.series_no,
        params=params,
        mode=job.mode,
        fonts=job.assets.fonts,
        duck_spans=tuple(job.duck),
        sfx_path=job.assets.sfx_pop,
        logo_path=job.assets.logo,
        hook_shown=bool(job.hook),
    )
    duration = spec.clip_duration_s
    bleeps = tuple(bleep_spans(words, profane, duration)) if params.bleep else ()
    sfx_times: tuple[float, ...] = ()
    if job.assets.sfx_pop is not None:
        sfx_times = writer.outside_spans(writer.keyword_times(job.lines, job.keywords), bleeps)
    nvenc = params.use_nvenc and job.mode == "express" and tools.nvenc_available(ffmpeg)
    spec = replace(spec, bleeps=bleeps, sfx_times_s=sfx_times, nvenc_available=nvenc)
    ass = build_ass(
        job.lines,
        layout=job.layout.kind,
        params=params,
        duration_s=duration,
        keywords=job.keywords,
        profanity=matcher,
        hook=job.hook,
        creator=job.creator or None,
        live_badge=job.lane == "spike",
        measure=_measurer(job.assets),
    )
    job.scratch_dir.mkdir(parents=True, exist_ok=True)
    Path(spec.ass_path).write_text(ass, encoding="utf-8", newline="\n")

    started = time.monotonic()
    measure_ms = 0
    if job.media.has_audio:
        measured = tools.run(
            build_loudnorm_measure_command(spec, ffmpeg=ffmpeg),
            timeout_s=tools.RENDER_TIMEOUT_S,
            what="loudness measure",
        )
        measurement = parse_loudnorm_report(measured.stderr.decode("utf-8", errors="replace"))
        measure_ms = int((time.monotonic() - started) * 1000)
        if measurement is None:
            log.warning("loudnorm_measure_unreadable")
        spec = replace(spec, loudnorm=measurement)
    tools.run(build_render_command(spec, ffmpeg=ffmpeg), timeout_s=tools.RENDER_TIMEOUT_S, what="render")
    render_ms = int((time.monotonic() - started) * 1000)

    scratch_output = Path(spec.output_path)
    media = tools.probe(scratch_output, ffprobe)
    expected = spec.output_duration_s
    if not media.has_video or not media.has_audio:
        raise tools.FfmpegError("rendered file lacks a video or an audio stream")
    if (media.width, media.height) != (params.width, params.height):
        raise tools.FfmpegError(f"rendered file is {media.width}x{media.height}")
    if abs(media.duration_s - expected) > DURATION_TOLERANCE_S:
        raise tools.FfmpegError(f"rendered file is {media.duration_s:.2f} s long, expected {expected:.2f} s")
    _move_into_place(scratch_output, job.output_path)
    size = job.output_path.stat().st_size
    log.info(
        "clip_render_done",
        mode=job.mode,
        layout=job.layout.kind,
        render_ms=render_ms,
        measure_ms=measure_ms,
        duration_s=round(media.duration_s, 2),
        file_size_bytes=size,
        bleeps=len(bleeps),
        nvenc=uses_nvenc(params, job.mode, nvenc_available=nvenc),
    )
    return RenderOutcome(
        output_path=job.output_path,
        media=media,
        clip_duration_s=duration,
        render_ms=render_ms,
        measure_ms=measure_ms,
        file_size_bytes=size,
        bleeps=bleeps,
        profanity_count=len(profane),
        sfx_times_s=sfx_times,
        nvenc=uses_nvenc(params, job.mode, nvenc_available=nvenc),
    )


def transcription_modes(mode: str) -> tuple[Mode, ...]:
    """Local first for a normal render, hosted first for an express one; the other is the fallback."""
    return ("hosted", "local") if mode == "express" else ("local", "hosted")


def _windows(item: RenderInput, cfg: FactoryConfig, file_s: float) -> tuple[ClipWindow, ClipWindow]:
    """The proposed window and the padded window, both inside the file (file time)."""
    start = min(item.window_start_s, max(0.0, file_s - 0.1))
    proposed = ClipWindow(start, max(min(item.window_end_s, file_s), start + 0.1))
    if item.pad_start_s is not None and item.pad_end_s is not None:
        padded = ClipWindow(
            max(0.0, min(item.pad_start_s, proposed.start_s)),
            min(file_s, max(item.pad_end_s, proposed.end_s)),
        )
    else:
        padded = padded_window(proposed, cfg.context_pad_s, file_s)
    return proposed, padded


def produce(
    item: RenderInput,
    cfg: FactoryConfig,
    *,
    deps: Deps,
    scratch_dir: Path,
    output_path: Path,
    series_no: int,
    choose_writing: WritingChooser,
    choose_layout: LayoutChooser,
    on_writing: WritingHook | None = None,
    clip_id: str | None = None,
) -> Produced:
    """Transcribe, write, trim, analyse and render one clip. Touches no database table itself.

    `choose_writing` returns the clip writer's reply (P2, or values typed by hand); `choose_layout` gets
    the source info and the time of the middle of the final window. `on_writing` is called with the reply
    before the render starts. Raises `ClipSkippedError` when there is no speech or the writer rejects.
    """
    timings: dict[str, int] = {}

    def timed(name: str, started: float) -> None:
        timings[name] = int((time.monotonic() - started) * 1000)

    assets = deps.assets or load_assets(cfg.params.font)
    media = tools.probe(item.source_path, deps.ffprobe)
    if not media.has_video or media.width < 2 or media.height < 2 or media.duration_s <= 0:
        raise tools.FfmpegError(f"{item.source_path.name} has no usable video stream")
    proposed, padded = _windows(item, cfg, media.duration_s)
    offset = padded.start_s
    ai_lane: BudgetKind = "express" if item.mode == "express" else "vod"

    started = time.monotonic()
    if media.has_audio:
        transcript = transcripts.transcribe_window(
            item.source_path,
            padded,
            modes=transcription_modes(item.mode),
            scratch_dir=scratch_dir,
            transcriber=deps.transcriber or transcripts.default_transcriber(cfg.whisper_model),
            ai_lane=ai_lane,
            ai_client=deps.ai_client,
            ref_id=clip_id,
            ffmpeg=deps.ffmpeg,
        )
    else:
        transcript = transcripts.WindowTranscript(language="en", original_language=None, words=())
    timed("transcribe", started)
    if not transcript.words:
        raise ClipSkippedError("no_speech")

    lines = group_lines(transcript.words)
    floor = item.spike_floor_s if item.spike_floor_s is not None else proposed.start_s
    writer_input = writer.WriterInput(
        creator=item.creator,
        platform=item.platform,
        stream_title=item.stream_title,
        lines=lines,
        window_s=padded.duration_s,
        proposed=ClipWindow(proposed.start_s - offset, proposed.end_s - offset),
        spike_floor_s=floor - offset,
        min_len_s=cfg.min_len_s,
        max_len_s=cfg.max_len_s,
        chat=tuple(
            ChatLine(t_s=line.t_s - offset, text=line.text, user=line.user)
            for line in item.chat
            if padded.start_s <= line.t_s <= padded.end_s
        ),
        verdict=item.verdict,
        profanity_hint=writer.profanity_hint(transcript.words, assets.profanity()),
        clip_id=clip_id,
    )
    started = time.monotonic()
    writing = choose_writing(writer_input)
    timed("write", started)
    if on_writing is not None:
        on_writing(writing, transcript)
    if writing.reject:
        raise ClipSkippedError(f"writer: {writing.reject_reason or 'rejected'}")

    window_w = writer.final_window(writing, writer_input)
    window = ClipWindow(window_w.start_s + offset, window_w.end_s + offset)
    clip_text = clip_lines(lines, window_w)
    if not clip_text:
        raise ClipSkippedError("no_speech")

    started = time.monotonic()
    analysis: music.MusicAnalysis | None = None
    if not deps.skip_music:
        try:
            analysis = music.analyse(
                item.source_path,
                window,
                detector=deps.music_detector,
                has_audio=media.has_audio,
                ffmpeg=deps.ffmpeg,
            )
        except music.MusicDetectorUnavailableError as exc:
            get_logger("factory").warning("music_detector_unavailable", error=str(exc)[:200])
    timed("music", started)

    started = time.monotonic()
    layout = choose_layout(media, (window.start_s + window.end_s) / 2)
    timed("layout", started)

    started = time.monotonic()
    outcome = render_file(
        RenderJob(
            source_path=item.source_path,
            media=media,
            window=window,
            lines=clip_text,
            layout=layout,
            params=cfg.params,
            assets=assets,
            output_path=output_path,
            scratch_dir=scratch_dir,
            series_no=series_no,
            mode="express" if item.mode == "express" else "normal",
            lane=item.lane,
            creator=item.creator,
            hook=writing.hook,
            keywords=writing.keyword_map(),
            duck=music.duck_spans(analysis, cfg.max_music_ratio, window.duration_s),
        ),
        ffmpeg=deps.ffmpeg,
        ffprobe=deps.ffprobe,
    )
    timed("render", started)
    return Produced(
        transcript=transcript,
        writing=writing,
        window=window,
        lines=tuple(clip_text),
        layout=layout,
        music=analysis,
        outcome=outcome,
        timings_ms=timings,
    )


# ── Database orchestration ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClipRow:
    """The clip row a render works on."""

    id: str
    series_no: int
    status: str
    created: bool


def scratch_dir_for(clip_id: str) -> Path:
    """`$DATA_DIR/render/<clip_id>/`: deleted when the render ends, whatever the result."""
    return env().DATA_DIR / "render" / clip_id


def ready_path_for(clip_id: str) -> Path:
    """`$DATA_DIR/ready/<clip_id>.mp4`: the finished clip, kept until it is posted."""
    return env().DATA_DIR / "ready" / f"{clip_id}.mp4"


def find_clip(item: RenderInput) -> ClipRow | None:
    """The clip row of an earlier call for the same `clip_id` or `spike_id`, if there is one."""
    if item.clip_id is not None:
        row = db.fetch_one("select id, series_no, status from clips where id = %s", (item.clip_id,))
    elif item.spike_id is not None:
        row = db.fetch_one(
            "select id, series_no, status from clips where spike_id = %s order by created_at limit 1",
            (item.spike_id,),
        )
    else:
        row = None
    if row is None:
        return None
    return ClipRow(str(row["id"]), int(row["series_no"] or 0), str(row["status"]), created=False)


def claim_clip(item: RenderInput, *, force: bool = False) -> ClipRow:
    """Find or create the clip row for this input, in one transaction.

    A new row takes the next number of `clip_series_seq` and starts in status `rendering`. A row that
    exists keeps its id and its series number, so a retried job never takes a second number. `force`
    puts an existing row back to `rendering`.
    """
    if item.creator_id is None:
        raise ValueError("render_clip needs creator_id")
    with db.transaction():
        key = item.clip_id or item.spike_id
        if key is not None:
            db.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"clip.render:{key}",))
        existing = find_clip(item)
        if existing is not None:
            if force and existing.status != "rendering":
                db.execute(
                    "update clips set status = 'rendering', reject_reason = null where id = %s",
                    (existing.id,),
                )
                return replace(existing, status="rendering")
            return existing
        row = db.fetch_one(
            """
            insert into clips
              (id, series_no, spike_id, creator_id, source_platform, source_id, source_url, source_type,
               lane, start_s, end_s, gate_profile, status)
            values
              (coalesce(%s::uuid, gen_random_uuid()), nextval('clip_series_seq'), %s, %s, %s, %s, %s, %s,
               %s, %s, %s, %s, 'rendering')
            returning id, series_no
            """,
            (
                item.clip_id,
                item.spike_id,
                item.creator_id,
                item.platform,
                item.source_id,
                item.source_url,
                item.source_type,
                item.lane,
                item.source_offset_s + item.window_start_s,
                item.source_offset_s + item.window_end_s,
                "express" if item.lane == "spike" else "hourly",
            ),
        )
        assert row is not None
        clip_id, series_no = str(row["id"]), int(row["series_no"])
        if item.spike_id is not None:
            db.execute(
                "update spikes set status = 'rendering', clip_id = %s where id = %s",
                (clip_id, item.spike_id),
            )
        events.emit(
            "clip.render_started",
            entity_type="clip",
            entity_id=clip_id,
            data={
                "series_no": series_no,
                "spike_id": item.spike_id,
                "lane": item.lane,
                "mode": item.mode,
                "creator": item.creator,
            },
        )
        return ClipRow(clip_id, series_no, "rendering", created=True)


def _store_writing(clip_id: str, writing: ClipWriting, transcript: transcripts.WindowTranscript) -> None:
    """Save the P2 outputs on the clip row as soon as they exist (also for a clip that is then rejected)."""
    columns = writer.clip_columns(writing)
    columns["original_language"] = transcript.original_language
    assignments = ", ".join(f"{name} = %s" for name in columns)
    db.execute(f"update clips set {assignments} where id = %s", (*columns.values(), clip_id))  # noqa: S608


def _reject(clip_id: str, item: RenderInput, reason: str, *, failed: bool = False) -> None:
    """Close the clip row as rejected and tell the spike row what happened."""
    with db.transaction():
        db.execute(
            "update clips set status = 'rejected', reject_reason = %s where id = %s and status = 'rendering'",
            (reason[:500], clip_id),
        )
        if item.spike_id is not None:
            db.execute(
                "update spikes set status = %s where id = %s and status = 'rendering'",
                ("failed" if failed else "rejected", item.spike_id),
            )
        events.emit(
            "clip.render_failed" if failed else "clip.rejected",
            level="error" if failed else "info",
            entity_type="clip",
            entity_id=clip_id,
            message=reason[:500],
            data={"spike_id": item.spike_id, "stage": "factory"},
        )


def mark_failed(item: RenderInput, error: str) -> str | None:
    """A render that failed for good: the clip row is closed as rejected (`render_failed: ...`), the spike
    as failed, and the owner is told. Returns the clip id, or None when no row exists."""
    row = find_clip(item)
    if row is None:
        return None
    if row.status == "rendering":
        _reject(row.id, item, f"render_failed: {error}", failed=True)
        notify.send("warn", "Clip render failed", f"clip {row.id} ({item.creator}): {error[:300]}")
    return row.id


def _finish(clip_id: str, item: RenderInput, cfg: FactoryConfig, produced: Produced) -> None:
    outcome = produced.outcome
    analysis = produced.music
    words = [word for line in produced.lines for word in line.words]
    with db.transaction():
        db.execute(
            """
            update clips set
              start_s = %s, end_s = %s, duration_s = %s, transcript = %s, music_ratio = %s,
              profanity_count = %s, gate_profile = %s, local_path = %s, file_size_bytes = %s,
              render_ms = %s, status = 'gating'
            where id = %s
            """,
            (
                round(item.source_offset_s + produced.window.start_s, 3),
                round(item.source_offset_s + produced.window.end_s, 3),
                round(outcome.clip_duration_s, 3),
                Jsonb(transcripts.stored_transcript(words)),
                round(analysis.ratio, 4) if analysis is not None else None,
                outcome.profanity_count,
                cfg.gate_profile,
                str(outcome.output_path),
                outcome.file_size_bytes,
                outcome.render_ms,
                clip_id,
            ),
        )
        if item.spike_id is not None:
            db.execute(
                "update spikes set status = 'clipped', clip_id = %s where id = %s", (clip_id, item.spike_id)
            )
        events.emit(
            "clip.rendered",
            entity_type="clip",
            entity_id=clip_id,
            data={
                "spike_id": item.spike_id,
                "lane": item.lane,
                "mode": item.mode,
                "layout": produced.layout.kind,
                "duration_s": round(outcome.clip_duration_s, 3),
                "file_duration_s": round(outcome.media.duration_s, 3),
                "render_ms": outcome.render_ms,
                "timings_ms": produced.timings_ms,
                "file_size_bytes": outcome.file_size_bytes,
                "transcriber": f"{produced.transcript.provider}:{produced.transcript.model}",
                "translated": produced.transcript.translated,
                "music": analysis.as_json() if analysis is not None else None,
                "bleeps": [[round(span.start_s, 3), round(span.end_s, 3)] for span in outcome.bleeps],
                "sfx_times_s": list(outcome.sfx_times_s),
                "nvenc": outcome.nvenc,
            },
        )


def upload_clip(clip_id: str, *, storage: clip_storage.ClipStorage | None = None) -> str:
    """Upload the finished clip to the private bucket and save the key in `clips.storage_path`.

    Called after the gate passes (docs/09 section 1). A clip that already has a storage path is not
    uploaded again. Returns the storage key.
    """
    row = db.fetch_one("select local_path, storage_path, created_at from clips where id = %s", (clip_id,))
    if row is None:
        raise ValueError(f"clip {clip_id} does not exist")
    if row["storage_path"]:
        return str(row["storage_path"])
    if not row["local_path"] or not Path(str(row["local_path"])).is_file():
        raise FileNotFoundError(f"clip {clip_id} has no local file to upload")
    store = storage or clip_storage.ClipStorage()
    key = store.upload(Path(str(row["local_path"])), clip_storage.clip_key(clip_id, row["created_at"]))
    db.execute("update clips set storage_path = %s where id = %s", (key, clip_id))
    events.emit("clip.uploaded", entity_type="clip", entity_id=clip_id, data={"storage_path": key})
    return key


def render_clip(item: RenderInput, *, deps: Deps | None = None, force: bool = False) -> str:
    """Make the clip for `item` and return its clip id.

    The row goes `rendering` → `gating` (file in `$DATA_DIR/ready/<clip_id>.mp4`), or `rendering` →
    `rejected` when there is no speech or the clip writer rejects the clip. Calling it again for the same
    `clip_id` / `spike_id` returns the same id: a finished clip is not rendered twice, an unfinished one is
    rendered again under its first series number. Any other error is raised with the row left in
    `rendering`, so the job can be retried; `mark_failed` closes it after the last attempt.
    """
    deps = deps or Deps()
    log = get_logger("factory")
    row = claim_clip(item, force=force)
    with structlog.contextvars.bound_contextvars(clip_id=row.id, spike_id=item.spike_id):
        if row.status != "rendering":
            log.info("clip_render_skipped", reason="already_done", status=row.status)
            return row.id
        cfg = FactoryConfig.load(item.lane)
        scratch = scratch_dir_for(row.id)
        ai_lane: BudgetKind = "express" if item.mode == "express" else "vod"

        def choose_writing(writer_input: writer.WriterInput) -> ClipWriting:
            return writer.write(writer_input, lane=ai_lane, client=deps.ai_client)

        def choose_layout(media: tools.MediaInfo, time_s: float) -> Layout:
            return facecam.get_layout(
                item.creator_id,
                item.source_path,
                time_s,
                media.width,
                media.height,
                lane=ai_lane,
                ref_id=row.id,
                client=deps.ai_client,
                ffmpeg=deps.ffmpeg,
            )

        try:
            scratch.mkdir(parents=True, exist_ok=True)
            produced = produce(
                item,
                cfg,
                deps=deps,
                scratch_dir=scratch,
                output_path=ready_path_for(row.id),
                series_no=row.series_no,
                choose_writing=choose_writing,
                choose_layout=choose_layout,
                on_writing=lambda writing, transcript: _store_writing(row.id, writing, transcript),
                clip_id=row.id,
            )
        except ClipSkippedError as skipped:
            log.info("clip_skipped", reason=skipped.reason)
            _reject(row.id, item, skipped.reason)
            return row.id
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        _finish(row.id, item, cfg, produced)
        if item.upload:
            upload_clip(row.id, storage=deps.storage)
        return row.id


# ── Job handlers ─────────────────────────────────────────────

AfterRender = Callable[[str, jobs.Job], None]
# What runs after a clip reaches `gating`, per job type (the gate, then posting for the spike lane). Set by
# the milestones that own those steps with `set_after_render`.
_after_render: dict[str, AfterRender] = {}


def set_after_render(job_type: str, callback: AfterRender | None) -> None:
    """Register (or clear, with None) the step that follows a successful render of `job_type`."""
    if callback is None:
        _after_render.pop(job_type, None)
    else:
        _after_render[job_type] = callback


def next_budget_reset(now: datetime | None = None) -> datetime:
    """When a job stopped by the daily AI budget may run again: shortly after the next 00:00 UTC."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(seconds=BUDGET_RETRY_MARGIN_S)


def _defer(job: jobs.Job) -> None:
    """Queue the same work again for after the budget reset; the current job then ends as done."""
    run_after = next_budget_reset()
    jobs.enqueue(
        "express" if job.queue == "express" else "normal",
        job.type,
        job.payload,
        priority=job.priority,
        run_after=run_after,
        dedupe_key=f"{job.dedupe_key}:budget" if job.dedupe_key else None,
        max_attempts=job.max_attempts,
    )
    events.emit(
        "clip.render_deferred",
        level="warn",
        entity_type="job",
        entity_id=job.id,
        message="ai.daily_budget_usd is used up",
        data={"run_after": run_after.isoformat(), "spike_id": job.payload.get("spike_id")},
    )


def _handle(job: jobs.Job, default_mode: str) -> None:
    item = RenderInput.from_payload(job.payload, default_mode=default_mode)
    try:
        clip_id = render_clip(item)
    except BudgetExceededError:
        _defer(job)
        return
    except Exception as exc:
        if job.attempts >= job.max_attempts:
            mark_failed(item, f"{type(exc).__name__}: {exc}")
        raise
    callback = _after_render.get(job.type)
    if callback is not None:
        row = db.fetch_one("select status from clips where id = %s", (clip_id,))
        if row is not None and row["status"] == "gating":
            callback(clip_id, job)


def handle_clip_render(job: jobs.Job) -> None:
    """Job `clip.render` (queue `normal`): local transcription, `factory.preset_normal`, two-pass loudness."""
    _handle(job, "normal")


def handle_express_render(job: jobs.Job) -> None:
    """Job `express.render` (queue `express`): hosted transcription, `factory.preset_express`."""
    _handle(job, "express")


def register_handlers(job_types: Sequence[str] = (JOB_CLIP_RENDER, JOB_EXPRESS_RENDER)) -> None:
    """Register the factory's job handlers with the worker (safe to call more than once)."""
    from sixstars.services import worker

    handlers: dict[str, jobs.Handler] = {
        JOB_CLIP_RENDER: handle_clip_render,
        JOB_EXPRESS_RENDER: handle_express_render,
    }
    for job_type in job_types:
        if worker.HANDLERS.get(job_type) is not handlers[job_type]:
            worker.register(job_type, handlers[job_type])

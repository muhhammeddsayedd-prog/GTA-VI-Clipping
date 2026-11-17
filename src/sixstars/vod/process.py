"""Processes one video end to end: chat replay, spike detection, section download and hand-off to the clip
factory. Implemented in M08.

Steps of `process_video` (job types `vod.process` and `replay.scan`):

1. Gates: creator still allowed, `vod.paused_until`, `control.emergency_stop`, downloads of the platform
   not blocked a moment ago, fewer than `vod.max_concurrent` videos in work. Then `videos.status` becomes
   `processing`.
2. Chat replay (kept as JSON Lines under `DATA_DIR/chat`), `chat_files` row, replay scan with
   `spikes.end_guard_s`, best `vod.max_spikes_per_video` spikes stored. After a live stream, spikes on
   ranges the live lane already clipped are left out.
3. Caption track (YouTube, no media), so the confirmation prompt can read what was said.
4. Confirmation (P1) in batches of ten; each batch is applied before the next is asked.
5. Transcript fallback (P3) when the chat gave fewer than `vod.fallback_below_spikes` confirmed spikes.
6. For each confirmed spike: download the padded window only, then one `clip.render` job.
7. `videos.status = done`; a `vod.cleanup` job removes each window file once its render has ended.

A video that cannot go on for a reason that is not its fault (downloads blocked, AI budget used up,
emergency stop, lane paused, too many videos in work) is *deferred*: status back to `new`, the job queued
again for later, no attempt used up. Every step can be repeated: a retried or duplicated job finds the
stored chat, spikes, verdicts, window files and render jobs and continues after them.
"""

import contextvars
import shutil
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, get_args

import structlog

from sixstars import events, jobs, notify, settings
from sixstars.ai import calls, spike_confirm
from sixstars.ai.calls import BudgetExceededError, ChatLine, SpikeContext
from sixstars.ai.schemas import MomentType, SpikeVerdict
from sixstars.chat import twitch_replay, youtube_replay
from sixstars.chat.models import ReplayChat, read_jsonl, write_jsonl
from sixstars.factory.commands import input_from_spike
from sixstars.factory.pipeline import next_budget_reset
from sixstars.logging import get_logger
from sixstars.spikes import DetectedSpike, SpikeConfig, analyse_replay, persist_spike
from sixstars.spikes.buckets import tokenise
from sixstars.vod import captions, fallback, sections, store, ytdlp
from sixstars.vod.captions import Track
from sixstars.vod.common import DeferredError, Deps, VideoGoneError
from sixstars.vod.config import VodConfig
from sixstars.vod.ranges import Range, clipped_offsets, split_clipped
from sixstars.vod.sections import Downloaded
from sixstars.vod.store import SpikeRow, Video
from sixstars.vod.ytdlp import Access, DownloadBlockedError, VideoUnavailableError, YtDlpError

JOB_VOD_PROCESS: Final = "vod.process"
JOB_REPLAY_SCAN: Final = "replay.scan"
JOB_CLIP_RENDER: Final = "clip.render"
JOB_CLEANUP: Final = "vod.cleanup"
REQUEUE_SUFFIX: Final = ":again"
RENDER_LANE: Final = "hourly"  # replay spikes never go to the spike lane: an S is routed like an A
RENDER_MODE: Final = "normal"
BLOCKED_TITLE: Final = "Video downloads are blocked"
FAILED_TITLE: Final = "VOD processing failed"
# Shape of the confirmation prompt, not runtime settings.
CONFIRMED_STATUSES: Final = ("confirmed", "rendering", "clipped")
TOP_TOKENS: Final = 5
TRANSCRIPT_MAX_CHARS: Final = 1500

Status = Literal["done", "deferred", "skipped", "failed", "ignored"]


class SectionsFailedError(RuntimeError):
    """Window downloads of a video failed for a reason worth another attempt."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one call of `process_video` did."""

    status: Status
    reason: str = ""
    run_after: datetime | None = None
    kind: str = ""
    source: str = ""
    messages: int = 0
    spikes: int = 0
    confirmed: int = 0
    downloaded: int = 0
    queued: int = 0
    timings_s: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON values, for events and the CLI."""
        return {
            "status": self.status,
            "reason": self.reason,
            "run_after": self.run_after.isoformat() if self.run_after else None,
            "kind": self.kind,
            "source": self.source,
            "messages": self.messages,
            "spikes": self.spikes,
            "confirmed": self.confirmed,
            "downloaded": self.downloaded,
            "queued": self.queued,
            "timings_s": self.timings_s,
        }


@dataclass(slots=True)
class _Stats:
    source: str = ""
    messages: int = 0
    spikes: int = 0
    confirmed: int = 0
    downloaded: int = 0
    queued: int = 0
    timings_s: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.timings_s[name] = round(self.timings_s.get(name, 0.0) + time.monotonic() - started, 1)

    def outcome(self, status: Status, **extra: Any) -> Outcome:
        return Outcome(
            status,
            source=self.source,
            messages=self.messages,
            spikes=self.spikes,
            confirmed=self.confirmed,
            downloaded=self.downloaded,
            queued=self.queued,
            timings_s=dict(self.timings_s),
            **extra,
        )


# ── Pure helpers ─────────────────────────────────────────────


def best_first(spikes: Sequence[DetectedSpike], limit: int) -> list[DetectedSpike]:
    """The `limit` best spikes: by score, then by message count."""
    ordered = sorted(spikes, key=lambda spike: (spike.score_z, spike.msgs), reverse=True)
    return ordered[: max(0, limit)]


def top_tokens(row: SpikeRow, count: int = TOP_TOKENS) -> list[str]:
    """The most common words chat used from the peak onwards (each counted once per message)."""
    words: Counter[str] = Counter()
    for entry in row.chat_sample:
        offset = entry.get("t_rel")
        if isinstance(offset, int | float) and offset >= 0:
            words.update(set(tokenise(str(entry.get("text", "")))))
    return [word for word, _ in words.most_common(count)]


def spike_context(video: Video, row: SpikeRow, track: Track | None) -> SpikeContext:
    """What the confirmation prompt sees about one stored spike. The transcript is what the caption track
    says across the padded window, when there is a track."""
    chat = tuple(
        ChatLine(t_s=float(entry["t_rel"]), text=str(entry.get("text", "")), user=None)
        for entry in row.chat_sample
        if isinstance(entry.get("t_rel"), int | float)
    )
    transcript = (
        captions.text_between(track.words, row.pad_start_s, row.pad_end_s, TRANSCRIPT_MAX_CHARS)
        if track is not None
        else ""
    )
    return SpikeContext(
        spike_id=row.id,
        creator=video.creator_handle,
        platform=video.platform,
        stream_title=video.title,
        score_z=row.score_z,
        msgs=row.msgs,
        uniq=row.uniq,
        top_tokens=top_tokens(row),
        chat=chat,
        transcript=transcript or None,
    )


def verdict_from_row(row: SpikeRow) -> SpikeVerdict | None:
    """The stored verdict of a confirmed spike (`claude_reason` is "<type>: <one line>"), for the clip
    writer. None when the row has no verdict."""
    if row.confidence is None or not row.reason:
        return None
    kind, separator, line = row.reason.partition(": ")
    moment_type: MomentType = "other"
    for known in get_args(MomentType):
        if separator and kind == known:
            moment_type = known
    one_line = " ".join((line if separator else row.reason).split()[:15])
    return SpikeVerdict(
        is_moment=True,
        confidence=min(1.0, max(0.0, row.confidence)),
        moment_type=moment_type,
        one_line=one_line or "moment",
        noise_reason=None,
    )


def requeue_key(dedupe_key: str | None) -> str | None:
    """The dedupe key of the job that replaces a deferred one. The deferred job is still running when its
    successor is queued, so the key must differ; it flips between `<key>` and `<key>:again`."""
    if dedupe_key is None:
        return None
    if dedupe_key.endswith(REQUEUE_SUFFIX):
        return dedupe_key[: -len(REQUEUE_SUFFIX)]
    return dedupe_key + REQUEUE_SUFFIX


def blocked_message(platform: str, waiting: int, backoff_s: float, access: Access, detail: str) -> str:
    """The body of the "downloads are blocked" notification: what happens and what fixes it."""
    minutes = max(1, round(backoff_s / 60))
    return (
        f"{platform} refuses video downloads from this server: {detail[:200]}\n"
        f"{waiting} video(s) are waiting. They are not failed; they are tried again every {minutes} min "
        f"and the backlog drains by itself once downloads work.\n"
        f"Fix: set PROXY_URL (a residential proxy) or YOUTUBE_COOKIES_FILE (cookies of a signed-in "
        f"account) in the server's .env and restart the workers. Now: {access.described}."
    )


# ── Steps ────────────────────────────────────────────────────


def _blocked(video: Video, cfg: VodConfig, access: Access, detail: str, deps: Deps) -> NoReturn:
    """The platform refused a download: remember it, tell the owner at most once per interval, defer."""
    store.note_block(video, detail, access.described)
    if store.claim_block_notification(video.platform, cfg.blocked_notify_interval_s):
        notify.send(
            "warn",
            BLOCKED_TITLE,
            blocked_message(
                video.platform, store.waiting_videos(video.platform), cfg.blocked_backoff_s, access, detail
            ),
        )
    raise DeferredError(
        "download_blocked", detail[:300], deps.now() + timedelta(seconds=cfg.blocked_backoff_s)
    )


def _gates(video: Video, cfg: VodConfig, deps: Deps, *, force: bool) -> None:
    """Reasons not to start now. Raises `DeferredError`."""
    now = deps.now()
    paused_until = settings.get_dt("vod.paused_until", None)
    if paused_until is not None and paused_until > now:
        raise DeferredError("paused", "vod.paused_until is set", paused_until)
    if settings.get_bool("control.emergency_stop"):
        raise DeferredError(
            "emergency_stop", "control.emergency_stop is set", now + timedelta(seconds=cfg.stop_retry_s)
        )
    if force:
        return
    last_block = store.last_block(video.platform)
    if last_block is not None:
        retry_at = last_block + timedelta(seconds=cfg.blocked_backoff_s)
        if retry_at > now:
            raise DeferredError("download_blocked", f"{video.platform} downloads were refused", retry_at)
    if store.count_processing(video.id, cfg.processing_stale_s) >= cfg.max_concurrent:
        raise DeferredError(
            "concurrency",
            "vod.max_concurrent videos are in work",
            now + timedelta(seconds=cfg.concurrency_retry_s),
        )


def download_chat(platform: str, source_id: str, raw_path: Path | None = None) -> ReplayChat | None:
    """Download a chat replay with the lane's access rules.

    Same contract as `sixstars.chat.download_replay`, but the cookies file is used only when it exists:
    the chat adapter opens the file named by `YOUTUBE_COOKIES_FILE` without checking, so a path that is
    configured but not present would fail every chat download.
    """
    access = ytdlp.access_for(platform)
    if platform == "twitch":
        return twitch_replay.download(source_id, raw_path, proxy=access.proxy)
    return youtube_replay.download(source_id, proxy=access.proxy, cookies_file=access.cookies_file)


def _load_chat(video: Video, cfg: VodConfig, access: Access, deps: Deps) -> ReplayChat | None:
    """The chat replay of the video: from the file of an earlier attempt, else downloaded and saved.
    None when the video has none (a YouTube upload, or a stream whose replay was switched off)."""
    log = get_logger("vod")
    if video.has_chat is False:
        return None
    path = store.chat_path(video.platform, video.source_id)
    if path.is_file():
        log.info("chat_reused", path=str(path))
        return ReplayChat(
            platform="twitch" if video.platform == "twitch" else "youtube",
            source_id=video.source_id,
            messages=read_jsonl(path),
            duration_s=video.duration_s,
            title=video.title or None,
        )
    download = deps.chat_downloader or download_chat
    try:
        replay = download(video.platform, video.source_id, None)
        if replay is None and video.platform == "youtube":
            # Right after a stream ends the replay may not be there yet: one more try before falling back.
            deps.sleep(cfg.chat_retry_delay_s)
            replay = download(video.platform, video.source_id, None)
    except youtube_replay.VideoUnavailableError as exc:
        failure = ytdlp.classify_failure(1, str(exc))
        if failure.kind == "blocked":
            _blocked(video, cfg, access, failure.detail, deps)
        if failure.kind == "unavailable":
            raise VideoGoneError(str(exc)) from exc
        raise
    if replay is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    write_jsonl(partial, replay.messages)
    partial.replace(path)
    return replay


def _scan(video: Video, replay: ReplayChat, cfg: VodConfig, stats: _Stats) -> None:
    """Find the spikes of a chat replay and store the best ones (idempotent)."""
    log = get_logger("vod")
    scan = analyse_replay(replay.messages, config=SpikeConfig.from_settings(), end_guard_s=cfg.end_guard_s)
    store.save_chat_file(
        video,
        path=store.chat_path(video.platform, video.source_id),
        messages_count=replay.message_count,
        duration_s=replay.duration_s or video.duration_s,
        baseline_rate=scan.baseline_rate,
        bot_names=sorted(scan.bot_names),
    )
    store.touch(video.id, title=replay.title, duration_s=replay.duration_s)
    clipped = clipped_offsets(video.clipped_ranges, video.session_started_at)
    fresh, already = split_clipped(
        scan.spikes,
        clipped,
        cfg.clipped_range_margin_s,
        window=lambda spike: (spike.window_start, spike.window_end),
    )
    best = best_first(fresh, cfg.max_spikes_per_video)
    for spike in best:
        persist_spike(
            spike, creator_id=video.creator_id, live_session_id=video.live_session_id, video_id=video.id
        )
    stats.messages = replay.message_count
    log.info(
        "vod_chat_scanned",
        messages=replay.message_count,
        counted=scan.counted_messages,
        baseline_rate=scan.baseline_rate,
        bots=len(scan.bot_names),
        found=len(scan.spikes),
        already_clipped=len(already),
        kept=len(best),
    )


def _captions(video: Video, cfg: VodConfig, access: Access, deps: Deps) -> Track | None:
    """The caption track, best effort: only a refusal of this machine or a vanished video stops the job."""
    if video.platform != "youtube":
        return None
    try:
        return captions.fetch(
            video.url,
            store.work_dir(video.id),
            access=access,
            langs=cfg.caption_langs,
            timeout_s=cfg.caption_timeout_s,
            runner=deps.runner,
        )
    except DownloadBlockedError as exc:
        _blocked(video, cfg, access, exc.detail, deps)
    except VideoUnavailableError as exc:
        raise VideoGoneError(exc.detail) from exc
    except YtDlpError as exc:
        get_logger("vod").warning("captions_failed", kind=exc.kind, detail=exc.detail)
        return None


def _confirm(video: Video, track: Track | None, deps: Deps) -> None:
    """Ask P1 about every spike still in status `detected`, ten at a time, storing each batch at once."""
    pending = [
        row for row in store.video_spikes(video.id) if row.status == "detected" and row.mode == "replay"
    ]
    for start in range(0, len(pending), calls.SPIKE_BATCH_SIZE):
        batch = pending[start : start + calls.SPIKE_BATCH_SIZE]
        contexts = [spike_context(video, row, track) for row in batch]
        try:
            verdicts = calls.confirm_spikes(contexts, client=deps.ai_client)
        except BudgetExceededError as exc:
            raise DeferredError("ai_budget", str(exc), next_budget_reset(deps.now())) from exc
        for row, verdict in zip(batch, verdicts, strict=True):
            tier: spike_confirm.Tier = "S" if row.tier == "S" else "A" if row.tier == "A" else "B"
            with structlog.contextvars.bound_contextvars(spike_id=row.id):
                decision = spike_confirm.apply(row.id, tier, verdict)
                get_logger("vod").info(
                    "spike_judged", status=decision.status, tier=decision.tier, confidence=decision.confidence
                )
        store.touch(video.id)


def _fallback(video: Video, cfg: VodConfig, track: Track | None, access: Access, deps: Deps) -> str:
    """Run the transcript fallback once per video. Returns where the transcript came from ("" = not run)."""
    if not cfg.fallback_enabled or store.has_event(video.id, store.EVENT_FALLBACK):
        return ""
    taken = [
        Range(row.window_start_s, row.window_end_s)
        for row in store.video_spikes(video.id)
        if row.status not in ("rejected", "failed")
    ]
    try:
        result = fallback.run(
            video,
            cfg,
            track=track,
            taken=taken,
            access=access,
            deps=deps,
            on_progress=lambda: store.touch(video.id),
        )
    except BudgetExceededError as exc:
        raise DeferredError("ai_budget", str(exc), next_budget_reset(deps.now())) from exc
    except DownloadBlockedError as exc:
        _blocked(video, cfg, access, exc.detail, deps)
    except VideoUnavailableError as exc:
        raise VideoGoneError(exc.detail) from exc
    return result.source


def _queue_render(video: Video, row: SpikeRow, downloaded: Downloaded, cfg: VodConfig) -> bool:
    """Queue the `clip.render` job of one downloaded window. False when one is already queued."""
    item = input_from_spike(
        row.id, downloaded.path, source_offset_s=downloaded.start_s, lane=RENDER_LANE, mode=RENDER_MODE
    )
    item = replace(item, verdict=verdict_from_row(row))
    priority = cfg.priority_for(row.tier)
    job_id = jobs.enqueue(
        "normal", JOB_CLIP_RENDER, item.to_payload(), priority=priority, dedupe_key=f"clip.render:{row.id}"
    )
    if job_id is None:
        return False
    events.emit(
        "spike.render_queued",
        entity_type="spike",
        entity_id=row.id,
        data={
            "job_id": job_id,
            "video_id": video.id,
            "tier": row.tier,
            "priority": priority,
            "section_start_s": downloaded.start_s,
            "section_s": round(downloaded.duration_s, 2),
        },
        service=store.SERVICE,
    )
    return True


def _download_and_queue(video: Video, cfg: VodConfig, access: Access, deps: Deps, stats: _Stats) -> None:
    """Download the padded window of every confirmed spike that has no render job yet, a few at a time,
    and queue its render as soon as the file is there."""
    log = get_logger("vod")
    rows = [
        row
        for row in store.video_spikes(video.id)
        if row.status == "confirmed" and not store.render_job_exists(row.id)
    ]
    rows.sort(key=lambda row: (-cfg.priority_for(row.tier), -row.score_z))
    work: list[tuple[SpikeRow, sections.Section]] = []
    for row in rows:
        try:
            work.append((row, sections.section_for(row.pad_start_s, row.pad_end_s, video.duration_s)))
        except ValueError as exc:
            store.fail_spike(row.id, str(exc))
    if not work:
        return
    out_dir = store.sections_dir(video.id)
    stop = threading.Event()

    def fetch(row: SpikeRow, section: sections.Section) -> Downloaded | YtDlpError | None:
        if stop.is_set():
            return None
        with structlog.contextvars.bound_contextvars(spike_id=row.id):
            try:
                return sections.download_section(
                    video.url,
                    section,
                    out_dir,
                    row.id,
                    access=access,
                    max_height=cfg.max_height,
                    timeout_s=cfg.download_timeout_s,
                    exact_cuts=cfg.section_exact_cuts,
                    runner=deps.runner,
                    ffprobe=deps.ffprobe,
                )
            except YtDlpError as exc:
                return exc

    blocked: YtDlpError | None = None
    gone: YtDlpError | None = None
    failures: list[YtDlpError] = []
    in_a_row = 0
    stopped = False
    with ThreadPoolExecutor(max_workers=cfg.download_workers, thread_name_prefix="vod-section") as pool:
        futures = {
            pool.submit(contextvars.copy_context().run, fetch, row, section): row for row, section in work
        }
        for future in as_completed(futures):
            row, result = futures[future], future.result()
            store.touch(video.id)
            if result is None:
                continue
            if isinstance(result, DownloadBlockedError):
                blocked = blocked or result
                stop.set()
            elif isinstance(result, VideoUnavailableError):
                gone = gone or result
                stop.set()
            elif isinstance(result, YtDlpError):
                failures.append(result)
                in_a_row += 1
                if in_a_row >= cfg.download_failures_max:
                    stop.set()
            else:
                in_a_row = 0
                stats.downloaded += 1
                if stopped or settings.get_bool("control.emergency_stop"):
                    stopped = True
                    stop.set()
                elif _queue_render(video, row, result, cfg):
                    stats.queued += 1
    if blocked is not None:
        _blocked(video, cfg, access, blocked.detail, deps)
    if gone is not None:
        raise VideoGoneError(gone.detail)
    if stopped:
        raise DeferredError(
            "emergency_stop",
            "control.emergency_stop is set",
            deps.now() + timedelta(seconds=cfg.stop_retry_s),
        )
    if failures:
        kinds = [failure.kind for failure in failures]
        log.warning("vod_sections_failed", failed=len(failures), kinds=sections.summarise_failures(kinds))
        if stats.downloaded == 0 and all(kind == "forbidden" for kind in kinds):
            # Every window was refused with 403 and none came through: from a data-centre address this
            # is the same refusal as the bot check, so the video waits instead of failing.
            _blocked(video, cfg, access, failures[-1].detail, deps)
        raise SectionsFailedError(
            f"{len(failures)} window download(s) failed ({sections.summarise_failures(kinds)}): "
            f"{failures[-1].detail}"
        )


# ── One video ────────────────────────────────────────────────


def _run(video: Video, cfg: VodConfig, deps: Deps, stats: _Stats) -> None:
    access = ytdlp.access_for(video.platform)
    with stats.timed("chat"):
        replay = _load_chat(video, cfg, access, deps)
    if replay is not None:
        stats.source = "chat"
        with stats.timed("scan"):
            _scan(video, replay, cfg, stats)
    track: Track | None = None
    captions_tried = False
    pending = any(row.mode == "replay" and row.status == "detected" for row in store.video_spikes(video.id))
    if pending and cfg.confirm_with_captions:
        with stats.timed("captions"):
            track, captions_tried = _captions(video, cfg, access, deps), True
    with stats.timed("confirm"):
        _confirm(video, track, deps)
    confirmed_from_chat = sum(
        row.mode == "replay" and row.status in CONFIRMED_STATUSES for row in store.video_spikes(video.id)
    )
    wanted = confirmed_from_chat < cfg.fallback_below_spikes and cfg.fallback_enabled
    if wanted and not store.has_event(video.id, store.EVENT_FALLBACK):
        with stats.timed("fallback"):
            if not captions_tried:
                track = _captions(video, cfg, access, deps)
            source = _fallback(video, cfg, track, access, deps)
        if source not in ("", "none") and not confirmed_from_chat:
            stats.source = f"transcript ({source})"
    rows = store.video_spikes(video.id)
    stats.spikes = len(rows)
    stats.confirmed = sum(row.status in CONFIRMED_STATUSES for row in rows)
    with stats.timed("download"):
        _download_and_queue(video, cfg, access, deps, stats)


def _defer(video: Video, error: DeferredError) -> None:
    """Put the video back to `new` with the reason. A video that was already waiting for the same kind
    of reason is left alone, so a long wait does not fill the events table."""
    reason = f"{error.kind}: {error.reason}"
    current = store.load_video(video.id)
    if current is not None and current.status == "new" and (current.skip_reason or "").startswith(error.kind):
        return
    store.set_status(
        video.id,
        "new",
        "video.blocked" if error.kind == "download_blocked" else "video.deferred",
        reason=reason,
        level="warn" if error.kind in ("download_blocked", "ai_budget", "transcribe_budget") else "info",
        data={"kind": error.kind, "run_after": error.run_after.isoformat()},
    )


def _remove_scratch(video_id: str) -> None:
    """Delete the scratch files of an attempt, and the video's folder when nothing else is in it."""
    shutil.rmtree(store.work_dir(video_id), ignore_errors=True)
    for folder in (store.sections_dir(video_id), store.video_dir(video_id)):
        with suppress(OSError):
            folder.rmdir()


def _queue_cleanup(video_id: str, cfg: VodConfig, deps: Deps) -> None:
    if store.sections_dir(video_id).is_dir():
        jobs.enqueue(
            "normal",
            JOB_CLEANUP,
            {"video_id": video_id},
            priority=0,
            run_after=deps.now() + timedelta(seconds=cfg.cleanup_interval_s),
            dedupe_key=f"{JOB_CLEANUP}:{video_id}",
        )


def give_up(video_id: str, error: str, *, deps: Deps | None = None) -> str:
    """Close a video whose last attempt failed. Returns the final status.

    Confirmed spikes that never got a window are marked failed. The video ends as `done` when at least
    one render was queued for it (the clips are on their way), else as `failed` with a notification.
    """
    deps = deps or Deps()
    cfg = VodConfig.load()
    queued = 0
    for row in store.video_spikes(video_id):
        if store.render_job_exists(row.id):
            queued += 1
        elif row.status == "confirmed":
            store.fail_spike(row.id, error)
    status = "done" if queued else "failed"
    changed = store.set_status(
        video_id,
        status,
        f"video.{status}",
        reason=error,
        level="warn" if queued else "error",
        data={"queued": queued, "partial": bool(queued)},
    )
    if changed and not queued:
        notify.send("warn", FAILED_TITLE, f"video {video_id}: {error[:300]}")
    _remove_scratch(video_id)
    _queue_cleanup(video_id, cfg, deps)
    return status


def process_video(
    video_id: str,
    *,
    deps: Deps | None = None,
    final_attempt: bool = False,
    force: bool = False,
) -> Outcome:
    """Process one video and say what happened.

    Returns an `Outcome`: `done`, `deferred` (try again at `run_after`; not a failure), `skipped` (the
    creator forbids clipping), `failed` (the video is gone) or `ignored` (unknown, finished, or another
    worker has it). Any other problem is raised so the job is retried; with `final_attempt` the video is
    closed first (`give_up`). `force` skips the blocked-downloads and concurrency gates and takes over a
    video that looks in work (the CLI uses it).
    """
    with structlog.contextvars.bound_contextvars(video_id=video_id):
        return _process(video_id, deps or Deps(), final_attempt=final_attempt, force=force)


def _process(video_id: str, deps: Deps, *, final_attempt: bool, force: bool) -> Outcome:
    log = get_logger("vod")
    video = store.load_video(video_id)
    if video is None:
        log.error("vod_video_not_found")
        return Outcome("ignored", "video row not found")
    if video.status not in store.OPEN_STATUSES:
        return Outcome("ignored", f"video is already {video.status}")
    if video.forbidden:
        reason = f"creator tier is {video.creator_tier}"
        store.set_status(video.id, "skipped", "video.skipped", reason=reason)
        return Outcome("skipped", reason)
    cfg = VodConfig.load()
    stats = _Stats()
    try:
        _gates(video, cfg, deps, force=force)
        if not store.claim(video.id, 0.0 if force else cfg.processing_stale_s):
            return Outcome("ignored", "another worker is processing this video")
        log.info(
            "vod_started", platform=video.platform, source_id=video.source_id, attempt=video.attempts + 1
        )
        _run(video, cfg, deps, stats)
    except DeferredError as deferred:
        _defer(video, deferred)
        _remove_scratch(video.id)
        _queue_cleanup(video.id, cfg, deps)
        log.warning(
            "vod_deferred",
            kind=deferred.kind,
            reason=deferred.reason,
            run_after=deferred.run_after.isoformat(),
        )
        return stats.outcome(
            "deferred", reason=deferred.reason, kind=deferred.kind, run_after=deferred.run_after
        )
    except VideoGoneError as gone:
        give_up(video.id, f"video unavailable: {gone}", deps=deps)
        log.warning("vod_video_gone", reason=str(gone))
        return stats.outcome("failed", reason=str(gone), kind="unavailable")
    except Exception as exc:
        if final_attempt:
            give_up(video.id, f"{type(exc).__name__}: {exc}", deps=deps)
        else:
            # Back to `new` so the retry of this job (or the next one) can take the video again.
            store.set_status(
                video.id,
                "new",
                "video.retry",
                reason=f"retry: {type(exc).__name__}: {exc}",
                level="warn",
                only_from=("processing",),
            )
            _remove_scratch(video.id)
        raise
    outcome = stats.outcome("done")
    store.set_status(video.id, "done", "video.done", data=outcome.to_dict())
    _remove_scratch(video.id)
    _queue_cleanup(video.id, cfg, deps)
    log.info("vod_done", **{key: value for key, value in outcome.to_dict().items() if key != "status"})
    return outcome


# ── Job handlers ─────────────────────────────────────────────

_deps_factory: Callable[[], Deps] = Deps


def video_id_for(payload: dict[str, Any]) -> str | None:
    """The video a job is about: `video_id`, or the video of `live_session_id` (job `replay.scan`)."""
    video_id = payload.get("video_id")
    if video_id:
        return str(video_id)
    session_id = payload.get("live_session_id")
    return store.video_for_live_session(str(session_id)) if session_id else None


def requeue(job: jobs.Job, run_after: datetime) -> int | None:
    """Queue the same work again for `run_after`; the current job then ends as done."""
    return jobs.enqueue(
        "express" if job.queue == "express" else "normal",
        job.type,
        job.payload,
        priority=job.priority,
        run_after=run_after,
        dedupe_key=requeue_key(job.dedupe_key),
        max_attempts=job.max_attempts,
    )


def handle(job: jobs.Job, deps: Deps | None = None) -> Outcome:
    """Run one `vod.process` or `replay.scan` job."""
    log = get_logger("vod")
    video_id = video_id_for(job.payload)
    if video_id is None:
        log.error("vod_job_without_video", job_type=job.type)
        return Outcome("ignored", "the job names no video")
    outcome = process_video(
        video_id, deps=deps or _deps_factory(), final_attempt=job.attempts >= job.max_attempts
    )
    if outcome.status == "deferred" and outcome.run_after is not None:
        requeue(job, outcome.run_after)
    elif outcome.status == "ignored":
        log.info("vod_job_ignored", video_id=video_id, reason=outcome.reason)
    return outcome


def handle_vod_process(job: jobs.Job) -> None:
    """Job `vod.process` (queue `normal`): payload `{"video_id": <videos.id>, ...}`."""
    handle(job)


def handle_replay_scan(job: jobs.Job) -> None:
    """Job `replay.scan` (queue `normal`): payload `{"video_id": ...}` or `{"live_session_id": ...}`.
    Same work as `vod.process`; ranges the live lane already clipped are left out."""
    handle(job)

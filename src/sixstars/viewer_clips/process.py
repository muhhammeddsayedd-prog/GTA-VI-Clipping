"""Job `viewer_clip.process` (queue `normal`, payload `{"viewer_clip_id": <viewer_clips.id>}`): download
one Twitch clip, render it through the clip factory on the hourly lane, hand it to the quality gate.

Steps:

1. Gates: the row is still open, `control.emergency_stop` is off (else the job is queued again for
   `vod.stop_retry_s` later, which is not a failure), the broadcaster's tier still allows clipping.
2. Download the clip's MP4 with yt-dlp through the VOD lane's wrapper (`vod.ytdlp.run`: timeout,
   retries, failure kinds). A Twitch clip is a short public file: no proxy and no cookies are used.
3. Render with `factory.pipeline.render_clip`. The window is the whole clip, or its last
   `gate.hourly.max_len` seconds when it is longer; the clip writer's own in and out points are not
   applied, only the usual snap to the words and the length limits. The clip row's id is the
   `viewer_clips` row's id, so a retried job finds the same row and a finished clip is never rendered
   twice.
4. The gate judges the clip (`gate.after_render`), exactly as after a `clip.render` job.
5. The downloaded file is deleted, whatever happened.

Where the clip lies in the broadcast decides how it is stored. When Twitch gave the broadcast's video id
and the offset, the clip row is stored under that video (`clips.source_id` = the video id, `start_s` /
`end_s` on the video's clock): then the gate's source-range duplicate check sees this clip and the VOD
lane's own clip of the same moment as clips of one video, whichever comes second is rejected. Without
them the clip row is stored under the Twitch clip id, from second 0.

A clip that Twitch no longer has is closed as `skipped`, at once and for good. Any other failure is
retried by the job queue; after the last attempt the row is closed as `failed`.
"""

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import structlog

from sixstars import jobs, settings
from sixstars.ai.calls import BudgetExceededError
from sixstars.factory import pipeline, tools
from sixstars.factory.pipeline import RenderInput
from sixstars.logging import get_logger
from sixstars.viewer_clips import store
from sixstars.viewer_clips.config import ClipsConfig
from sixstars.viewer_clips.store import ViewerClip
from sixstars.vod import ytdlp
from sixstars.vod.config import VodConfig
from sixstars.vod.ytdlp import Access, Failure, Runner, VideoUnavailableError, YtDlpError

FILE_STEM: Final = "source"
CONTAINER: Final = "mp4"
REQUEUE_SUFFIX: Final = ":again"
RENDER_LANE: Final = "hourly"
RENDER_MODE: Final = "normal"
# yt-dlp's words for a clip Twitch has deleted (seen 2026-11-17: "This clip is no longer available"),
# besides the markers of `vod.ytdlp` for videos.
GONE_MARKERS: Final[tuple[str, ...]] = ("no longer available", "clip does not exist")
# Twitch also offers auto-cropped portrait renditions of a clip (format ids `portrait-360` ...); the
# factory makes its own 9:16 layout from the full picture, so those are never taken.
LANDSCAPE: Final = "[format_id!^=portrait]"


class ClipGoneError(Exception):
    """Twitch no longer has the clip: it is skipped, not retried."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _gate_after_render(clip_id: str, job: jobs.Job) -> None:
    from sixstars.gate import gate

    gate.after_render(clip_id, job)


@dataclass(slots=True)
class Deps:
    """Replaceable collaborators (tests pass fakes)."""

    runner: Runner | None = None
    render: Callable[[RenderInput], str] = pipeline.render_clip
    after_render: Callable[[str, jobs.Job], None] = _gate_after_render
    now: Callable[[], datetime] = utc_now
    ffprobe: str = "ffprobe"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one job run did: `done`, `skipped`, `deferred`, `ignored`."""

    status: str
    reason: str = ""
    clip_status: str | None = None


# ── pure helpers ─────────────────────────────────────────────


def format_selector(max_height: int) -> str:
    """The best landscape rendition up to `max_height`, else the smallest landscape one above it, else
    whatever there is. A Twitch clip is one progressive MP4 per rendition (video and audio together)."""
    return f"b[height<={max_height}]{LANDSCAPE}/b{LANDSCAPE}/b"


def download_arguments(url: str, out_dir: Path, max_height: int) -> list[str]:
    """The yt-dlp arguments (after `vod.ytdlp.base_command`) that fetch the clip to `out_dir/source.mp4`."""
    return [
        "-f",
        format_selector(max_height),
        "--merge-output-format",
        CONTAINER,
        "--no-part",
        "--force-overwrites",
        "--output",
        str(out_dir / f"{FILE_STEM}.%(ext)s"),
        url,
    ]


def is_gone(error: YtDlpError) -> bool:
    """True when the failure says the clip itself cannot be had by anyone."""
    if isinstance(error, VideoUnavailableError):
        return True
    return any(marker in ytdlp.normalise(error.detail) for marker in GONE_MARKERS)


def render_input(
    row: ViewerClip,
    source_path: Path,
    file_duration_s: float,
    max_len_s: float,
    moment_chosen: bool = False,
) -> RenderInput:
    """The factory input of a downloaded clip. Pure.

    The credit (`creator`) is the broadcaster's handle. The window is the whole file and the clip
    writer's own in and out points are not applied (`whole_window`): measured on real clips, the writer
    cut a 60-second clip down to its first 20 seconds, and the moment of a Twitch clip is at its end
    (a viewer presses "clip" just after it happened). For the same reason a file longer than
    `max_len_s` keeps its last `max_len_s` seconds. The whole file is still read by the writer for the
    hook, the title and the caption. No chat and no spike
    verdict: viewers chose the moment. `moment_chosen` (the setting `twitch_clips.render_without_moment`)
    passes that on to the factory: see `RenderInput.moment_chosen`.
    """
    in_broadcast = row.vod_id is not None and row.vod_offset_s is not None
    return RenderInput(
        source_path=source_path,
        window_start_s=max(0.0, file_duration_s - max_len_s),
        window_end_s=file_duration_s,
        creator=row.creator_handle,
        creator_id=row.creator_id,
        platform=store.PLATFORM,
        source_id=str(row.vod_id) if in_broadcast else row.source_id,
        source_url=row.url,
        source_type="vod",
        lane=RENDER_LANE,
        mode=RENDER_MODE,
        stream_title=row.title,
        clip_id=row.id,
        pad_start_s=0.0,
        pad_end_s=file_duration_s,
        source_offset_s=float(row.vod_offset_s or 0.0) if in_broadcast else 0.0,
        moment_chosen=moment_chosen,
        whole_window=True,
    )


def requeue_key(dedupe_key: str | None) -> str | None:
    """The key of the job that replaces a deferred one (the deferred job is still running when its
    successor is queued, so the key flips between `<key>` and `<key>:again`)."""
    if dedupe_key is None:
        return None
    if dedupe_key.endswith(REQUEUE_SUFFIX):
        return dedupe_key[: -len(REQUEUE_SUFFIX)]
    return dedupe_key + REQUEUE_SUFFIX


# ── steps ────────────────────────────────────────────────────


def download(row: ViewerClip, cfg: VodConfig, deps: Deps) -> tuple[Path, tools.MediaInfo]:
    """Fetch the clip and check the file. Raises `ClipGoneError` or `YtDlpError`."""
    out_dir = store.download_dir(row.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{FILE_STEM}.{CONTAINER}"
    try:
        ytdlp.run(
            download_arguments(row.url, out_dir, cfg.max_height),
            access=Access(),
            timeout_s=cfg.download_timeout_s,
            what="viewer_clip",
            runner=deps.runner,
        )
    except YtDlpError as exc:
        if is_gone(exc):
            raise ClipGoneError(exc.detail) from exc
        raise
    try:
        media = tools.probe(path, deps.ffprobe)
    except tools.FfmpegError as exc:
        raise YtDlpError(Failure("error", f"the downloaded clip cannot be read: {exc}"[:300])) from exc
    if not media.has_video or media.duration_s <= 0:
        raise YtDlpError(Failure("error", "the downloaded clip has no video"))
    return path, media


def _defer(job: jobs.Job, run_after: datetime) -> None:
    """Queue the same work again for `run_after`; the current job then ends as done, with no attempt of
    its successor used."""
    jobs.enqueue(
        "normal",
        job.type,
        job.payload,
        priority=job.priority,
        run_after=run_after,
        dedupe_key=requeue_key(job.dedupe_key),
        max_attempts=job.max_attempts,
    )


def _finish(row: ViewerClip, job: jobs.Job, deps: Deps) -> Outcome:
    """The render has ended (now or in an earlier run): let the gate judge a clip that waits for it and
    close the row."""
    status = store.clip_status(row.id)
    if status == "gating":
        deps.after_render(row.id, job)
        status = store.clip_status(row.id)
    store.set_status(row.id, "done", "viewer_clip.done", data={"clip_id": row.id, "clip_status": status})
    return Outcome("done", clip_status=status)


def process(row: ViewerClip, job: jobs.Job, deps: Deps) -> Outcome:
    """Run one open row to its end. Raises for a failure worth a retry."""
    log = get_logger("viewer_clips")
    if settings.get_bool("control.emergency_stop", False):
        vod = VodConfig.load()
        run_after = deps.now() + timedelta(seconds=vod.stop_retry_s)
        _defer(job, run_after)
        log.info("viewer_clip_deferred", reason="control.emergency_stop is set")
        return Outcome("deferred", "control.emergency_stop is set")
    if row.forbidden:
        store.set_status(row.id, "skipped", "viewer_clip.skipped", reason="creator forbids clipping")
        return Outcome("skipped", "creator forbids clipping")
    existing = store.clip_status(row.id)
    if existing is not None and existing != "rendering":
        return _finish(row, job, deps)  # rendered by an earlier run of this job: nothing to do twice

    store.set_status(row.id, "processing", "viewer_clip.processing", count_attempt=True)
    vod = VodConfig.load()
    clips_config = ClipsConfig.load()
    try:
        try:
            path, media = download(row, vod, deps)
        except ClipGoneError as exc:
            reason = f"unavailable: {exc}"[:300]
            store.set_status(row.id, "skipped", "viewer_clip.skipped", reason=reason, level="warn")
            log.info("viewer_clip_gone", detail=str(exc)[:200])
            return Outcome("skipped", reason)
        item = render_input(
            row,
            path,
            media.duration_s,
            clips_config.max_len_s,
            moment_chosen=clips_config.render_without_moment,
        )
        try:
            deps.render(item)
        except BudgetExceededError:
            run_after = pipeline.next_budget_reset(deps.now())
            _defer(job, run_after)
            store.set_status(
                row.id,
                "new",
                "viewer_clip.deferred",
                reason="ai.daily_budget_usd is used up",
                level="warn",
                data={"run_after": run_after.isoformat()},
            )
            return Outcome("deferred", "ai.daily_budget_usd is used up")
        except Exception as exc:
            if job.attempts >= job.max_attempts:
                pipeline.mark_failed(item, f"{type(exc).__name__}: {exc}")
            raise
        return _finish(row, job, deps)
    finally:
        shutil.rmtree(store.download_dir(row.id), ignore_errors=True)


# ── job handler ──────────────────────────────────────────────

_deps_factory: Callable[[], Deps] = Deps


def configure(factory: Callable[[], Deps] | None) -> None:
    """Set (or reset, with None) how the job handler builds its collaborators."""
    global _deps_factory  # noqa: PLW0603
    _deps_factory = factory or Deps


def handle(job: jobs.Job, deps: Deps | None = None) -> Outcome:
    """Run one `viewer_clip.process` job and say what happened."""
    log = get_logger("viewer_clips")
    row_id = str(job.payload.get("viewer_clip_id") or "")
    row = store.load(row_id) if row_id else None
    if row is None:
        log.error("viewer_clip_job_without_row", viewer_clip_id=row_id)
        return Outcome("ignored", "the job names no stored clip")
    with structlog.contextvars.bound_contextvars(viewer_clip_id=row.id, clip_id=row.id, job_id=job.id):
        if row.closed:
            log.info("viewer_clip_job_ignored", status=row.status)
            return Outcome("ignored", f"status is {row.status}")
        try:
            outcome = process(row, job, deps or _deps_factory())
        except Exception as exc:
            if job.attempts >= job.max_attempts:
                reason = f"{type(exc).__name__}: {exc}"[:300]
                store.set_status(row.id, "failed", "viewer_clip.failed", reason=reason, level="error")
            raise
        log.info(
            "viewer_clip_job_done", status=outcome.status, reason=outcome.reason, clip=outcome.clip_status
        )
        return outcome


def handle_job(job: jobs.Job) -> None:
    """Worker handler of `viewer_clip.process`."""
    handle(job)

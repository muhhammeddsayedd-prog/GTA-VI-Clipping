"""Implementation of the `sixstars vod` commands (M08): process one video from its URL, or only scan its
chat for spikes.
"""

import json
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import typer

from sixstars import db, jobs
from sixstars.chat import twitch_replay
from sixstars.chat import youtube_innertube as innertube
from sixstars.chat.models import ChatSourceError
from sixstars.factory import pipeline
from sixstars.factory import transcript as factory_transcript
from sixstars.spikes import SpikeConfig, analyse_replay
from sixstars.spikes import commands as spike_commands
from sixstars.vod import process, store, ytdlp
from sixstars.vod.common import Deps
from sixstars.vod.config import VodConfig
from sixstars.vod.ytdlp import Runner, YtDlpError

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2
EXIT_NO_CHAT: Final = 3
EXIT_DEFERRED: Final = 4
METADATA_TIMEOUT_S: Final = 120


@dataclass(frozen=True, slots=True)
class Source:
    """A video URL taken apart."""

    platform: str
    source_id: str
    url: str


@dataclass(frozen=True, slots=True)
class Metadata:
    """What the `creators` and `videos` rows need to know about a video."""

    channel_id: str
    handle: str
    display_name: str | None
    title: str | None
    duration_s: float | None
    was_live: bool | None


def parse_source(url: str) -> Source:
    """Platform, id and canonical URL of a Twitch VOD or YouTube video URL (or bare id). Pure.
    Raises `ValueError` when it is neither."""
    text = url.strip()
    if "twitch.tv" in text or text.isdigit():
        vod_id = twitch_replay.normalise_vod_id(text)
        return Source("twitch", vod_id, f"https://www.twitch.tv/videos/{vod_id}")
    try:
        video_id = innertube.extract_video_id(text)
    except ChatSourceError as exc:
        raise ValueError(str(exc)) from exc
    return Source("youtube", video_id, f"https://www.youtube.com/watch?v={video_id}")


def parse_metadata(source: Source, data: Mapping[str, Any]) -> Metadata:
    """The fields of yt-dlp's video JSON that the rows need. Pure."""
    uploader_id = str(data.get("uploader_id") or "").strip()
    channel_id = str(data.get("channel_id") or uploader_id or data.get("uploader") or "").strip()
    handle = uploader_id.lstrip("@") or str(data.get("channel") or data.get("uploader") or channel_id)
    if not channel_id:
        raise ValueError(f"yt-dlp reported no channel for {source.url}")
    duration = data.get("duration")
    live_status = data.get("live_status")
    was_live = (
        True
        if source.platform == "twitch" or live_status in ("was_live", "post_live")
        else False
        if live_status == "not_live"
        else None
    )
    return Metadata(
        channel_id=channel_id,
        handle=handle,
        display_name=str(data.get("channel") or data.get("uploader") or "") or None,
        title=str(data.get("title") or "") or None,
        duration_s=float(duration) if isinstance(duration, int | float) else None,
        was_live=was_live,
    )


def fetch_metadata(source: Source, runner: Runner | None = None) -> Metadata:
    """Ask yt-dlp for the video's JSON (no media)."""
    stdout = ytdlp.run(
        ["--dump-single-json", "--skip-download", source.url],
        access=ytdlp.access_for(source.platform),
        timeout_s=METADATA_TIMEOUT_S,
        what="metadata",
        runner=runner,
    )
    data = json.loads(stdout)
    if not isinstance(data, Mapping):
        raise ValueError("yt-dlp printed no video object")
    return parse_metadata(source, data)


def ensure_rows(source: Source, runner: Runner | None = None) -> str:
    """The `videos.id` for a URL, creating the creator and video rows when they are missing."""
    row = db.fetch_one(
        "select id::text as id from videos where platform = %s and source_id = %s",
        (source.platform, source.source_id),
    )
    if row is not None:
        return str(row["id"])
    meta = fetch_metadata(source, runner)
    creator_id = store.ensure_creator(source.platform, meta.channel_id, meta.handle, meta.display_name)
    return store.ensure_video(
        creator_id=creator_id,
        platform=source.platform,
        source_id=source.source_id,
        url=source.url,
        title=meta.title,
        duration_s=meta.duration_s,
        has_chat=meta.was_live,
    )


def run_renders(video_id: str, whisper_model: str | None) -> list[tuple[str, str]]:
    """Run the queued `clip.render` jobs of one video in this process. Returns `(clip id, status)`."""
    deps = pipeline.Deps(
        transcriber=factory_transcript.default_transcriber(whisper_model) if whisper_model else None
    )
    results: list[tuple[str, str]] = []
    spike_ids = [row.id for row in store.video_spikes(video_id)]
    rows = db.fetch_all(
        """
        update jobs set status = 'running', attempts = attempts + 1, locked_by = 'cli', locked_at = now()
        where type = 'clip.render' and status = 'queued' and payload->>'spike_id' = any(%s)
        returning *
        """,
        (spike_ids,),
    )
    for row in sorted(rows, key=lambda item: -int(item["priority"])):
        job = jobs.Job.from_row(row)
        item = pipeline.RenderInput.from_payload(job.payload)
        clip_id, error = "", ""
        for _attempt in range(max(1, job.max_attempts)):  # as many tries as the worker would give it
            try:
                clip_id, error = pipeline.render_clip(item, deps=deps), ""
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        if error:
            jobs.fail(job.id, error, retry=False)
            pipeline.mark_failed(item, error)
            results.append((str(item.spike_id), f"failed: {error}"))
            continue
        jobs.complete(job.id)
        clip = db.fetch_one("select status, reject_reason from clips where id = %s", (clip_id,))
        status = str(clip["status"]) if clip is not None else "?"
        if clip is not None and clip["reject_reason"]:
            status += f" ({clip['reject_reason']})"
        results.append((clip_id, status))
    return results


def _stdout_utf8() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")


def process_url(url: str, *, render: bool, whisper_model: str, force: bool, as_json: bool) -> int:
    """Run `sixstars vod process`. Returns the process exit code."""
    _stdout_utf8()
    try:
        source = parse_source(url)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    try:
        video_id = ensure_rows(source)
    except (YtDlpError, ValueError) as exc:
        typer.echo(f"cannot read the video's details: {exc}", err=True)
        return EXIT_ERROR
    outcome = process.process_video(video_id, deps=Deps(), force=force)
    wanted = render and outcome.status in ("done", "ignored")
    renders = run_renders(video_id, whisper_model or None) if wanted else []
    if as_json:
        typer.echo(json.dumps({"video_id": video_id, **outcome.to_dict(), "renders": renders}))
    else:
        typer.echo(f"video {video_id} ({source.platform} {source.source_id}): {outcome.status}")
        if outcome.reason:
            typer.echo(f"  reason: {outcome.reason}")
        if outcome.run_after is not None:
            typer.echo(f"  try again after: {outcome.run_after.isoformat()}")
        typer.echo(
            f"  source {outcome.source or '-'}, {outcome.messages} chat messages, {outcome.spikes} spikes, "
            f"{outcome.confirmed} confirmed, {outcome.downloaded} windows downloaded, "
            f"{outcome.queued} renders queued"
        )
        typer.echo(f"  seconds per step: {outcome.timings_s}")
        for clip_id, status in renders:
            typer.echo(f"  clip {clip_id}: {status}")
    if outcome.status == "deferred":
        return EXIT_DEFERRED
    return EXIT_OK if outcome.status in ("done", "skipped", "ignored") else EXIT_ERROR


def scan_only(url: str, *, top: int, details: bool) -> int:
    """Run `sixstars vod scan-only`: download the chat replay and print its spikes. Nothing is stored
    and nothing is downloaded but the chat."""
    _stdout_utf8()
    try:
        source = parse_source(url)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    cfg = VodConfig.load()
    try:
        with tempfile.TemporaryDirectory(prefix="sixstars-vod-scan-") as temp_name:
            raw = Path(temp_name) / "chat.json" if source.platform == "twitch" else None
            replay = process.download_chat(source.platform, source.source_id, raw)
    except ChatSourceError as exc:
        typer.echo(f"chat replay failed: {exc}", err=True)
        return EXIT_ERROR
    if replay is None:
        typer.echo("this video has no chat replay (the VOD lane would use the transcript fallback)")
        return EXIT_NO_CHAT
    scan = analyse_replay(replay.messages, config=SpikeConfig.from_settings(), end_guard_s=cfg.end_guard_s)
    best = process.best_first(scan.spikes, top if top > 0 else len(scan.spikes))
    typer.echo(spike_commands.format_summary(Path(f"{source.platform}:{source.source_id}"), scan, len(best)))
    for line in spike_commands.format_table(best):
        typer.echo(line)
    if details:
        for rank, spike in enumerate(best, start=1):
            typer.echo("")
            for line in spike_commands.format_details(rank, spike, 12):
                typer.echo(line)
    return EXIT_OK

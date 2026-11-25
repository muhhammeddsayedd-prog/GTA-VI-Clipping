"""Command line entry point (`sixstars <command>`). Declares every process entry point and testing
command from `docs/03-repo-and-stack.md` section 3 as a typer command. Real so far: `version` (M00),
`worker`, `settings get/set` and `db ping` (M01), `youtube auth` and `youtube whoami` (M02). Every other
command is a placeholder that prints
"not implemented yet" and exits with code 1, and is filled in by the milestone that owns it.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager

import typer

from sixstars import __version__, db, settings
from sixstars.config import MissingConfigError, env

NOT_IMPLEMENTED = "not implemented yet"

app = typer.Typer(name="sixstars", help="6 Stars Clips command line.", no_args_is_help=True)
discovery_app = typer.Typer(help="Creator discovery.", no_args_is_help=True)
vod_app = typer.Typer(help="VOD lane.", no_args_is_help=True)
spikes_app = typer.Typer(help="Chat spike engine.", no_args_is_help=True)
clip_app = typer.Typer(help="Clip factory.", no_args_is_help=True)
post_app = typer.Typer(help="Posting engine.", no_args_is_help=True)
settings_app = typer.Typer(help="Runtime settings.", no_args_is_help=True)
db_app = typer.Typer(help="Database.", no_args_is_help=True)
youtube_app = typer.Typer(help="YouTube owner channel (OAuth).", no_args_is_help=True)

app.add_typer(discovery_app, name="discovery")
app.add_typer(vod_app, name="vod")
app.add_typer(spikes_app, name="spikes")
app.add_typer(clip_app, name="clip")
app.add_typer(post_app, name="post")
app.add_typer(settings_app, name="settings")
app.add_typer(db_app, name="db")
app.add_typer(youtube_app, name="youtube")


def _not_implemented() -> None:
    """Print the placeholder message and exit non-zero so a stub is never mistaken for success."""
    typer.echo(NOT_IMPLEMENTED)
    raise typer.Exit(code=1)


@contextmanager
def _database() -> Iterator[None]:
    """Run a command that uses the database: report missing configuration cleanly and close the pool."""
    try:
        yield
    except MissingConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    finally:
        db.close_pool()


@app.command()
def version() -> None:
    """Print the package version and the current environment."""
    typer.echo(f"sixstars {__version__} (env={env().APP_ENV})")


@app.command()
def api(
    host: str = typer.Option("0.0.0.0", "--host", help="Address to listen on."),
    port: int = typer.Option(8080, "--port", help="Port to listen on."),
) -> None:
    """Run the FastAPI control API."""
    from sixstars.api import main as api_main

    with _database():
        api_main.run(host=host, port=port)


@app.command("live-manager")
def live_manager() -> None:
    """Run live recorders, chat readers and live spike detection."""
    from sixstars.services import live_manager as live_manager_service

    with _database():
        live_manager_service.run()


@app.command()
def worker(queue: str = typer.Option(..., "--queue", help="Queue to work: express or normal.")) -> None:
    """Run a job queue worker."""
    from sixstars.services import worker as worker_service

    if queue not in ("express", "normal"):
        typer.echo("--queue must be express or normal", err=True)
        raise typer.Exit(code=2)
    with _database():
        worker_service.run(queue)


@app.command()
def scheduler() -> None:
    """Run heartbeats, the ranking refresh, notification retries and stale-job recovery."""
    from sixstars.services import scheduler as scheduler_service

    with _database():
        scheduler_service.run()


@app.command("console-media-backfill")
def console_media_backfill(
    limit: int = typer.Option(200, "--limit", min=1, help="Rows per step (creators: per round)."),
) -> None:
    """Fill clip thumbnails, source thumbnails and creator pictures for rows made before them (docs/26)."""
    from sixstars import console_media

    with _database():
        result = console_media.backfill(limit)
    typer.echo(json.dumps(result, indent=2, default=str))


@discovery_app.command("run-once")
def discovery_run_once(
    as_json: bool = typer.Option(False, "--json", help="Print the full summary as JSON."),
) -> None:
    """Run one discovery pass and print the summary with the top live streams and videos."""
    from sixstars.discovery import commands

    with _database():
        code = commands.run_once(as_json=as_json)
    raise typer.Exit(code=code)


@discovery_app.command("permissions")
def discovery_permissions(
    limit: int = typer.Option(5, "--limit", help="How many creators to check."),
) -> None:
    """Run the permission check (prompt P6) now for creators that are due."""
    from sixstars.discovery import commands

    with _database():
        code = commands.permissions(limit)
    raise typer.Exit(code=code)


@discovery_app.command("set-tier")
def discovery_set_tier(
    platform: str = typer.Argument(..., help="youtube or twitch."),
    channel: str = typer.Argument(..., help="Channel id or handle of a known creator."),
    tier: str = typer.Argument(..., help="preferred, auto, restricted or blocked."),
    reason: str = typer.Option("set by the owner", "--reason", help="Why (stored on the creator)."),
) -> None:
    """Set a creator's tier by hand, for example `blocked` so they are never clipped."""
    from sixstars.discovery import commands

    with _database():
        code = commands.set_tier(platform, channel, tier, reason)
    raise typer.Exit(code=code)


@discovery_app.command("doctor")
def discovery_doctor(
    video: str = typer.Option("", "--video", help="YouTube id or URL of a past stream to test chat on."),
    search_api: bool = typer.Option(False, "--search-api", help="Also make one Data API search call."),
    as_json: bool = typer.Option(False, "--json", help="Print the results as JSON."),
) -> None:
    """Check each source path once (yt-dlp search, Data API, RSS, chat, a 5 s download, Twitch)."""
    from sixstars.discovery import commands

    with _database():
        code = commands.doctor(video=video, search_api=search_api, as_json=as_json)
    raise typer.Exit(code=code)


@vod_app.command("process")
def vod_process(
    url: str = typer.Argument(..., help="Twitch VOD or YouTube video URL to process end to end."),
    render: bool = typer.Option(False, "--render", help="Also run the queued clip.render jobs here."),
    whisper_model: str = typer.Option("", "--whisper-model", help="Local model for --render (testing)."),
    force: bool = typer.Option(False, "--force", help="Ignore the blocked-downloads and concurrency gates."),
    as_json: bool = typer.Option(False, "--json", help="Print the outcome as JSON."),
) -> None:
    """Process one video end to end: chat, spikes, confirmation, window downloads, render jobs."""
    from sixstars.vod import commands

    with _database():
        code = commands.process_url(
            url, render=render, whisper_model=whisper_model, force=force, as_json=as_json
        )
    raise typer.Exit(code=code)


@vod_app.command("retry-waiting")
def vod_retry_waiting(
    platform: str = typer.Option("youtube", "--platform", help="youtube or twitch."),
) -> None:
    """Run the jobs of videos waiting after a download refusal now, not at the end of their back-off."""
    from sixstars.vod import store

    with _database():
        moved = store.retry_waiting_now(platform)
    typer.echo(f"{moved} waiting {platform} job(s) will run now")


@vod_app.command("scan-only")
def vod_scan_only(
    url: str = typer.Argument(..., help="Twitch VOD or YouTube video URL."),
    top: int = typer.Option(25, "--top", help="How many spikes to print, best first (0 = all)."),
    details: bool = typer.Option(False, "--details", help="Also print the z-scores and a chat sample."),
) -> None:
    """Download the chat replay and print its spikes. Stores nothing and downloads no video."""
    from sixstars.vod import commands

    with _database():
        code = commands.scan_only(url, top=top, details=details)
    raise typer.Exit(code=code)


@spikes_app.command("scan")
def spikes_scan(
    chat_file: str = typer.Argument(
        ..., help="Chat file: .jsonl from `chat replay`, or TwitchDownloader JSON."
    ),
    top: int = typer.Option(25, "--top", help="How many spikes to print, best first (0 = all)."),
    details: bool = typer.Option(False, "--details", help="Also print the z-scores and a chat sample."),
    sample: int = typer.Option(12, "--sample", help="Chat messages per spike with --details."),
    settings_file: str = typer.Option(
        "", "--settings-file", help="JSON object of settings to use instead of the database."
    ),
    set_pairs: list[str] = typer.Option(
        [], "--set", help="Override one setting for this run, as key=json (repeatable)."
    ),
    end_guard: float = typer.Option(
        0.0, "--end-guard", help="Drop spikes in the last N seconds of the chat."
    ),
) -> None:
    """Print the spikes found in a chat file."""
    from pathlib import Path

    from sixstars.spikes import commands

    def run() -> int:
        return commands.scan(
            Path(chat_file),
            top=top,
            details=details,
            sample=sample,
            settings_file=Path(settings_file) if settings_file else None,
            set_pairs=set_pairs,
            end_guard_s=end_guard,
        )

    if settings_file:
        raise typer.Exit(code=run())
    with _database():
        code = run()
    raise typer.Exit(code=code)


@clip_app.command("render")
def clip_render(
    spike_id: str = typer.Argument(..., help="Spike to render a clip for."),
    source: str = typer.Option("", "--source", help="Local video file that holds the spike window."),
    source_offset: float = typer.Option(
        0.0, "--source-offset", help="Where the file starts in the original video, in seconds."
    ),
    lane: str = typer.Option("", "--lane", help="spike or hourly (default: from the spike)."),
    mode: str = typer.Option("", "--mode", help="normal or express (default: from the lane)."),
    upload: bool = typer.Option(False, "--upload", help="Also upload the clip to Storage."),
    force: bool = typer.Option(False, "--force", help="Render again even if the clip is finished."),
) -> None:
    """Render one clip from a stored spike (transcribe, write, render; the clip row ends in `gating`)."""
    from pathlib import Path

    from sixstars.factory import commands

    with _database():
        code = commands.render_spike(
            spike_id,
            source=Path(source) if source else None,
            source_offset_s=source_offset,
            lane=lane,
            mode=mode,
            upload=upload,
            force=force,
        )
    raise typer.Exit(code=code)


@clip_app.command("render-file")
def clip_render_file(
    video: str = typer.Argument(..., help="Local video file."),
    start: float = typer.Option(..., "--start", help="Clip start in the file, in seconds."),
    end: float = typer.Option(..., "--end", help="Clip end in the file, in seconds."),
    creator: str = typer.Option(..., "--creator", help="Creator handle for the credit."),
    lane: str = typer.Option("hourly", "--lane", help="hourly or spike (spike adds the LIVE badge)."),
    mode: str = typer.Option("normal", "--mode", help="normal or express."),
    output: str = typer.Option("", "-o", "--output", help="MP4 to write (default: next to the video)."),
    hook: str = typer.Option("", "--hook", help="Hook title (without --ai)."),
    keyword: list[str] = typer.Option([], "--keyword", help="Word to highlight (repeatable, without --ai)."),
    facecam: str = typer.Option("", "--facecam", help="Facecam box x,y,w,h in source pixels (layout A)."),
    layout: str = typer.Option("auto", "--layout", help="auto or b (b forces the no-facecam layout)."),
    series_no: int = typer.Option(1, "--series-no", help="Series number shown on the end card."),
    ai: bool = typer.Option(False, "--ai", help="Use the clip writer and layout detection (needs the DB)."),
    title: str = typer.Option("", "--title", help="Stream title given to the clip writer (with --ai)."),
    whisper_model: str = typer.Option("", "--whisper-model", help="Local Whisper model for this run."),
    set_pairs: list[str] = typer.Option(
        [], "--set", help="Override one setting, as key=json (repeatable, without --ai)."
    ),
    stills: str = typer.Option("", "--stills", help="Directory for stills at 1 s, 5 s, middle, end card."),
    no_music: bool = typer.Option(False, "--no-music", help="Skip music detection."),
    payoff: float = typer.Option(-1.0, "--payoff", help="Payoff time in the file, seconds (without --ai)."),
    payoff_emoji: str = typer.Option("", "--payoff-emoji", help="Payoff emoji (without --ai)."),
    style: str = typer.Option(
        "", "--style", help="Design style id from factory.styles (default: the single factory.style)."
    ),
    title_line: str = typer.Option("", "--title-line", help="Writer's title line (without --ai)."),
    headline: str = typer.Option("", "--headline", help="Writer's news headline (without --ai)."),
    question: str = typer.Option("", "--question", help="Writer's question title (without --ai)."),
    pov: str = typer.Option("", "--pov", help="Writer's POV line (without --ai)."),
) -> None:
    """Render a clip from a local video without writing a clip row (for design iteration)."""
    from pathlib import Path

    from sixstars.factory import commands

    def run() -> int:
        return commands.render_file(
            Path(video),
            start=start,
            end=end,
            creator=creator,
            lane=lane,
            mode=mode,
            output=Path(output) if output else None,
            hook=hook,
            keywords=keyword,
            facecam_box=facecam,
            layout=layout,
            series_no=series_no,
            use_ai=ai,
            title=title,
            whisper_model=whisper_model,
            set_pairs=set_pairs,
            stills_dir=Path(stills) if stills else None,
            no_music=no_music,
            payoff_s=payoff if payoff >= 0 else None,
            payoff_emoji=payoff_emoji,
            style_id=style,
            texts={"title_line": title_line, "headline": headline, "question": question, "pov_line": pov},
        )

    if not ai:
        raise typer.Exit(code=run())
    with _database():
        code = run()
    raise typer.Exit(code=code)


@post_app.command("hourly")
def post_hourly(
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate the slot without posting."),
) -> None:
    """Run one hourly posting slot."""
    _not_implemented()


@settings_app.command("get")
def settings_get(key: str = typer.Argument(..., help="Setting key.")) -> None:
    """Print a runtime setting as JSON."""
    with _database():
        try:
            value = settings.get(key)
        except KeyError as exc:
            typer.echo(f"unknown setting: {key}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(json.dumps(value, ensure_ascii=False))


@settings_app.command("set")
def settings_set(
    key: str = typer.Argument(..., help="Setting key."),
    value: str = typer.Argument(..., help="New value as JSON, for example 20, true or '\"text\"'."),
) -> None:
    """Change a runtime setting. The value must be valid JSON."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        typer.echo(f"value is not valid JSON: {exc.msg}", err=True)
        raise typer.Exit(code=2) from exc
    with _database():
        settings.set(key, parsed)
        typer.echo(f"{key} = {json.dumps(parsed, ensure_ascii=False)}")


@db_app.command("ping")
def db_ping() -> None:
    """Check that the database answers."""
    with _database():
        try:
            ok = db.ping()
        except MissingConfigError:
            raise
        except Exception as exc:
            typer.echo(f"database ping failed: {type(exc).__name__}", err=True)
            raise typer.Exit(code=1) from exc
        if not ok:
            typer.echo("database ping failed", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"ok (env={env().APP_ENV})")


@youtube_app.command("auth")
def youtube_auth(
    port: int = typer.Option(0, "--port", help="Local port for the sign-in redirect (0 picks a free one)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the sign-in URL without opening it."),
    timeout_s: int = typer.Option(300, "--timeout", help="Seconds to wait for the sign-in to finish."),
) -> None:
    """Sign in to the owner's YouTube channel once and print the refresh token for `.env`.

    Run it on a machine with a browser. It needs YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET
    of a "Desktop app" OAuth client.
    """
    from sixstars.clients import youtube_oauth
    from sixstars.clients._http import ClientError

    settings_env = env()
    try:
        client_id = settings_env.require("YOUTUBE_OAUTH_CLIENT_ID")
        client_secret = settings_env.require("YOUTUBE_OAUTH_CLIENT_SECRET")
    except MissingConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    try:
        tokens = youtube_oauth.run_local_flow(
            client_id,
            client_secret,
            port=port,
            open_browser=not no_browser,
            timeout_s=timeout_s,
            echo=typer.echo,
        )
    except ClientError as exc:
        typer.echo(f"sign-in failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        typer.echo(
            "Google returned no refresh token: remove this app from the Google account and retry", err=True
        )
        raise typer.Exit(code=1)
    typer.echo("Add this line to .env (keep it secret):")
    typer.echo(f"YOUTUBE_OAUTH_REFRESH_TOKEN={refresh_token}")


@youtube_app.command("whoami")
def youtube_whoami() -> None:
    """Print the channel that YOUTUBE_OAUTH_REFRESH_TOKEN belongs to."""
    from sixstars.clients._http import ClientError
    from sixstars.clients.youtube import YouTubeClient

    client = YouTubeClient()
    if not client.has_oauth():
        typer.echo(
            "YouTube OAuth is not configured: set YOUTUBE_OAUTH_CLIENT_ID, YOUTUBE_OAUTH_CLIENT_SECRET and "
            "YOUTUBE_OAUTH_REFRESH_TOKEN (run `sixstars youtube auth`)",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        channel = client.whoami()
    except ClientError as exc:
        typer.echo(f"whoami failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        db.close_pool()
    typer.echo(f"{channel.title} ({channel.id})")


# Chat ingestion (M03). Kept as one block so other milestones' edits to this file do not collide with it.
chat_app = typer.Typer(help="Chat ingestion.", no_args_is_help=True)
app.add_typer(chat_app, name="chat")


@chat_app.command("dump")
def chat_dump(
    source: str = typer.Argument(..., help="Twitch login, or the URL of a live YouTube stream."),
    seconds: float = typer.Option(60.0, "--seconds", help="How long to read the chat."),
) -> None:
    """Print a live chat for a number of seconds."""
    from sixstars.chat import commands

    raise typer.Exit(code=commands.dump(source, seconds))


@chat_app.command("replay")
def chat_replay(
    source: str = typer.Argument(..., help="Twitch VOD id or URL, or YouTube video URL."),
    output: str = typer.Option(..., "-o", "--output", help="JSON Lines file to write."),
) -> None:
    """Download a whole chat replay to a JSON Lines file."""
    from pathlib import Path

    from sixstars.chat import commands

    raise typer.Exit(code=commands.replay(source, Path(output)))


# Posting engine (M10). Kept as one block so other milestones' edits to this file do not collide with it.
posting_app = typer.Typer(help="Posting engine (M10): plan, run and test posts.", no_args_is_help=True)
app.add_typer(posting_app, name="posting")


@posting_app.command("plan")
def posting_plan(
    slot: str = typer.Option("", "--slot", help="Slot to plan as an ISO hour (default: the current hour)."),
) -> None:
    """Print what the hourly slot would do. Writes and sends nothing."""
    from sixstars.posting import commands

    with _database():
        code = commands.plan(slot)
    raise typer.Exit(code=code)


@posting_app.command("hourly")
def posting_hourly(
    dry_run: bool = typer.Option(False, "--dry-run", help="Only print the plan: write and send nothing."),
) -> None:
    """Run the current hourly slot now, without jitter. Respects dry run and the emergency stop."""
    from sixstars.posting import commands

    with _database():
        code = commands.plan() if dry_run else commands.hourly()
    raise typer.Exit(code=code)


@posting_app.command("trigger")
def posting_trigger() -> None:
    """Do what the hourly trigger does: heartbeat and one post.hourly job at the jittered time."""
    from sixstars.posting import commands

    with _database():
        code = commands.trigger()
    raise typer.Exit(code=code)


@posting_app.command("clip")
def posting_clip(
    clip_id: str = typer.Argument(..., help="Approved clip to post."),
    account: str = typer.Option(..., "--account", help="Account id, for example youtube_main."),
) -> None:
    """Staging only: post one approved clip to one account (privately, through every posting rule)."""
    from sixstars.posting import commands

    with _database():
        code = commands.clip(clip_id, account)
    raise typer.Exit(code=code)


# Notifications (M12). Kept as one block so other milestones' edits to this file do not collide with it.
notify_app = typer.Typer(help="Telegram notifications through the n8n notify webhook.", no_args_is_help=True)
app.add_typer(notify_app, name="notify")


@notify_app.command("test")
def notify_test() -> None:
    """Send the info notification "Test" through the notify webhook, to check the path to Telegram."""
    from sixstars import notify

    with _database():
        delivered = notify.send("info", "Test")
    typer.echo("Test notification delivered." if delivered else "Test notification was NOT delivered.")
    raise typer.Exit(code=0 if delivered else 1)


# Quality gate (M07). Kept as one block so other milestones' edits to this file do not collide with it.
gate_app = typer.Typer(help="Quality gate (M07): judge clips and explain verdicts.", no_args_is_help=True)
app.add_typer(gate_app, name="gate")


@gate_app.command("run")
def gate_run(
    clip_id: str = typer.Argument(..., help="Clip in status gating."),
    no_post: bool = typer.Option(False, "--no-post", help="Do not hand an approved spike clip to posting."),
) -> None:
    """Judge one clip: approve, reject or hold it, and store the verdict."""
    from sixstars.gate import commands

    with _database():
        code = commands.run(clip_id, post=not no_post)
    raise typer.Exit(code=code)


@gate_app.command("pending")
def gate_pending(
    older_than: float = typer.Option(
        0.0, "--older-than", help="Only clips waiting at least this many seconds."
    ),
    no_post: bool = typer.Option(False, "--no-post", help="Do not hand approved spike clips to posting."),
) -> None:
    """Judge every clip still in status gating (recovery)."""
    from sixstars.gate import commands

    with _database():
        code = commands.pending(older_than, post=not no_post)
    raise typer.Exit(code=code)


@gate_app.command("explain")
def gate_explain(clip_id: str = typer.Argument(..., help="Any clip.")) -> None:
    """Print the stored verdict of a clip with every check. Writes nothing."""
    from sixstars.gate import commands

    with _database():
        code = commands.explain(clip_id)
    raise typer.Exit(code=code)


@gate_app.command("check-file")
def gate_check_file(
    video: str = typer.Argument(..., help="Rendered MP4 file."),
    duration: float = typer.Option(
        0.0, "--duration", help="Clip length without the end card (default: file)."
    ),
    p5: bool = typer.Option(False, "--p5", help="Also ask the vision model about four frames (costs money)."),
) -> None:
    """Measure a rendered file as the gate does; with --p5 also run the visual check. Writes no clip."""
    from sixstars.gate import commands

    with _database():
        code = commands.check_file(video, duration or None, p5)
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()


# Guardian and report (M13). Kept as one block so other milestones' edits to this file do not collide.
guardian_app = typer.Typer(help="Guardian (M13): claims check, health, strikes.", no_args_is_help=True)
report_app = typer.Typer(help="Stats and the daily report (M13).", no_args_is_help=True)
app.add_typer(guardian_app, name="guardian")
app.add_typer(report_app, name="report")


@guardian_app.command("check")
def guardian_check() -> None:
    """Run one claims and removals check now (what POST /v1/guardian/check does)."""
    from sixstars.guardian import commands

    with _database():
        code = commands.check()
    raise typer.Exit(code=code)


@guardian_app.command("health")
def guardian_health() -> None:
    """Run the health checks once and print what they found."""
    from sixstars.guardian import commands

    with _database():
        code = commands.health_once()
    raise typer.Exit(code=code)


@guardian_app.command("strikes")
def guardian_strikes() -> None:
    """Show the strikes that count now, the limit and the emergency stop."""
    from sixstars.guardian import commands

    with _database():
        code = commands.strikes()
    raise typer.Exit(code=code)


@guardian_app.command("pending")
def guardian_pending() -> None:
    """List the posts that wait for a manual removal."""
    from sixstars.guardian import commands

    with _database():
        code = commands.pending()
    raise typer.Exit(code=code)


@report_app.command("daily")
def report_daily(
    format_: str = typer.Option("telegram", "--format", help="telegram or json."),
    day: str = typer.Option("", "--day", help="Day as YYYY-MM-DD (default: the day that just ended)."),
) -> None:
    """Print the daily report."""
    from sixstars.guardian import commands

    with _database():
        code = commands.daily(format_, day)
    raise typer.Exit(code=code)


@report_app.command("collect")
def report_collect() -> None:
    """Collect account and post numbers now (what POST /v1/stats/collect does)."""
    from sixstars.guardian import commands

    with _database():
        code = commands.collect()
    raise typer.Exit(code=code)


# Live lane (M14). Kept as one block so other milestones' edits to this file do not collide with it.
live_app = typer.Typer(help="Live lane (M14): sessions, buffers and source checks.", no_args_is_help=True)
app.add_typer(live_app, name="live")


@live_app.command("status")
def live_status(as_json: bool = typer.Option(False, "--json", help="Print everything as JSON.")) -> None:
    """Show the live sessions, the buffers on disk and the health of the live sources."""
    from sixstars.live import commands

    with _database():
        code = commands.status(as_json)
    raise typer.Exit(code=code)


@live_app.command("probe")
def live_probe(
    source: str = typer.Argument(..., help="Twitch login or URL, or the URL of a live YouTube stream."),
    seconds: float = typer.Option(40.0, "--seconds", help="How long to record."),
    keep: bool = typer.Option(False, "--keep", help="Keep the recorded files."),
    no_chat: bool = typer.Option(False, "--no-chat", help="Do not read the live chat."),
) -> None:
    """Record a stream for a few seconds the way the live lane does and report what happened.

    Writes nothing to the database. Exit code 0: recorded; 3: the platform refused or throttled this
    machine (use --seconds 90 or more to see throttling, and read "lag_s"); 1: other.
    """
    from sixstars.live import commands

    with _database():
        code = commands.probe(source, seconds, keep=keep, with_chat=not no_chat)
    raise typer.Exit(code=code)


@live_app.command("cut")
def live_cut(
    session: str = typer.Argument(..., help="Session id, or the path of a buffer directory."),
    start: str = typer.Option(..., "--start", help="Window start, ISO 8601 (UTC without a zone)."),
    end: str = typer.Option(..., "--end", help="Window end, ISO 8601."),
    output: str = typer.Option("", "-o", "--output", help="File to write (default: the cuts directory)."),
) -> None:
    """Cut a wall-clock window out of a session's rolling buffer, without re-encoding."""
    from sixstars.live import commands

    raise typer.Exit(code=commands.cut(session, start, end, output))


# Title writer v2 (docs/30-titles-v2.md). Kept as one block so other edits to this file do not collide.
titles_app = typer.Typer(help="Title writer v2: facts in, checked titles out.", no_args_is_help=True)
app.add_typer(titles_app, name="titles")


@titles_app.command("retitle")
def titles_retitle(
    clip_id: str = typer.Argument(..., help="Any clip with a transcript."),
    apply: bool = typer.Option(False, "--apply", help="Write the title on the clip (only while not posted)."),
) -> None:
    """Write v2 titles for one clip and show every candidate with the checks it failed (one AI call)."""
    from sixstars.titles import commands

    with _database():
        code = commands.retitle(clip_id, apply)
    raise typer.Exit(code=code)


@titles_app.command("compare")
def titles_compare(
    limit: int = typer.Option(30, "--limit", help="How many of the most recent clips."),
    status: list[str] = typer.Option(["posted"], "--status", help="Clip statuses to take (repeatable)."),
    out: str = typer.Option("", "--out", help="Also write the side-by-side table to this Markdown file."),
) -> None:
    """Dry run: old and new title side by side for recent clips. Changes no clip (one AI call per clip)."""
    from sixstars.titles import commands

    with _database():
        code = commands.compare(limit, status, out or None)
    raise typer.Exit(code=code)


@titles_app.command("queue")
def titles_queue(
    limit: int = typer.Option(200, "--limit", help="At most this many waiting clips, best ranked first."),
    apply: bool = typer.Option(False, "--apply", help="Write the titles (without it: dry run)."),
) -> None:
    """Re-title approved clips waiting to post that have no v2 title yet (one AI call per clip)."""
    from sixstars.titles import commands

    with _database():
        code = commands.queue(limit, apply)
    raise typer.Exit(code=code)


# Clipping campaigns (D118). Kept as one block so other milestones' edits to this file do not collide with it.
campaign_app = typer.Typer(help="Clipping campaigns (D118): registry, quotas, status.", no_args_is_help=True)
app.add_typer(campaign_app, name="campaign")


@campaign_app.callback()
def _campaign_console() -> None:
    from sixstars.campaigns import commands

    commands.safe_stdout()


_STREAMER_HELP = "Streamer: twitch:<login>, youtube:@<handle> or youtube:<channel id> (repeatable)."
_QUOTA_HELP = "Daily quota per platform: youtube=2-3 (repeatable)."


@campaign_app.command("add")
def campaign_add(
    slug: str = typer.Argument(..., help="Short id, e.g. dylanpage-marathon."),
    name: str = typer.Option(..., "--name"),
    rate: float = typer.Option(..., "--rate", help="Payout per 1,000 views."),
    platforms: str = typer.Option(..., "--platforms", help="Comma list: youtube,instagram,tiktok."),
    streamer: list[str] = typer.Option(..., "--streamer", help=_STREAMER_HELP),
    quota: list[str] = typer.Option(..., "--quota", help=_QUOTA_HELP),
    min_payout: float | None = typer.Option(None, "--min-payout"),
    max_payout: float | None = typer.Option(None, "--max-payout"),
    currency: str | None = typer.Option(None, "--currency"),
    budget: float | None = typer.Option(None, "--budget", help="Total budget (budget left starts equal)."),
    budget_left: float | None = typer.Option(None, "--budget-left"),
    profile: str | None = typer.Option(None, "--profile", help="Render profile id (default brand-disclose)."),
    priority: int | None = typer.Option(None, "--priority"),
    starts: str | None = typer.Option(None, "--starts", help="ISO time (UTC)."),
    ends: str | None = typer.Option(None, "--ends", help="ISO time (UTC)."),
    game: str | None = typer.Option(None, "--game", help='Required game, or "any".'),
    tag: list[str] = typer.Option([], "--tag", help="Required hashtag (repeatable)."),
    mention: str | None = typer.Option(None, "--mention"),
    cta: str | None = typer.Option(None, "--cta", help="Required caption line."),
    banned: list[str] = typer.Option([], "--banned", help="Banned caption word (repeatable)."),
    brief: str | None = typer.Option(None, "--brief", help='Brief rules JSON, e.g. {"min_len": 10}.'),
    submit_url: str | None = typer.Option(None, "--submit-url"),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    """Add a campaign as a draft (look at it with `show`, then `activate`)."""
    from sixstars.campaigns import commands

    options = {
        "name": name,
        "rate": rate,
        "platforms": platforms,
        "streamer": streamer,
        "quota": quota,
        "min_payout": min_payout,
        "max_payout": max_payout,
        "currency": currency,
        "budget": budget,
        "budget_left": budget_left,
        "profile": profile,
        "priority": priority,
        "starts": starts,
        "ends": ends,
        "game": game,
        "tag": tag,
        "mention": mention,
        "cta": cta,
        "banned": banned,
        "brief": brief,
        "submit_url": submit_url,
        "notes": notes,
    }
    with _database():
        code = commands.add(slug, options)
    raise typer.Exit(code=code)


@campaign_app.command("edit")
def campaign_edit(
    slug: str = typer.Argument(...),
    name: str | None = typer.Option(None, "--name"),
    rate: float | None = typer.Option(None, "--rate"),
    platforms: str | None = typer.Option(None, "--platforms"),
    streamer: list[str] = typer.Option([], "--streamer", help=_STREAMER_HELP + " Replaces the list."),
    quota: list[str] = typer.Option([], "--quota", help=_QUOTA_HELP + " Replaces the quota."),
    min_payout: float | None = typer.Option(None, "--min-payout"),
    max_payout: float | None = typer.Option(None, "--max-payout"),
    budget: float | None = typer.Option(None, "--budget"),
    budget_left: float | None = typer.Option(None, "--budget-left"),
    profile: str | None = typer.Option(None, "--profile"),
    priority: int | None = typer.Option(None, "--priority"),
    starts: str | None = typer.Option(None, "--starts"),
    ends: str | None = typer.Option(None, "--ends"),
    game: str | None = typer.Option(None, "--game"),
    tag: list[str] = typer.Option([], "--tag"),
    mention: str | None = typer.Option(None, "--mention"),
    cta: str | None = typer.Option(None, "--cta"),
    banned: list[str] = typer.Option([], "--banned"),
    brief: str | None = typer.Option(None, "--brief"),
    submit_url: str | None = typer.Option(None, "--submit-url"),
    notes: str | None = typer.Option(None, "--notes"),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    """Change a campaign (only the options given)."""
    from sixstars.campaigns import commands

    options = {
        "name": name,
        "rate": rate,
        "platforms": platforms,
        "streamer": streamer,
        "quota": quota,
        "min_payout": min_payout,
        "max_payout": max_payout,
        "budget": budget,
        "budget_left": budget_left,
        "profile": profile,
        "priority": priority,
        "starts": starts,
        "ends": ends,
        "game": game,
        "tag": tag,
        "mention": mention,
        "cta": cta,
        "banned": banned,
        "brief": brief,
        "submit_url": submit_url,
        "notes": notes,
        "reason": reason,
    }
    with _database():
        code = commands.edit(slug, options)
    raise typer.Exit(code=code)


def _campaign_status(slug: str, status: str, reason: str | None) -> None:
    from sixstars.campaigns import commands

    with _database():
        code = commands.set_status(slug, status, reason)  # type: ignore[arg-type]
    raise typer.Exit(code=code)


@campaign_app.command("activate")
def campaign_activate(
    slug: str = typer.Argument(...), reason: str | None = typer.Option(None, "--reason")
) -> None:
    """Draft or paused -> active: the campaign gets slots, recording priority and its look."""
    _campaign_status(slug, "active", reason)


@campaign_app.command("resume")
def campaign_resume(
    slug: str = typer.Argument(...), reason: str | None = typer.Option(None, "--reason")
) -> None:
    """Paused -> active."""
    _campaign_status(slug, "active", reason)


@campaign_app.command("pause")
def campaign_pause(
    slug: str = typer.Argument(...), reason: str | None = typer.Option(None, "--reason")
) -> None:
    """Active -> paused: its slots return to the Normal lane."""
    _campaign_status(slug, "paused", reason)


@campaign_app.command("end")
def campaign_end(
    slug: str = typer.Argument(...), reason: str | None = typer.Option(None, "--reason")
) -> None:
    """End a campaign (kept for the earnings history)."""
    _campaign_status(slug, "ended", reason)


@campaign_app.command("budget")
def campaign_budget(slug: str = typer.Argument(...), amount: float = typer.Argument(...)) -> None:
    """Record the budget left (from the Content Rewards page)."""
    from sixstars.campaigns import commands

    with _database():
        code = commands.budget(slug, amount)
    raise typer.Exit(code=code)


@campaign_app.command("list")
def campaign_list(status: str | None = typer.Option(None, "--status")) -> None:
    """List campaigns."""
    from sixstars.campaigns import commands

    with _database():
        code = commands.list_campaigns(status)
    raise typer.Exit(code=code)


@campaign_app.command("show")
def campaign_show(slug: str = typer.Argument(...)) -> None:
    """Show one campaign and its lane quotas."""
    from sixstars.campaigns import commands

    with _database():
        code = commands.show(slug)
    raise typer.Exit(code=code)


@campaign_app.command("lookup")
def campaign_lookup(ref: str = typer.Argument(..., help=_STREAMER_HELP)) -> None:
    """Look a streamer up on their platform (nothing is saved), to confirm the right person."""
    from sixstars.campaigns import commands

    raise typer.Exit(code=commands.find(ref))


@campaign_app.command("quotas")
def campaign_quotas(account: str | None = typer.Option(None, "--account")) -> None:
    """Daily quotas per account and lane (normal, spike, campaign:<slug>)."""
    from sixstars.campaigns import commands

    with _database():
        code = commands.quotas(account)
    raise typer.Exit(code=code)


@campaign_app.command("profile-add")
def campaign_profile_add(
    profile_id: str = typer.Argument(..., help="New profile id, e.g. partner-logo-gtaclipper."),
    base: str = typer.Option("partner-logo", "--from", help="Profile to copy."),
    logo: str | None = typer.Option(None, "--logo", help="Campaign logo PNG file."),
    pos: str | None = typer.Option(None, "--pos", help="bottom_right, bottom_left, top_right or top_left."),
    watermark: bool | None = typer.Option(None, "--watermark/--no-watermark"),
    disclosure: str | None = typer.Option(None, "--disclosure", help='e.g. "Paid partnership".'),
    disclosure_s: float | None = typer.Option(None, "--disclosure-s"),
) -> None:
    """Make a render profile for a campaign (a copy of another, with its own logo)."""
    from sixstars.campaigns import commands

    with _database():
        code = commands.profile_add(profile_id, base, logo, pos, watermark, disclosure, disclosure_s)
    raise typer.Exit(code=code)


@campaign_app.command("profiles")
def campaign_profiles() -> None:
    """List render profiles."""
    from sixstars.campaigns import commands

    with _database():
        code = commands.profile_list()
    raise typer.Exit(code=code)


covers_app = typer.Typer(help="Cover engine (docs/29a): checks and tools.", no_args_is_help=True)
app.add_typer(covers_app, name="covers")


@covers_app.command("eval-lines")
def covers_eval_lines(
    limit: int = typer.Option(50, "--limit", help="How many of the newest clips with a transcript."),
    out: str | None = typer.Option(None, "--out", help="Write every candidate as JSON to this file."),
) -> None:
    """C4 eval: write cover lines (P12) for recent clips and check them. Spends AI budget (fast model)."""
    from pathlib import Path

    from sixstars.covers import hookline

    with _database():
        report = hookline.evaluate(limit)
    if out:
        Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False))


@covers_app.command("report")
def covers_report(
    days: int = typer.Option(7, "--days", help="How many days back."),
    out: str | None = typer.Option(None, "--out", help="Append the section to this markdown file."),
) -> None:
    """The weekly report's covers section (docs/29a C8): QA pass rate, fallbacks, arms, best and worst."""
    from pathlib import Path

    from sixstars.covers import report

    with _database():
        section = report.weekly_section(days)
    if out:
        with Path(out).open("a", encoding="utf-8") as handle:
            handle.write("\n" + section)
    typer.echo(section)


longform_app = typer.Typer(help="Long-form episodes (M20).", no_args_is_help=True)
app.add_typer(longform_app, name="longform")


@longform_app.command("build")
def longform_build(
    date_: str = typer.Option("", "--date", help="Episode date YYYY-MM-DD (UTC); default today."),
) -> None:
    """Plan the episode of a day (idempotent) and print the result."""
    from datetime import UTC, date, datetime

    from sixstars.longform.planner import plan_episode

    with _database():
        day = date.fromisoformat(date_) if date_ else datetime.now(UTC).date()
        result = plan_episode(day)
        typer.echo(
            f"episode {result.episode_id} {result.episode_date} {result.status}: "
            f"{result.n_moments} moments, ~{result.est_s:.0f}s (new={result.created})"
        )

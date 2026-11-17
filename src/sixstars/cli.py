"""Command line entry point (`sixstars <command>`). Declares every process entry point and testing
command from `docs/03-repo-and-stack.md` section 3 as a typer command. Real so far: `version` (M00),
`worker`, `settings get/set` and `db ping` (M01). Every other command is a placeholder that prints
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

app.add_typer(discovery_app, name="discovery")
app.add_typer(vod_app, name="vod")
app.add_typer(spikes_app, name="spikes")
app.add_typer(clip_app, name="clip")
app.add_typer(post_app, name="post")
app.add_typer(settings_app, name="settings")
app.add_typer(db_app, name="db")


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
def api() -> None:
    """Run the FastAPI control API."""
    _not_implemented()


@app.command("live-manager")
def live_manager() -> None:
    """Run live recorders, chat readers and live spike detection."""
    _not_implemented()


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
    """Run heartbeats, failover and cleanup."""
    _not_implemented()


@discovery_app.command("run-once")
def discovery_run_once() -> None:
    """Run one discovery pass."""
    _not_implemented()


@vod_app.command("process")
def vod_process(url: str = typer.Argument(..., help="Video URL to process end to end.")) -> None:
    """Process one video end to end."""
    _not_implemented()


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
        )

    if settings_file:
        raise typer.Exit(code=run())
    with _database():
        code = run()
    raise typer.Exit(code=code)


@clip_app.command("render")
def clip_render(spike_id: str = typer.Argument(..., help="Spike to render a clip for.")) -> None:
    """Render one clip."""
    _not_implemented()


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


if __name__ == "__main__":
    app()

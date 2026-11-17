"""Command line entry point (`sixstars <command>`). Declares every process entry point and testing
command from `docs/03-repo-and-stack.md` section 3 as a typer command. In M00 only `version` is real;
every other command is a placeholder that prints "not implemented yet" and exits with code 1, and is
filled in by the milestone that owns it.
"""

import typer

from sixstars import __version__
from sixstars.config import env

NOT_IMPLEMENTED = "not implemented yet"

app = typer.Typer(name="sixstars", help="6 Stars Clips command line.", no_args_is_help=True)
discovery_app = typer.Typer(help="Creator discovery.", no_args_is_help=True)
vod_app = typer.Typer(help="VOD lane.", no_args_is_help=True)
spikes_app = typer.Typer(help="Chat spike engine.", no_args_is_help=True)
clip_app = typer.Typer(help="Clip factory.", no_args_is_help=True)
post_app = typer.Typer(help="Posting engine.", no_args_is_help=True)
settings_app = typer.Typer(help="Runtime settings.", no_args_is_help=True)

app.add_typer(discovery_app, name="discovery")
app.add_typer(vod_app, name="vod")
app.add_typer(spikes_app, name="spikes")
app.add_typer(clip_app, name="clip")
app.add_typer(post_app, name="post")
app.add_typer(settings_app, name="settings")


def _not_implemented() -> None:
    """Print the placeholder message and exit non-zero so a stub is never mistaken for success."""
    typer.echo(NOT_IMPLEMENTED)
    raise typer.Exit(code=1)


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
    _not_implemented()


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
def spikes_scan(chat_file: str = typer.Argument(..., help="Path to a chat JSON file.")) -> None:
    """Print the spikes found in a chat file."""
    _not_implemented()


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
    """Read a runtime setting."""
    _not_implemented()


@settings_app.command("set")
def settings_set(
    key: str = typer.Argument(..., help="Setting key."),
    value: str = typer.Argument(..., help="New value."),
) -> None:
    """Change a runtime setting."""
    _not_implemented()


if __name__ == "__main__":
    app()

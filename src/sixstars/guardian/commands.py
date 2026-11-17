"""CLI commands of the guardian and of the report: `sixstars guardian ...`, `sixstars report ...`.
Each returns the process exit code."""

import json
from datetime import UTC, date, datetime
from typing import Any

import typer

from sixstars import db, settings
from sixstars.guardian import health, incidents, service, stop
from sixstars.stats import report as report_view


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def check() -> int:
    """One claims and removals pass, exactly as `POST /v1/guardian/check` runs it."""
    result = service.run_check()
    _print(result)
    return 0 if result["status"] == "ok" else 1


def health_once() -> int:
    """Run the health checks once (alerts are sent on a change of state, as in the scheduler)."""
    _print(health.run_checks(datetime.now(UTC)))
    return 0


def strikes() -> int:
    """Show the strikes that count now, the limit and the emergency stop."""
    now = datetime.now(UTC)
    window_d = settings.get_float("guardian.strike_window_d")
    since = stop.window_start(now, window_d, stop.last_stop_at())
    times = stop.strike_times(since)
    _print(
        {
            "strikes": len(times),
            "limit": settings.get_int("guardian.strike_limit"),
            "window_d": window_d,
            "counted_since": since.isoformat(),
            "strike_times": [at.astimezone(UTC).isoformat() for at in times],
            "emergency_stop": stop.is_stopped(),
            "open_incidents": sorted(incidents.open_keys()),
        }
    )
    return 0


def pending() -> int:
    """List the posts that wait for a manual removal."""
    rows = db.fetch_all(
        """
        select p.id::text as id, p.platform, p.account_id, p.post_url, p.remove_reason, p.updated_at
        from posts p where p.status = 'remove_failed' and not p.dry_run order by p.updated_at
        """
    )
    if not rows:
        typer.echo("No post waits for a manual removal.")
        return 0
    for row in rows:
        typer.echo(f"{row['platform']:<10} {row['account_id']:<16} {row['post_url'] or '(no link)'}")
        typer.echo(f"{'':<10} reason: {row['remove_reason'] or '-'}")
    return 0


def daily(format_: str, day: str) -> int:
    """Print the daily report."""
    if format_ not in ("telegram", "json"):
        typer.echo("--format must be telegram or json", err=True)
        return 2
    chosen: date | None = None
    if day:
        try:
            chosen = date.fromisoformat(day)
        except ValueError:
            typer.echo("--day must be a date such as 2026-11-19", err=True)
            return 2
    data = service.daily_report(chosen)
    if format_ == "telegram":
        typer.echo(report_view.render_telegram(data))
    else:
        _print(data)
    return 0


def collect() -> int:
    """One stats pass, exactly as `POST /v1/stats/collect` runs it."""
    result = service.collect_stats()
    _print(result)
    return 0 if result["status"] == "ok" else 1

"""Implementation of the `sixstars posting` commands (M10).

- `plan`: print what the current hourly slot would do. Read-only.
- `hourly`: run the current hourly slot now (no jitter). With `--dry-run` it is the same as `plan`.
- `trigger`: what the hourly trigger does: heartbeat plus one `post.hourly` job at the jittered time.
- `clip`: staging only, post one approved clip to one account through the gate and the caps.

Nothing here bypasses the gate: with `launch.dry_run` true (or before `launch.launch_at`) the commands
record dry-run rows and send nothing, and under `control.emergency_stop` they do nothing.
"""

import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Final

import typer

from sixstars.clients.upload_post import UploadPostClient
from sixstars.config import env
from sixstars.posting.caps import cap_state
from sixstars.posting.gate import Mode, check_gate, posting_mode
from sixstars.posting.hourly import (
    SlotRun,
    preview_slot,
    record_decision,
    run_hourly_slot,
    schedule_hourly_slot,
)
from sixstars.posting.models import CLIP_APPROVED, Decision, Lane, ensure_utc
from sixstars.posting.precedence import clip_block
from sixstars.posting.publisher import Outcome, Publisher, PublishResult
from sixstars.posting.selection import SlotPlan
from sixstars.posting.slots import fire_at, slot_start
from sixstars.posting.store import DbStore, utcnow

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2

# The client used by the commands that can send. Tests replace it with the fake.
client_factory: Callable[[], UploadPostClient] = UploadPostClient


def _safe_stdout() -> None:
    """Never crash on a console that cannot show a character (emoji on a Windows code page)."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")


def _parse_slot(slot: str) -> datetime | None:
    return ensure_utc(datetime.fromisoformat(slot), "--slot") if slot else None


def decision_line(decision: Decision) -> str:
    clip = f" clip={decision.clip_id}" if decision.clip_id else ""
    return (
        f"  {decision.account_id:<16} {decision.action.value:<8} {decision.code.value}{clip}"
        f" :: {decision.reason}"
    )


def _print_plan(plan: SlotPlan, mode: Mode, fire: datetime) -> None:
    typer.echo(f"slot {plan.slot_at.isoformat()}  mode={mode.value}  planned time {fire.isoformat()}")
    for decision in plan.decisions:
        typer.echo(decision_line(decision))
    if plan.slot_empty:
        typer.echo("  slot is empty: no clip for any account")


def plan(slot: str = "") -> int:
    """Print the decisions of the slot without writing anything."""
    _safe_stdout()
    try:
        slot_at = _parse_slot(slot)
        now = utcnow()
        slot_plan, params = preview_slot(slot_at, now)
    except (KeyError, TypeError, ValueError) as exc:
        typer.echo(f"cannot plan: {exc}", err=True)
        return EXIT_ERROR
    mode, _ = posting_mode(now, params)
    _print_plan(slot_plan, mode, fire_at(slot_plan.slot_at, params.jitter_max_s))
    if mode is Mode.STOPPED:
        typer.echo("control.emergency_stop is on: a real run would do nothing")
    typer.echo("nothing was written or sent")
    return EXIT_OK


def _print_results(results: tuple[PublishResult, ...]) -> None:
    for result in results:
        typer.echo(f"  post {result.post_id}: {result.outcome.value} {result.detail}")


def hourly() -> int:
    """Run the current hourly slot now."""
    _safe_stdout()
    run: SlotRun = run_hourly_slot(slot_start(utcnow()), client_factory())
    if run.plan is None:
        typer.echo("control.emergency_stop is on: nothing was planned, recorded or sent")
        return EXIT_OK
    typer.echo(f"slot {run.slot_at.isoformat()}  mode={run.mode.value}")
    for decision in run.plan.decisions:
        typer.echo(decision_line(decision))
    typer.echo(f"{len(run.post_ids)} real posts, {len(run.dry_run_ids)} dry-run rows")
    _print_results(run.results)
    return EXIT_OK


def trigger() -> int:
    """Do what the hourly trigger does."""
    result = schedule_hourly_slot()
    fire = result.fire_at.isoformat() if result.fire_at else "-"
    typer.echo(f"{result.status}: slot {result.slot_at.isoformat()} at {fire} (job {result.job_id})")
    return EXIT_OK


def _plan_clip(store: DbStore, clip_id: str, account_id: str) -> tuple[str | None, bool, str]:
    """Decide and record one clip on one account (inside the planning lock). Returns the new post id,
    whether it is a real post, and the reason or problem as text."""
    now = utcnow()
    params = store.load_params()
    loaded = store.get_clip(clip_id)
    account = store.get_account(account_id)
    if loaded is None or account is None:
        return None, False, "unknown clip or account"
    wanted = loaded.clip
    lane = wanted.lane if wanted.lane in account.lanes else min(account.lanes, default=wanted.lane)
    slot = slot_start(now) if lane is Lane.HOURLY else None
    decision = replace(check_gate(account, lane, now, params), clip_id=clip_id, slot_at=slot)
    if not decision.would_post:
        return None, False, f"{decision.code.value}: {decision.reason}"
    if wanted.status != CLIP_APPROVED:
        return None, False, f"clip is {wanted.status!r}, not approved"
    history = store.load_history(now, [clip_id])
    blocked = clip_block(wanted, account, history, decision.dry_run)
    if blocked is not None:
        return None, False, blocked[1]
    state = cap_state(account, history, now, params, dry=decision.dry_run)
    if state.at_cap:
        return None, False, f"{account_id} is at its cap ({state.used_24h} of {state.cap})"
    if not state.gap_ok:
        return None, False, f"the minimum gap on {account_id} is not met yet"
    post_id, real = record_decision(store, decision, wanted, account, now, params)
    if post_id is None:
        return None, real, "a row for this clip and account already exists"
    return post_id, real, decision.reason


def clip(clip_id: str, account_id: str) -> int:
    """Staging only: post one approved clip to one account, through the gate, the cap and the gap."""
    _safe_stdout()
    if env().APP_ENV != "staging":
        typer.echo("`posting clip` runs in staging only (APP_ENV=staging)", err=True)
        return EXIT_USAGE
    store = DbStore()
    with store.planning_lock():
        post_id, real, text = _plan_clip(store, clip_id, account_id)
    if post_id is None:
        typer.echo(f"not posted: {text}", err=True)
        return EXIT_ERROR
    if not real:
        typer.echo(f"dry run recorded as post {post_id}: nothing was sent ({text})")
        return EXIT_OK
    result = Publisher(client_factory(), store).publish(post_id)
    _print_results((result,))
    return EXIT_OK if result.outcome in (Outcome.PUBLISHED, Outcome.PENDING) else EXIT_ERROR

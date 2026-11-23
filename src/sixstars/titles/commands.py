"""`sixstars titles ...`: run the title writer v2 by hand, compare old and new titles, re-title the queue."""

from collections.abc import Sequence
from pathlib import Path

import typer

from sixstars.titles import service, store


def _print(outcome: service.Outcome, verbose: bool) -> None:
    typer.echo(f"{outcome.clip_id}")
    typer.echo(f"  old: {outcome.prior_title}")
    mark = " (runner-up, exploring)" if outcome.explored else ""
    typer.echo(f"  new: {outcome.title}   [{outcome.pattern or 'fallback'}{mark}]")
    typer.echo(f"  caption: {outcome.caption}   tags: {' '.join('#' + t for t in outcome.hashtags)}")
    if verbose:
        for item in outcome.candidates:
            state = "ok  " if item.passed else "FAIL"
            typer.echo(f"    {state} {item.pattern:<21} {item.title}")
            for reason in item.failed:
                typer.echo(f"           - {reason}")


def retitle(clip_id: str, apply: bool) -> int:
    """One clip, with every candidate and the checks it failed."""
    outcome = service.retitle(clip_id, apply=apply)
    _print(outcome, verbose=True)
    if apply and not outcome.applied:
        typer.echo("  not applied: the clip is already posted")
    return 0


def _markdown(rows: Sequence[service.Outcome]) -> str:
    def cell(text: str | None) -> str:
        return (text or "").replace("|", "\\|")

    lines = [
        "| # | Old title | New title | Pattern | Caption (TikTok / Instagram) |",
        "|---|---|---|---|---|",
    ]
    for number, item in enumerate(rows, 1):
        pattern = (item.pattern or "fallback") + (" (explore)" if item.explored else "")
        lines.append(
            f"| {number} | {cell(item.prior_title)} | {cell(item.title)} | {pattern} | {cell(item.caption)} |"
        )
    return "\n".join(lines) + "\n"


def compare(limit: int, statuses: Sequence[str], out: str | None) -> int:
    """Dry run on the most recent clips: old and new title side by side. Nothing on the clips changes; each
    run is stored in `clip_titles` with `applied = false`."""
    outcomes: list[service.Outcome] = []
    failures = 0
    for clip_id in store.recent_ids(limit, statuses):
        try:
            outcome = service.retitle(clip_id, apply=False)
        except Exception as exc:  # noqa: BLE001 - one bad clip does not stop the comparison
            failures += 1
            typer.echo(f"{clip_id}: failed: {type(exc).__name__}: {exc}"[:300], err=True)
            continue
        outcomes.append(outcome)
        _print(outcome, verbose=False)
    if out:
        Path(out).write_text(_markdown(outcomes), encoding="utf-8")
        typer.echo(f"Wrote {len(outcomes)} rows to {out}")
    passed = sum(1 for item in outcomes if item.source == "v2")
    typer.echo(
        f"{len(outcomes)} clips: {passed} v2 titles, {len(outcomes) - passed} fallbacks, {failures} failed"
    )
    return 0 if not failures else 1


def queue(limit: int, apply: bool) -> int:
    """Re-title approved clips waiting to post that have no v2 title yet."""
    ids = store.queue_ids(limit)
    for clip_id in ids:
        try:
            _print(service.retitle(clip_id, apply=apply), verbose=False)
        except Exception as exc:  # noqa: BLE001 - keep going; the clip keeps its title
            typer.echo(f"{clip_id}: failed: {type(exc).__name__}: {exc}"[:300], err=True)
    typer.echo(f"{len(ids)} clips {'re-titled' if apply else 'checked (dry run)'}")
    return 0

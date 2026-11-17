"""Implementation of the `sixstars gate` commands (M07).

- `run <clip_id>`: judge one clip that is in `gating` (what the factory hook does).
- `pending`: judge every clip still in `gating` (recovery after a crash or a lost job).
- `explain <clip_id>`: print the stored verdict of a clip. Read-only.
- `check-file <video>`: measure a rendered file (frame size, audio, black and frozen stretches) and, with
  `--p5`, ask the vision model about four of its frames. Writes nothing to `clips`.

`run` and `pending` hand an approved spike clip to the posting engine exactly as the worker does (the
posting engine applies dry run and the emergency stop); `--no-post` leaves it approved without that step.
"""

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Final

import typer

from sixstars import db
from sixstars.gate import gate, profiles
from sixstars.gate import visual as visuals
from sixstars.gate.gate import GateOutcome
from sixstars.gate.store import file_as_json

EXIT_OK: Final = 0
EXIT_REJECTED: Final = 1
EXIT_USAGE: Final = 2


def _safe_stdout() -> None:
    """Never crash on a console that cannot show a character (emoji on a Windows code page)."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")


def _deps(post: bool) -> gate.GateDeps:
    return replace(gate.current_deps(), hand_off=post)


def _print(outcome: GateOutcome) -> None:
    if outcome.decision is None:
        typer.echo(f"{outcome.clip_id}: not judged ({outcome.skipped})")
        return
    decision = outcome.decision
    line = f"{outcome.clip_id}: {decision.verdict.value} profile={decision.profile} lane={decision.lane}"
    if decision.demoted:
        failed = ", ".join(code.value for code in decision.express_codes)
        line += f" (moved from the spike lane: {failed})"
    typer.echo(line)
    for code, reason in zip(decision.codes, decision.reasons, strict=True):
        typer.echo(f"  {code.value}: {reason}")
    if outcome.handoff_error:
        typer.echo(f"  hand-off to posting failed, a retry job is queued: {outcome.handoff_error}")
    elif outcome.spike_run is not None:
        run = outcome.spike_run
        typer.echo(f"  posting: mode={run.mode.value} posts={len(run.post_ids)} dry={len(run.dry_run_ids)}")


def run(clip_id: str, post: bool = True) -> int:
    """Judge one clip. Exit code 0 when it was approved or held, 1 when rejected, 2 when not judged."""
    _safe_stdout()
    try:
        outcome = gate.run_gate(clip_id, deps=_deps(post))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    _print(outcome)
    if outcome.decision is None:
        return EXIT_USAGE
    return EXIT_REJECTED if outcome.status == "rejected" else EXIT_OK


def pending(older_than_s: float = 0.0, post: bool = True) -> int:
    """Judge every clip in `gating`."""
    _safe_stdout()
    outcomes = gate.run_pending(older_than_s, deps=_deps(post))
    for outcome in outcomes:
        _print(outcome)
    typer.echo(f"{len(outcomes)} clips judged")
    return EXIT_OK


def explain(clip_id: str) -> int:
    """Print the stored verdict of a clip."""
    _safe_stdout()
    try:
        row = db.fetch_one(
            "select status, lane, gate_profile, reject_reason, gate_result from clips where id = %s",
            (clip_id,),
        )
    except Exception as exc:  # noqa: BLE001 - a malformed id is a usage error, not a crash
        typer.echo(f"cannot read clip {clip_id}: {type(exc).__name__}", err=True)
        return EXIT_USAGE
    if row is None:
        typer.echo(f"clip {clip_id} does not exist", err=True)
        return EXIT_USAGE
    typer.echo(f"{clip_id}: status={row['status']} lane={row['lane']} profile={row['gate_profile']}")
    result = row["gate_result"]
    if not isinstance(result, dict):
        typer.echo("  the gate has not judged this clip")
        return EXIT_OK
    typer.echo(f"  verdict={result.get('verdict')} precheck={result.get('precheck')}")
    codes, reasons = result.get("codes") or [], result.get("reasons") or []
    for index, code in enumerate(codes):
        typer.echo(f"  {code}: {reasons[index] if index < len(reasons) else ''}")
    checks = result.get("checks")
    for name, check in checks.items() if isinstance(checks, dict) else ():
        mark = "ok  " if check.get("ok") else "FAIL"
        typer.echo(
            f"  {mark} {name}: value={json.dumps(check.get('value'))} limit={json.dumps(check.get('limit'))}"
        )
    return EXIT_OK


def check_file(video: str, duration_s: float | None, p5: bool) -> int:
    """Measure a file and optionally ask P5. Exit code 1 when P5 flags something."""
    _safe_stdout()
    path = Path(video)
    if not path.is_file():
        typer.echo(f"{video} is not a file", err=True)
        return EXIT_USAGE
    settings = profiles.load()
    facts = visuals.measure_file(path, duration_s, settings)
    typer.echo(json.dumps({"file": file_as_json(facts)}))
    if not p5 or facts is None or facts.duration_s is None:
        return EXIT_OK
    clip_s = duration_s if duration_s else max(facts.duration_s - settings.end_card_s, 0.1)
    findings = visuals.look_at_file(path, clip_s, lane="vod", clip_id=f"check-file:{path.name}")
    document = visuals.findings_as_json(findings)
    typer.echo(json.dumps({"p5": document}))
    flagged = [
        name
        for name, value in document.items()
        if (value is True and name != "readable_subtitles")
        or (name == "readable_subtitles" and value is False)
    ]
    typer.echo("P5: " + ("flags " + ", ".join(flagged) if flagged else "nothing flagged"))
    return EXIT_REJECTED if flagged else EXIT_OK

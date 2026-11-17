"""Failover for the n8n schedules (docs/12 section 6).

n8n fires four triggers: discovery, the hourly slot, the guardian check and stats. Each call writes the
heartbeat `trigger:<name>`. When one is older than its grace time in `guardian.failover_grace_s`, the
scheduler runs that job itself and keeps doing so (the hourly slot once per slot, the others every
`guardian.failover_interval_s`) until n8n calls again.

A heartbeat written by a failover run carries `{"source": "failover"}`. That is how the two are told
apart: while the latest heartbeat has that mark, n8n is still missing, however fresh the heartbeat is.
The owner is alerted once when failover starts and once when n8n is back (`incidents.py`).

docs/12 names only the hourly slot and discovery. The guardian check and stats are covered as well,
because a silent n8n must not switch off claim detection.
"""

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sixstars import db
from sixstars.guardian import incidents
from sixstars.guardian.config import TRIGGER_PREFIX, TRIGGERS
from sixstars.logging import get_logger
from sixstars.posting.slots import slot_start

SERVICE = "guardian"
SOURCE_KEY = "source"
SOURCE_FAILOVER = "failover"

Runner = Callable[[datetime], Mapping[str, Any] | None]


@dataclass(frozen=True, slots=True)
class TriggerState:
    """The heartbeat of one trigger. `last_seen` is None when it was never written."""

    name: str
    last_seen: datetime | None = None
    info: Mapping[str, Any] = field(default_factory=dict)

    @property
    def by_failover(self) -> bool:
        return self.info.get(SOURCE_KEY) == SOURCE_FAILOVER


def trigger_missing(state: TriggerState, now: datetime, grace_s: float, started_at: datetime) -> bool:
    """n8n is missing when the last heartbeat came from a failover run, or when nothing arrived for
    longer than the grace time. A trigger that never fired is measured from the scheduler's start."""
    if state.by_failover:
        return True
    reference = state.last_seen or started_at
    return (now - reference).total_seconds() > grace_s


def failover_due(
    state: TriggerState, now: datetime, grace_s: float, interval_s: float, started_at: datetime
) -> bool:
    """Should the scheduler run the job now?

    Not while n8n is alive. Once it is missing: at once, then the hourly slot once per slot and the other
    jobs every `interval_s`.
    """
    if not trigger_missing(state, now, grace_s, started_at):
        return False
    if not state.by_failover or state.last_seen is None:
        return True
    if state.name == "hourly":
        return state.info.get("slot_at") != slot_start(now).isoformat()
    return (now - state.last_seen).total_seconds() >= interval_s


def load_states() -> dict[str, TriggerState]:
    rows = db.fetch_all(
        "select name, last_seen, info from heartbeats where name = any(%s)",
        ([TRIGGER_PREFIX + name for name in TRIGGERS],),
    )
    states = {name: TriggerState(name) for name in TRIGGERS}
    for row in rows:
        name = str(row["name"]).removeprefix(TRIGGER_PREFIX)
        info = row["info"] if isinstance(row["info"], dict) else {}
        states[name] = TriggerState(name, row["last_seen"], info)
    return states


def mark(name: str, info: Mapping[str, Any] | None = None) -> None:
    """Write the trigger heartbeat as a failover run."""
    db.heartbeat(TRIGGER_PREFIX + name, {**dict(info or {}), SOURCE_KEY: SOURCE_FAILOVER})


class Failover:
    """Runs the missing triggers in background threads, one at a time per trigger, so a slow job never
    blocks the scheduler loop."""

    def __init__(self, runners: Mapping[str, Runner], started_at: datetime) -> None:
        self.runners = dict(runners)
        self.started_at = started_at
        self._threads: dict[str, threading.Thread] = {}

    def busy(self, name: str) -> bool:
        thread = self._threads.get(name)
        return thread is not None and thread.is_alive()

    def _run(self, name: str, now: datetime) -> None:
        log = get_logger(SERVICE)
        try:
            # Marked before the job runs, so a long job is not started twice.
            mark(name, {"slot_at": slot_start(now).isoformat()} if name == "hourly" else None)
            info = self.runners[name](now)
            if info is not None:
                mark(name, info)
            log.warning("failover_ran", trigger=name)
        except Exception as exc:
            log.error("failover_failed", trigger=name, error=type(exc).__name__, exc_info=True)

    def check(
        self,
        now: datetime,
        grace: Mapping[str, float],
        interval: Mapping[str, float],
        enabled: bool,
        open_now: set[str] | None = None,
        wait: bool = False,
    ) -> list[str]:
        """One pass: alert about missing triggers and start the jobs that are due. Returns the names of
        the jobs started. `wait=True` runs them in this thread (tests and the CLI)."""
        states = load_states()
        started: list[str] = []
        for name in TRIGGERS:
            grace_s = grace.get(name)
            if grace_s is None:
                continue  # no grace time configured: this trigger is not watched
            state = states[name]
            missing = trigger_missing(state, now, grace_s, self.started_at)
            last = state.last_seen.isoformat() if state.last_seen else "never"
            doing = "the scheduler runs it instead" if enabled and name in self.runners else "nothing runs it"
            incidents.report(
                TRIGGER_PREFIX + name,
                missing,
                severity="critical",
                title=f"n8n trigger missing: {name}",
                body=f"No call from n8n for more than {grace_s:g} s (last: {last}); {doing}. "
                "Check that the n8n workflow is active.",
                recovered_title=f"n8n trigger is back: {name}",
                data={"trigger": name, "grace_s": grace_s, "failover": enabled},
                open_now=open_now,
            )
            if not (enabled and name in self.runners) or self.busy(name):
                continue
            if failover_due(state, now, grace_s, interval.get(name, grace_s), self.started_at):
                started.append(name)
                if wait:
                    self._run(name, now)
                else:
                    thread = threading.Thread(
                        target=self._run, args=(name, now), name=f"failover-{name}", daemon=True
                    )
                    self._threads[name] = thread
                    thread.start()
        return started

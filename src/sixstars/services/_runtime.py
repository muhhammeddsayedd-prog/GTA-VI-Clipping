"""Shared pieces of the long-running services: stop signals and a loop of periodic tasks."""

import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import FrameType

from structlog.typing import FilteringBoundLogger

TICK_S = 1.0


def install_stop_signals(stop: threading.Event, log: FilteringBoundLogger) -> None:
    """Set `stop` on SIGINT or SIGTERM. Does nothing outside the main thread (tests run services in
    threads and set the event themselves)."""
    if threading.current_thread() is not threading.main_thread():
        return

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("stop_signal", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)


@dataclass(slots=True)
class PeriodicTask:
    """A function that the service loop calls every `interval_s` seconds, starting with the first tick."""

    name: str
    interval_s: float
    run: Callable[[], object]
    next_at: float = 0.0

    def due(self, now: float) -> bool:
        return now >= self.next_at

    def schedule_next(self, now: float) -> None:
        self.next_at = now + self.interval_s


def run_due(tasks: Sequence[PeriodicTask], now: float, log: FilteringBoundLogger) -> list[str]:
    """Run every task that is due at `now` and return their names. A task that raises is logged and
    tried again after its interval; it never stops the loop or the other tasks."""
    ran: list[str] = []
    for task in tasks:
        if not task.due(now):
            continue
        task.schedule_next(now)
        ran.append(task.name)
        try:
            task.run()
        except Exception as exc:
            log.error("periodic_task_failed", task=task.name, error=type(exc).__name__, exc_info=True)
    return ran


def run_loop(
    tasks: Sequence[PeriodicTask],
    stop: threading.Event,
    log: FilteringBoundLogger,
    tick_s: float = TICK_S,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Call the due tasks once per tick until `stop` is set. The wait between ticks ends at once on stop."""
    while not stop.is_set():
        run_due(tasks, clock(), log)
        stop.wait(tick_s)

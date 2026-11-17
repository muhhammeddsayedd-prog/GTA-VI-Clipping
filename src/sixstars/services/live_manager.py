"""Process entry point for the live manager service: live recorders, chat readers and live spike
detection (M14). The work is in `sixstars.live.manager.LiveManager`; this module is the loop around it.

One process supervises up to `live.max_slots` sessions. Each session is two child processes (streamlink
and ffmpeg, a stream copy) plus a chat thread; the service loop polls them once a second and writes the
`live-manager` heartbeat every 30 seconds, with `started_at` so the guardian can count restarts.

With `live.max_slots = 0` the service starts nothing and only heartbeats.

Shutdown. On SIGINT or SIGTERM the loop ends, every recorder and chat reader is stopped in parallel
(`SHUTDOWN_JOIN_S`, well inside the 60 s `stop_grace_period` of docker compose) and the session rows stay
`recording`, so the next start resumes them.
"""

import threading

from sixstars import db
from sixstars.live.manager import SERVICE, SHUTDOWN_JOIN_S, LiveManager
from sixstars.logging import configure_logging, get_logger
from sixstars.services._runtime import PeriodicTask, install_stop_signals, run_loop

HEARTBEAT_INTERVAL_S = 30.0
SESSION_TICK_S = 1.0

__all__ = ["HEARTBEAT_INTERVAL_S", "SERVICE", "SESSION_TICK_S", "run"]


def run(
    stop_event: threading.Event | None = None,
    tick_s: float = 1.0,
    manager: LiveManager | None = None,
    shutdown_join_s: float = SHUTDOWN_JOIN_S,
) -> None:
    """Run the live manager until a stop signal arrives (or `stop_event` is set)."""
    configure_logging()
    log = get_logger(SERVICE)
    stop = stop_event or threading.Event()
    install_stop_signals(stop, log)
    live = manager or LiveManager()

    def heartbeat() -> None:
        db.heartbeat(SERVICE, live.info())

    log.info("live_manager_started")
    try:
        run_loop(
            [
                PeriodicTask("sessions", min(SESSION_TICK_S, tick_s), live.tick),
                PeriodicTask("heartbeat", HEARTBEAT_INTERVAL_S, heartbeat),
            ],
            stop,
            log,
            tick_s=tick_s,
        )
    finally:
        try:
            live.shutdown(shutdown_join_s)
        finally:
            log.info("live_manager_stopped")
            db.close_pool()

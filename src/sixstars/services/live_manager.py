"""Process entry point for the live manager service: live recorders, chat readers and live spike detection.

Until M14 this is a stub: it writes the `live-manager` heartbeat and does nothing else. It keeps running
(docker compose restarts a service that exits) and stops cleanly on SIGINT or SIGTERM.
"""

import os
import threading

from sixstars import db
from sixstars.logging import configure_logging, get_logger
from sixstars.services._runtime import PeriodicTask, install_stop_signals, run_loop

SERVICE = "live-manager"
HEARTBEAT_INTERVAL_S = 30.0


def heartbeat() -> None:
    db.heartbeat(SERVICE, {"pid": os.getpid(), "stub": True})


def run(stop_event: threading.Event | None = None, tick_s: float = 1.0) -> None:
    """Run the live manager until a stop signal arrives (or `stop_event` is set)."""
    configure_logging()
    log = get_logger(SERVICE)
    stop = stop_event or threading.Event()
    install_stop_signals(stop, log)
    log.info("live_manager_started", stub=True)
    try:
        run_loop([PeriodicTask("heartbeat", HEARTBEAT_INTERVAL_S, heartbeat)], stop, log, tick_s=tick_s)
    finally:
        log.info("live_manager_stopped")
        db.close_pool()

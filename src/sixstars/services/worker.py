"""Process entry point for a queue worker (`express` or `normal`) that claims and runs jobs from the `jobs`
table. Implemented in M01 and extended by later milestones.

Later milestones add their job handlers with `register()`. `run()` blocks until the process receives
SIGINT or SIGTERM, lets the current job finish, then closes the database pool.
"""

import signal
import threading
from types import FrameType
from typing import cast, get_args

from sixstars import db, jobs
from sixstars.logging import configure_logging, get_logger

HANDLERS: dict[str, jobs.Handler] = {}


def register(job_type: str, handler: jobs.Handler) -> None:
    """Register the handler for one job type. Registering the same type twice is an error."""
    if job_type in HANDLERS:
        raise ValueError(f"a handler for job type {job_type!r} is already registered")
    HANDLERS[job_type] = handler


def run(queue: str, stop_event: threading.Event | None = None) -> None:
    """Run a worker for `queue` until a stop signal arrives (or `stop_event` is set)."""
    if queue not in get_args(jobs.Queue):
        raise ValueError(f"unknown queue {queue!r}: expected one of {get_args(jobs.Queue)}")
    configure_logging()
    log = get_logger("worker")
    stop = stop_event or threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("worker_stop_signal", signal=signum)
        stop.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    try:
        jobs.run_worker(cast(jobs.Queue, queue), HANDLERS, stop)
    finally:
        db.close_pool()

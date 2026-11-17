"""Postgres job queue on the `jobs` table: enqueue, claim (`FOR UPDATE SKIP LOCKED` via `claim_job()`),
complete, fail and retry. This is the only mechanism for background work; there is no Celery or Redis.
Implemented in M01.

Queues are `express` and `normal`. A claim takes the queued job with the highest priority whose
`run_after` has passed. A failed job goes back to the queue after 30 s, then 120 s, then 600 s, until it
has been attempted `max_attempts` times; then it is marked `failed`. `dedupe_key` is unique among queued
and running jobs, so the same piece of work is never queued twice.
"""

import os
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from psycopg.types.json import Jsonb

from sixstars import db
from sixstars.logging import bind_context, clear_context, get_logger

Queue = Literal["express", "normal"]
Handler = Callable[["Job"], None]

RETRY_BACKOFF_S: tuple[int, ...] = (30, 120, 600)
IDLE_SLEEP_S = 1.0
HEARTBEAT_INTERVAL_S = 30.0
MAX_ERROR_CHARS = 2000


@dataclass(frozen=True, slots=True)
class Job:
    """One row of the `jobs` table as claimed by a worker."""

    id: int
    queue: str
    type: str
    payload: dict[str, Any]
    priority: int
    status: str
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_by: str | None
    locked_at: datetime | None
    last_error: str | None
    dedupe_key: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Job":
        """Build a `Job` from a `jobs` row dict."""
        return cls(**{name: row[name] for name in cls.__slots__})


def enqueue(
    queue: Queue,
    type: str,  # noqa: A002
    payload: Mapping[str, Any] | None = None,
    priority: int = 0,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
) -> int | None:
    """Add a job and return its id, or None when a queued or running job has the same `dedupe_key`.

    Higher `priority` is claimed first. `run_after` (timezone-aware) delays the job; None means now.
    """
    row = db.fetch_one(
        """
        insert into jobs (queue, type, payload, priority, run_after, dedupe_key, max_attempts)
        values (%s, %s, %s, %s, coalesce(%s, now()), %s, %s)
        on conflict (dedupe_key) where dedupe_key is not null and status in ('queued', 'running')
        do nothing
        returning id
        """,
        (queue, type, Jsonb(dict(payload or {})), priority, run_after, dedupe_key, max_attempts),
    )
    return int(row["id"]) if row is not None else None


def claim(queue: Queue, worker_name: str) -> Job | None:
    """Claim the next runnable job of `queue` for `worker_name`, or return None when there is none."""
    row = db.fetch_one("select * from claim_job(%s, %s)", (queue, worker_name))
    return Job.from_row(row) if row is not None else None


def get(job_id: int) -> Job | None:
    """Return the current state of one job, or None when it does not exist."""
    row = db.fetch_one("select * from jobs where id = %s", (job_id,))
    return Job.from_row(row) if row is not None else None


def complete(job_id: int) -> None:
    """Mark a running job as done."""
    db.execute(
        "update jobs set status = 'done', last_error = null where id = %s and status = 'running'",
        (job_id,),
    )


def fail(job_id: int, error: str, retry: bool = True) -> str | None:
    """Record a failure of a running job. Returns the new status: `queued` (will retry) or `failed`.

    With `retry=True` and attempts left, the job is queued again after the backoff for its attempt
    number (30 s, 120 s, 600 s; later attempts reuse the last value). Otherwise it becomes `failed`.
    Returns None when the job is not running (already finished or cancelled).
    """
    row = db.fetch_one(
        """
        update jobs
        set status = case when %(retry)s and attempts < max_attempts then 'queued' else 'failed' end,
            run_after = case
              when %(retry)s and attempts < max_attempts
              then now() + make_interval(
                secs => (%(backoff)s::int[])[least(attempts, cardinality(%(backoff)s::int[]))]
              )
              else run_after
            end,
            last_error = %(error)s,
            locked_by = null,
            locked_at = null
        where id = %(id)s and status = 'running'
        returning status
        """,
        {
            "id": job_id,
            "retry": retry,
            "error": error[:MAX_ERROR_CHARS],
            "backoff": list(RETRY_BACKOFF_S),
        },
    )
    return str(row["status"]) if row is not None else None


def cancel(dedupe_key: str) -> int:
    """Cancel queued jobs with this `dedupe_key`; return how many were cancelled.

    A job that is already running is not interrupted and keeps its status.
    """
    return db.execute(
        "update jobs set status = 'cancelled' where dedupe_key = %s and status = 'queued'",
        (dedupe_key,),
    )


def worker_name(queue: str) -> str:
    """Name of this worker process: `worker-<queue>-<hostname>-<pid>`."""
    return f"worker-{queue}-{socket.gethostname()}-{os.getpid()}"


def _heartbeat_loop(name: str, queue: str, stop_event: threading.Event, interval_s: float) -> None:
    """Write a heartbeat for `name` now and then every `interval_s` seconds until `stop_event` is set."""
    log = get_logger("worker")
    while True:
        try:
            db.heartbeat(name, {"queue": queue, "pid": os.getpid()})
        except Exception as exc:
            log.warning("worker_heartbeat_failed", worker=name, error=type(exc).__name__)
        if stop_event.wait(interval_s):
            return


def _run_job(job: Job, handlers: Mapping[str, Handler]) -> None:
    """Dispatch one claimed job to its handler and record the result."""
    log = get_logger("worker")
    bind_context(job_id=job.id)
    try:
        handler = handlers.get(job.type)
        if handler is None:
            log.error("job_unknown_type", job_type=job.type)
            fail(job.id, f"no handler for job type {job.type!r}", retry=False)
            return
        log.info("job_started", job_type=job.type, queue=job.queue, attempt=job.attempts)
        try:
            handler(job)
        except Exception as exc:
            status = fail(job.id, f"{type(exc).__name__}: {exc}")
            log.error("job_failed", job_type=job.type, error=type(exc).__name__, status=status, exc_info=True)
            return
        complete(job.id)
        log.info("job_done", job_type=job.type)
    finally:
        clear_context()


def run_worker(
    queue: Queue,
    handlers: Mapping[str, Handler],
    stop_event: threading.Event,
    idle_sleep_s: float = IDLE_SLEEP_S,
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
) -> None:
    """Claim and run jobs of `queue` until `stop_event` is set.

    Each job is dispatched to `handlers[job.type]`. A handler that returns completes the job; one that
    raises fails it with retry. A job whose type has no handler fails without retry. When the queue is
    empty the loop waits `idle_sleep_s`. A background thread writes the heartbeat
    `worker-<queue>-<hostname>-<pid>` every `heartbeat_interval_s`, also while a long job runs.
    """
    log = get_logger("worker")
    name = worker_name(queue)
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(name, queue, heartbeat_stop, heartbeat_interval_s),
        name=f"{name}-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    log.info("worker_started", worker=name, queue=queue, job_types=sorted(handlers))
    try:
        while not stop_event.is_set():
            try:
                job = claim(queue, name)
            except Exception as exc:
                log.error("worker_claim_failed", worker=name, error=type(exc).__name__)
                stop_event.wait(idle_sleep_s)
                continue
            if job is None:
                stop_event.wait(idle_sleep_s)
                continue
            try:
                _run_job(job, handlers)
            except Exception as exc:
                log.error("worker_job_bookkeeping_failed", job_id=job.id, error=type(exc).__name__)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=5)
        log.info("worker_stopped", worker=name, queue=queue)

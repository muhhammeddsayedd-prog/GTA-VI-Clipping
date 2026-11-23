"""Process entry point for a queue worker (`express` or `normal`) that claims and runs jobs from the `jobs`
table. Implemented in M01, completed in M11.

Handlers. Every package that owns job types has a function that calls `worker.register(job_type,
handler)` for each of its types. `HANDLER_MODULES` lists those functions as "module" (function `register`)
or "module:function"; `register_all()` calls each one at start-up. A package adds one line to that list.
An entry whose module or function does not exist yet is skipped.

Shutdown. On SIGINT or SIGTERM the worker stops claiming and lets the current job finish. docker compose
kills the container after `stop_grace_period` (60 s), so after `SHUTDOWN_GRACE_S` the worker gives the
job back to the queue (`jobs.release`) and exits; another worker runs it again. Handlers must therefore
be safe to run twice, as every posting handler is. A worker that is killed outright leaves its job
`running`; the scheduler recovers it (`jobs.requeue_stale`).
"""

import importlib
import threading
from typing import cast, get_args

from sixstars import db, jobs, notify
from sixstars.logging import configure_logging, get_logger
from sixstars.services._runtime import install_stop_signals

HANDLERS: dict[str, jobs.Handler] = {}

# Where each package registers its job handlers: "module" (its function `register`) or
# "module:function". To add a package, add one line here. Entries whose module or function does not
# exist (yet) are skipped.
HANDLER_MODULES: tuple[str, ...] = (
    "sixstars.posting.handlers",  # M10: post.hourly, post.retry
    "sixstars.factory.pipeline:register_handlers",  # M06
    "sixstars.factory.variants:register_handlers",  # M18: clip.variants
    "sixstars.covers.miner:register_handlers",  # cover engine C3: cover.mine
    "sixstars.gate.gate:install",  # M07
    "sixstars.vod:register_handlers",  # M08
    "sixstars.discovery.tiers:register_handlers",  # M09
    "sixstars.precheck.handlers",  # M07
    "sixstars.live.handlers",  # M14
    "sixstars.guardian.handlers",  # M13
    "sixstars.stats.handlers",  # M13
    "sixstars.viewer_clips:register_handlers",  # D110: viewer_clip.process
    "sixstars.ai.shadow:register_handlers",  # AI cost plan B1: ai.shadow
)
SHUTDOWN_GRACE_S = 45.0  # below the 60 s `stop_grace_period` of docker compose
JOIN_POLL_S = 0.5

_registered_modules: set[str] = set()


def register(job_type: str, handler: jobs.Handler) -> None:
    """Register the handler for one job type. Registering the same type twice is an error."""
    if job_type in HANDLERS:
        raise ValueError(f"a handler for job type {job_type!r} is already registered")
    HANDLERS[job_type] = handler


def _is_missing(module_name: str, exc: ModuleNotFoundError) -> bool:
    """True when `exc` says that `module_name` itself (or its package) does not exist, as opposed to a
    module that exists and imports something that is not installed."""
    missing = exc.name or ""
    return bool(missing) and (module_name == missing or module_name.startswith(missing + "."))


def register_all(modules: tuple[str, ...] = HANDLER_MODULES) -> list[str]:
    """Register the job handlers of every package that has some. Returns the modules registered by this
    call. Safe to call twice: a module is registered once per process.

    A module that does not exist, or has no `register` function, is skipped. A module that exists but
    fails to import or to register is an error: it is logged and reported to Telegram, and the worker
    continues with the handlers of the other packages rather than stopping all background work.
    """
    log = get_logger("worker")
    done: list[str] = []
    for entry in modules:
        if entry in _registered_modules:
            continue
        name, _, function = entry.partition(":")
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if _is_missing(name, exc):
                log.debug("handlers_module_absent", module=name)
            else:
                _report_broken(name, exc)
            continue
        except Exception as exc:
            _report_broken(name, exc)
            continue
        register_fn = getattr(module, function or "register", None)
        if not callable(register_fn):
            log.debug("handlers_module_without_register", module=name)
            continue
        try:
            register_fn()
        except Exception as exc:
            _report_broken(name, exc)
            continue
        _registered_modules.add(entry)
        done.append(entry)
    log.info("handlers_registered", modules=done, job_types=sorted(HANDLERS))
    return done


def _report_broken(module_name: str, exc: Exception) -> None:
    get_logger("worker").error(
        "handlers_module_failed", module=module_name, error=type(exc).__name__, exc_info=True
    )
    notify.send(
        "critical",
        "Worker could not load job handlers",
        f"{module_name}: {type(exc).__name__}. Jobs of that package will fail until this is fixed.",
    )


class _ReleasedJob(BaseException):  # noqa: N818
    """Raised in the worker loop when the handler of a released job returns. It is not an `Exception`, so
    the queue code neither completes nor fails the job (it may belong to another worker by now) and the
    loop ends."""


class _CurrentJob:
    """Remembers the job the worker is running, so a shutdown that cannot wait can give it back."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: jobs.Job | None = None
        self._released: set[int] = set()

    def wrap(self, handler: jobs.Handler) -> jobs.Handler:
        def tracked(job: jobs.Job) -> None:
            with self._lock:
                self._job = job
            try:
                handler(job)
            finally:
                with self._lock:
                    self._job = None
                    released = job.id in self._released
                if released:
                    raise _ReleasedJob

        return tracked

    def release(self) -> jobs.Job | None:
        """Give the running job back to the queue. Returns it, or None when no job was running."""
        with self._lock:
            job = self._job
            if job is None or job.locked_by is None:
                return None
            self._released.add(job.id)
        return job if jobs.release(job.id, job.locked_by) else None


def run(queue: str, stop_event: threading.Event | None = None, grace_s: float = SHUTDOWN_GRACE_S) -> None:
    """Run a worker for `queue` until a stop signal arrives (or `stop_event` is set).

    After the stop the current job has `grace_s` seconds to finish; then it is released to the queue.
    """
    if queue not in get_args(jobs.Queue):
        raise ValueError(f"unknown queue {queue!r}: expected one of {get_args(jobs.Queue)}")
    configure_logging()
    log = get_logger("worker")
    stop = stop_event or threading.Event()
    install_stop_signals(stop, log)
    register_all()
    current = _CurrentJob()
    handlers = {job_type: current.wrap(handler) for job_type, handler in HANDLERS.items()}
    crashed: list[BaseException] = []

    def loop() -> None:
        try:
            jobs.run_worker(cast(jobs.Queue, queue), handlers, stop)
        except _ReleasedJob:
            pass
        except BaseException as exc:
            crashed.append(exc)

    thread = threading.Thread(target=loop, name=f"worker-{queue}-loop", daemon=True)
    thread.start()
    try:
        # Short joins keep the main thread free to receive the stop signal.
        while thread.is_alive() and not stop.is_set():
            thread.join(JOIN_POLL_S)
        thread.join(grace_s)
        if thread.is_alive():
            job = current.release()
            log.warning(
                "worker_shutdown_grace_expired",
                grace_s=grace_s,
                job_id=job.id if job else None,
                job_type=job.type if job else None,
                released=job is not None,
            )
        if crashed:
            raise crashed[0]
    finally:
        db.close_pool()

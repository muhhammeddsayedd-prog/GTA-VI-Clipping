"""Process entry point for a queue worker (`express` or `normal`) that claims and runs jobs from the `jobs`
table. Implemented in M01, completed in M11.

Handlers. Every package that owns job types has a module `<package>.handlers` with a function
`register()` that calls `worker.register(job_type, handler)` for each of its types. `register_all()` goes
through `HANDLER_MODULES` at start-up and calls each one. A later milestone only adds its `handlers.py`
(the module names of every planned package are already listed); a package whose module does not exist yet
is skipped.

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

# One `handlers` module per package that owns job types. Missing modules are skipped.
HANDLER_MODULES: tuple[str, ...] = (
    "sixstars.posting.handlers",  # M10: post.hourly, post.retry
    "sixstars.vod.handlers",  # M08
    "sixstars.factory.handlers",  # M06
    "sixstars.gate.handlers",  # M07
    "sixstars.precheck.handlers",  # M07
    "sixstars.discovery.handlers",  # M09
    "sixstars.live.handlers",  # M14
    "sixstars.guardian.handlers",  # M13
    "sixstars.stats.handlers",  # M13
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
    for name in modules:
        if name in _registered_modules:
            continue
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
        register_fn = getattr(module, "register", None)
        if not callable(register_fn):
            log.debug("handlers_module_without_register", module=name)
            continue
        try:
            register_fn()
        except Exception as exc:
            _report_broken(name, exc)
            continue
        _registered_modules.add(name)
        done.append(name)
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

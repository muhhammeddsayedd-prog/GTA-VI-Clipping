"""Trigger routes that n8n calls on a schedule: discovery, the hourly slot and the M13 placeholders.

Every trigger writes its heartbeat (`trigger:discovery`, `trigger:hourly`, `trigger:guardian`,
`trigger:stats`) when the call arrives, also while the work behind it is not built yet, so the heartbeat
always means "n8n is calling".
"""

import asyncio
import dataclasses
import importlib
import threading
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from sixstars import db
from sixstars.api.errors import ApiError
from sixstars.logging import get_logger
from sixstars.posting.hourly import schedule_hourly_slot

router = APIRouter(prefix="/v1")

DISCOVERY_MODULE = "sixstars.discovery.run"
DISCOVERY_FUNCTION = "run_discovery"
DISCOVERY_TIMEOUT_S = 60.0  # docs/17: the route answers within 60 s
HEARTBEAT_DISCOVERY = "trigger:discovery"
HEARTBEAT_GUARDIAN = "trigger:guardian"
HEARTBEAT_STATS = "trigger:stats"

# One discovery pass at a time in this process. A pass that outlives its 60 s keeps the lock until it ends.
_discovery_lock = threading.Lock()


def not_implemented(milestone: str, what: str) -> ApiError:
    return ApiError(
        501,
        "not_implemented",
        f"{what} is not implemented yet (arrives with {milestone})",
        extra={"milestone": milestone},
    )


def find_run_discovery() -> Callable[[], Any] | None:
    """`sixstars.discovery.run.run_discovery`, or None when it cannot be imported."""
    try:
        module = importlib.import_module(DISCOVERY_MODULE)
    except Exception as exc:
        get_logger("api").error("discovery_import_failed", error=type(exc).__name__, exc_info=True)
        return None
    function = getattr(module, DISCOVERY_FUNCTION, None)
    return function if callable(function) else None


def summary_to_json(summary: Any) -> dict[str, Any]:
    """The discovery summary as a JSON object: a dataclass, a pydantic model or a mapping is passed
    through; anything else is wrapped as `{"summary": ...}`."""
    to_dict = getattr(summary, "to_dict", None)
    if callable(to_dict):
        summary = to_dict()
    elif dataclasses.is_dataclass(summary) and not isinstance(summary, type):
        summary = dataclasses.asdict(summary)
    encoded = jsonable_encoder(summary)
    return encoded if isinstance(encoded, dict) else {"summary": encoded}


def _locked_run(run: Callable[[], Any]) -> Any:
    try:
        return run()
    except Exception as exc:
        # Also logged here because nobody is waiting for the result after a timeout.
        get_logger("api").error("discovery_run_failed", error=type(exc).__name__)
        raise
    finally:
        _discovery_lock.release()


@router.post("/discovery/run")
async def discovery_run() -> JSONResponse:
    """Run one discovery pass in a worker thread and answer with its summary."""
    await asyncio.to_thread(db.heartbeat, HEARTBEAT_DISCOVERY)
    run = find_run_discovery()
    if run is None:
        raise not_implemented("M09", "discovery")
    if not _discovery_lock.acquire(blocking=False):
        raise ApiError(409, "discovery_running", "a discovery pass is already running")
    try:
        work = asyncio.ensure_future(asyncio.to_thread(_locked_run, run))
    except BaseException:
        _discovery_lock.release()
        raise
    try:
        summary = await asyncio.wait_for(asyncio.shield(work), DISCOVERY_TIMEOUT_S)
    except TimeoutError as exc:
        work.add_done_callback(lambda done: done.cancelled() or done.exception())  # result is not needed
        raise ApiError(
            504,
            "discovery_timeout",
            f"discovery did not finish within {DISCOVERY_TIMEOUT_S:g} s; it continues in the background",
        ) from exc
    return JSONResponse(summary_to_json(summary))


@router.post("/posting/hourly", status_code=202)
def posting_hourly() -> JSONResponse:
    """Schedule the current hourly slot. Calling it again for the same slot queues nothing new."""
    result = schedule_hourly_slot()  # writes the heartbeat `trigger:hourly`
    return JSONResponse(
        {
            "status": result.status,
            "slot_at": result.slot_at.isoformat(),
            "fires_at": result.fire_at.isoformat() if result.fire_at else None,
            "job_id": result.job_id,
        },
        status_code=202,
    )


@router.post("/guardian/check")
def guardian_check() -> None:
    db.heartbeat(HEARTBEAT_GUARDIAN)
    raise not_implemented("M13", "the guardian check")


@router.post("/guardian/takedown")
def guardian_takedown() -> None:
    raise not_implemented("M13", "takedown intake")


@router.post("/stats/collect")
def stats_collect() -> None:
    db.heartbeat(HEARTBEAT_STATS)
    raise not_implemented("M13", "stats collection")


@router.get("/reports/daily")
def reports_daily() -> None:
    raise not_implemented("M13", "the daily report")

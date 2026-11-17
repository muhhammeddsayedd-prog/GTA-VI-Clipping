"""Trigger routes that n8n calls: discovery, the hourly slot, the guardian check, takedown intake, stats
and the daily report.

Every trigger writes its heartbeat (`trigger:discovery`, `trigger:hourly`, `trigger:guardian`,
`trigger:stats`) when the call arrives, before the work starts, so the heartbeat
always means "n8n is calling".
"""

import asyncio
import dataclasses
import importlib
import threading
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sixstars import db
from sixstars.api.errors import ApiError
from sixstars.guardian import service as guardian_service
from sixstars.logging import get_logger
from sixstars.posting.hourly import schedule_hourly_slot
from sixstars.stats import report as report_view

router = APIRouter(prefix="/v1")  # the trigger routes: n8n's token only (docs/26)
read_router = APIRouter(prefix="/v1")  # read routes: n8n's or the admin console's token

DISCOVERY_MODULE = "sixstars.discovery.run"
DISCOVERY_FUNCTION = "run_discovery"
DISCOVERY_TIMEOUT_S = 60.0  # docs/17: the route answers within 60 s
HEARTBEAT_DISCOVERY = "trigger:discovery"
HEARTBEAT_GUARDIAN = "trigger:guardian"
HEARTBEAT_STATS = "trigger:stats"
MAX_COERCED_CHARS = 2000

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


class TakedownBody(BaseModel):
    """The email as WF04 sends it. Every field is untrusted text from a public inbox. Unknown fields
    are ignored and nothing is rejected for being long (the request body limit of the API still
    applies): a refused request would lose the email, so oversized text is cut by the guardian."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    sender: str | None = Field(default=None, alias="from")
    subject: str | None = None
    body_text: str | None = None
    received_at: str | int | float | None = None
    message_id: str | None = None

    @field_validator("sender", "subject", "body_text", "message_id", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str | None:
        """n8n may send a number, a list or an object where text is expected: keep it as text."""
        if value is None or isinstance(value, str):
            return value
        return str(value)[:MAX_COERCED_CHARS]


@router.post("/guardian/check")
def guardian_check() -> JSONResponse:
    """One pass of the claims and removals check (WF03)."""
    db.heartbeat(HEARTBEAT_GUARDIAN)
    return JSONResponse(guardian_service.run_check())


@router.post("/guardian/takedown")
def guardian_takedown(body: TakedownBody) -> JSONResponse:
    """One email from the takedown inbox (WF04). Processed once per `message_id`."""
    received = None if body.received_at is None else str(body.received_at)
    result = guardian_service.handle_takedown(
        body.sender, body.subject, body.body_text, body.message_id, received
    )
    return JSONResponse(result)


@router.post("/stats/collect")
def stats_collect() -> JSONResponse:
    """Collect account and post numbers (WF05)."""
    db.heartbeat(HEARTBEAT_STATS)
    return JSONResponse(guardian_service.collect_stats())


@read_router.get("/reports/daily")
def reports_daily(
    format: Literal["telegram", "json"] = Query("json"),  # noqa: A002
    day: Annotated[date | None, Query()] = None,
) -> JSONResponse:
    """The daily report of the day that just ended (or of `day`, as YYYY-MM-DD).
    `format=telegram` answers `{"text": "<HTML-safe text>"}`."""
    data = guardian_service.daily_report(day)
    if format == "telegram":
        return JSONResponse({"text": report_view.render_telegram(data)})
    return JSONResponse(jsonable_encoder(data))

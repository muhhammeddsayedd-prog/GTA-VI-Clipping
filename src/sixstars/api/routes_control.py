"""Status and control routes: `/v1/status`, the emergency stop and manual setting changes.

Every change writes an `events` row (who did what, with the old and the new value) and sends a Telegram
notification. The setting routes read and write the table directly inside one transaction, then drop the
30 s settings cache of this process.
"""

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sixstars import clock, db, events, notify, settings
from sixstars.api import status as status_view
from sixstars.api.errors import ApiError
from sixstars.learning import delivery
from sixstars.logging import get_logger

router = APIRouter(prefix="/v1")

STOP_KEY = "control.emergency_stop"
REASON_KEY = "control.stop_reason"
DEFAULT_STOP_REASON = "manual stop (control API)"
MAX_REASON_CHARS = 500
MAX_VALUE_PREVIEW_CHARS = 300
SERVICE = "api"

SettingKey = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
]


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_REASON_CHARS)] = ""


class SettingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: SettingKey
    value: Any = Field(...)  # required; any JSON value, checked against the stored one


@router.get("/status")
def status(format: Literal["telegram", "json"] = Query("json")) -> JSONResponse:  # noqa: A002
    """What the system is doing now. `format=telegram` answers `{"text": "<HTML-safe text>"}`."""
    snapshot = status_view.collect()
    if format == "telegram":
        return JSONResponse({"text": status_view.render_telegram(snapshot)})
    return JSONResponse(snapshot)


def _locked_values(keys: list[str]) -> dict[str, Any]:
    rows = db.fetch_all("select key, value from settings where key = any(%s) for update", (keys,))
    return {row["key"]: row["value"] for row in rows}


def _set_stop(stopped: bool, reason: str | None) -> tuple[bool, str | None]:
    """Write the stop flag and its reason. Returns whether the flag changed and the previous reason."""
    with db.transaction():
        before = _locked_values([STOP_KEY, REASON_KEY])
        was_stopped = before.get(STOP_KEY) is True
        settings.set(STOP_KEY, stopped)
        settings.set(REASON_KEY, reason)
        events.emit(
            "control.stop" if stopped else "control.start",
            level="critical" if stopped else "warn",
            entity_type="setting",
            entity_id=STOP_KEY,
            message=(f"emergency stop set: {reason}" if stopped else "emergency stop cleared"),
            data={
                "old": {STOP_KEY: before.get(STOP_KEY), REASON_KEY: before.get(REASON_KEY)},
                "new": {STOP_KEY: stopped, REASON_KEY: reason},
                "changed": was_stopped != stopped,
            },
            service=SERVICE,
        )
    settings.invalidate()
    previous = before.get(REASON_KEY)
    return was_stopped != stopped, previous if isinstance(previous, str) else None


@router.post("/control/stop")
def control_stop(body: Annotated[StopBody | None, Body()] = None) -> JSONResponse:
    """Set the emergency stop: all posting stops until `/v1/control/start`."""
    reason = (body.reason if body else "") or DEFAULT_STOP_REASON
    changed, _ = _set_stop(True, reason)
    get_logger("api").warning("emergency_stop_set", changed=changed)
    if changed:
        notify.send("critical", "Emergency stop ON", f"All posting is stopped. Reason: {reason}")
    return JSONResponse({"stopped": True, "reason": reason, "changed": changed})


class LaneResumeBody(BaseModel):
    """`account_id`, or the Telegram message text `/resume <account_id>` (WF08 sends the whole text)."""

    model_config = ConfigDict(extra="ignore")

    account_id: str | None = None
    text: str | None = None

    def account(self) -> str | None:
        if self.account_id:
            return self.account_id.strip()
        words = (self.text or "").split()
        return words[1].strip() if len(words) > 1 else None


@router.post("/control/lane/resume")
def control_lane_resume(body: LaneResumeBody) -> JSONResponse:
    """Clear the hourly-lane pause the delivery guard set on one account (only the owner does this)."""
    account_id = body.account()
    if not account_id:
        return JSONResponse({"text": "Usage: /resume <account_id>, e.g. /resume tiktok_main"})
    result = delivery.resume_lane(account_id)
    if not result["found"]:
        text = f"No account {account_id}."
    elif result["changed"]:
        text = f"▶️ Hourly lane of {account_id} resumed."
    else:
        text = f"The hourly lane of {account_id} was not paused."
    return JSONResponse(result | {"text": text})


@router.post("/control/start")
def control_start() -> JSONResponse:
    """Clear the emergency stop."""
    changed, previous = _set_stop(False, None)
    get_logger("api").warning("emergency_stop_cleared", changed=changed)
    if changed:
        notify.send(
            "critical",
            "Emergency stop OFF",
            f"Posting may run again. The stop reason was: {previous or 'none given'}",
        )
    return JSONResponse({"stopped": False, "changed": changed})


def json_kind(value: Any) -> str:
    """The JSON type of a decoded value: null, boolean, number, string, array or object."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def kind_allowed(old: Any, new: Any) -> bool:
    """A setting keeps its JSON type. Two exceptions for the nullable settings (a stop reason, a
    `..._until` time): a stored null accepts any value, and a stored string may become null again."""
    old_kind, new_kind = json_kind(old), json_kind(new)
    if old_kind == new_kind or old_kind == "null":
        return True
    return old_kind == "string" and new_kind == "null"


def _preview(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= MAX_VALUE_PREVIEW_CHARS else text[:MAX_VALUE_PREVIEW_CHARS] + "..."


@router.post("/control/settings")
def control_settings(body: SettingBody) -> JSONResponse:
    """Change one existing setting. Unknown keys are refused: this route never creates a setting."""
    try:
        encoded = json.dumps(body.value, allow_nan=False)
    except ValueError as exc:
        raise ApiError(422, "invalid_value", "value must be JSON (NaN and Infinity are not)") from exc
    if "\\u0000" in encoded:
        raise ApiError(422, "invalid_value", "value must not contain NUL characters")
    if body.key == clock.TIMEZONE_KEY and not clock.is_valid_zone(body.value):
        raise ApiError(
            422, "invalid_timezone", f"{body.value!r} is not an IANA time zone such as Africa/Cairo"
        )
    with db.transaction():
        current = db.fetch_one("select key, value from settings where key = %s for update", (body.key,))
        if current is None:
            raise ApiError(404, "unknown_setting", f"there is no setting named {body.key}")
        old = current["value"]
        if not kind_allowed(old, body.value):
            raise ApiError(
                422,
                "type_mismatch",
                f"{body.key} is a JSON {json_kind(old)}; a {json_kind(body.value)} was sent",
            )
        row = db.fetch_one(
            "update settings set value = %s where key = %s returning key, value, description, updated_at",
            (Jsonb(body.value), body.key),
        )
        assert row is not None
        changed = old != body.value
        events.emit(
            "setting.changed",
            level="warn",
            entity_type="setting",
            entity_id=body.key,
            message=f"{body.key} changed through the control API",
            data={"key": body.key, "old": old, "new": body.value, "changed": changed},
            service=SERVICE,
        )
    settings.invalidate()
    get_logger("api").warning("setting_changed", key=body.key, changed=changed)
    if changed:
        notify.send("info", f"Setting changed: {body.key}", f"{_preview(old)} → {_preview(body.value)}")
    return JSONResponse(
        {
            "key": row["key"],
            "value": row["value"],
            "previous": old,
            "description": row["description"],
            "updated_at": row["updated_at"].isoformat(),
            "changed": changed,
        }
    )

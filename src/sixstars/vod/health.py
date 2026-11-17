"""Which platforms refuse video downloads from this machine right now, in the style of `live/health.py`.

The lane's own back-off reads the `vod.download_blocked` events (`store.block_retry_at`). Discovery needs
the same fact as a state it can read in one look, so that it stops queueing videos of a platform that
cannot be downloaded: the settings key `vod.source_health` (written by code), one entry per blocked
platform:

    {"youtube": {"since": "...", "last_blocked_at": "...", "blocks": 7, "error": "...", "access": "..."}}

`record_blocked` is called when a download is classified as blocked, `record_ok` when a window of that
platform was downloaded (the entry is removed). An entry that is not refreshed is treated as lifted by
its reader after `discovery.blocked_platform_stale_s`, so a record nobody clears cannot hold a platform
back for ever.

`note_blocked`, `note_ok` and `blocked` are pure; `load`, `record_blocked` and `record_ok` touch the
database. The two writers never raise: this bookkeeping must not change what happens to a video.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from sixstars import db, settings
from sixstars.logging import get_logger

STATE_KEY: Final = "vod.source_health"
ERROR_CHARS: Final = 300

State = dict[str, dict[str, Any]]
_log = get_logger("vod")


def _state(raw: Any) -> State:
    if not isinstance(raw, dict):
        return {}
    return {str(name): dict(entry) for name, entry in raw.items() if isinstance(entry, dict)}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def load() -> State:
    """The stored state (through the settings cache)."""
    return _state(settings.get(STATE_KEY, None))


def note_blocked(
    state: Mapping[str, Mapping[str, Any]], platform: str, now: datetime, detail: str, access: str | None
) -> State:
    """Pure: record that `platform` refused a download."""
    new: State = {name: dict(entry) for name, entry in state.items()}
    entry = new.get(platform) or {}
    new[platform] = {
        "since": entry.get("since") or now.isoformat(),
        "last_blocked_at": now.isoformat(),
        "blocks": int(entry.get("blocks") or 0) + 1,
        "error": detail[:ERROR_CHARS],
        "access": access,
    }
    return new


def note_ok(state: Mapping[str, Mapping[str, Any]], platform: str) -> State:
    """Pure: record that a download of `platform` worked (its entry goes)."""
    return {name: dict(entry) for name, entry in state.items() if name != platform}


def blocked(state: Mapping[str, Mapping[str, Any]], now: datetime, stale_s: float) -> dict[str, datetime]:
    """Pure: the platforms whose downloads count as blocked at `now`, with the start of each block. An
    entry whose last refusal is more than `stale_s` old no longer counts."""
    result: dict[str, datetime] = {}
    for platform, entry in state.items():
        last = _time(entry.get("last_blocked_at"))
        if last is None or (now - last).total_seconds() > stale_s:
            continue
        result[platform] = _time(entry.get("since")) or last
    return result


def _locked_state() -> State:
    """The state read under a row lock (inside a transaction), past the settings cache: two workers that
    record at the same moment must not overwrite each other."""
    row = db.fetch_one("select value from settings where key = %s for update", (STATE_KEY,))
    return _state(row["value"]) if row is not None else {}


def record_blocked(platform: str, now: datetime, detail: str, access: str | None) -> None:
    """Store one refused download of `platform`. Never raises."""
    try:
        with db.transaction():
            settings.set(STATE_KEY, note_blocked(_locked_state(), platform, now, detail, access))
    except Exception as exc:
        _log.warning("vod_source_health_failed", error=type(exc).__name__)


def record_ok(platform: str) -> None:
    """Clear the block of `platform`, if one is recorded. Never raises."""
    try:
        if platform not in load():  # the usual case, answered from the cache without a write
            return
        with db.transaction():
            state = _locked_state()
            if platform in state:
                settings.set(STATE_KEY, note_ok(state, platform))
        _log.info("vod_source_recovered", platform=platform)
    except Exception as exc:
        _log.warning("vod_source_health_failed", error=type(exc).__name__)

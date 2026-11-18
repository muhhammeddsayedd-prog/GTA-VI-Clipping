"""The `scoring_config` flags, cached for 30 seconds like `settings`, and the scoring config version.

A flag is a JSON object with an `enabled` boolean and its numbers. `flag(key)` returns the object when it is
enabled and None otherwise; a missing table (a database before migration 0026), a missing key or a
malformed value all read as "off", so the overhaul can never break the old behaviour by being absent.

`version()` is the newest `scoring_config_history.version` (0 before the first change): every spike, clip
and `scoring_log` row records it, so each decision can be traced to the flags that made it.
"""

import threading
import time
from collections.abc import Mapping
from typing import Any, Final

from sixstars import db
from sixstars.logging import get_logger

CACHE_TTL_S: Final = 30.0

_lock = threading.Lock()
_values: dict[str, Any] | None = None
_version = 0
_loaded_at = 0.0
_log = get_logger("scoring")


def _load() -> tuple[dict[str, Any], int]:
    global _values, _version, _loaded_at  # noqa: PLW0603
    with _lock:
        now = time.monotonic()
        if _values is None or now - _loaded_at >= CACHE_TTL_S:
            try:
                rows = db.fetch_all("select key, value from scoring_config")
                row = db.fetch_one("select coalesce(max(version), 0) as v from scoring_config_history")
                _values = {str(item["key"]): item["value"] for item in rows}
                _version = int(row["v"]) if row is not None else 0
            except Exception as exc:  # noqa: BLE001 - no table or no database: every flag is off
                _log.warning("scoring_config_unreadable", error=f"{type(exc).__name__}: {exc}"[:200])
                _values, _version = {}, 0
            _loaded_at = now
        return _values, _version


def invalidate() -> None:
    """Drop the cache (tests, and the CLI after a change)."""
    global _values  # noqa: PLW0603
    with _lock:
        _values = None


def set_cache(values: Mapping[str, Any], version_no: int = 0) -> None:
    """Replace the cache by hand (tests). Lasts `CACHE_TTL_S`."""
    global _values, _version, _loaded_at  # noqa: PLW0603
    with _lock:
        _values, _version, _loaded_at = dict(values), version_no, time.monotonic()


def raw(key: str) -> Any:
    """The JSON value of `key`, or None."""
    values, _ = _load()
    return values.get(key)


def flag(key: str) -> Mapping[str, Any] | None:
    """The flag's object when it is enabled, else None."""
    value = raw(key)
    if isinstance(value, Mapping) and value.get("enabled") is True:
        return value
    return None


def section(key: str) -> Mapping[str, Any]:
    """A config object whatever its `enabled` (for shared parameters such as `percentile.history`)."""
    value = raw(key)
    return value if isinstance(value, Mapping) else {}


def version() -> int:
    """The scoring config version (newest history row)."""
    _, number = _load()
    return number


def number(values: Mapping[str, Any], key: str, default: float) -> float:
    """`values[key]` as a float, or `default` when it is missing or not a number. Pure."""
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def strings(values: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """`values[key]` as a tuple of strings (empty when it is not a list). Pure."""
    value = values.get(key)
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def pair(values: Mapping[str, Any], key: str, default: tuple[float, float]) -> tuple[float, float]:
    """`values[key]` as a `[low, high]` pair of numbers. Pure."""
    value = values.get(key)
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, int | float) and not isinstance(item, bool) for item in value)
    ):
        low, high = float(value[0]), float(value[1])
        return (low, high) if low <= high else (high, low)
    return default

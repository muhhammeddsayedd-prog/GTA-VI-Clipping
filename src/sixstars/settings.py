"""Runtime settings read from the Postgres `settings` table and cached for 30 seconds. Every threshold,
feature flag and limit that the docs mark as a setting is read through this module, never hard-coded.
Implemented in M01.

The whole table is loaded in one query and kept in process memory for `CACHE_TTL_S` seconds, so a change
made anywhere (CLI, control API, SQL) reaches every process within that time. `set()` also refreshes the
cache of the process that made the change. Reading a key that does not exist raises `KeyError` unless a
default is passed, which catches typos in key names.
"""

import threading
import time
from datetime import UTC, datetime
from typing import Any, Final

from psycopg.types.json import Jsonb

from sixstars import db

CACHE_TTL_S: Final = 30.0


class _Missing:
    """Sentinel type meaning that the caller passed no default."""


_MISSING: Final = _Missing()

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_loaded_at = 0.0


def _all() -> dict[str, Any]:
    """Return the cached settings table, reloading it when the cache is older than `CACHE_TTL_S`."""
    global _cache, _loaded_at
    with _lock:
        now = time.monotonic()
        if _cache is None or now - _loaded_at >= CACHE_TTL_S:
            rows = db.fetch_all("select key, value from settings")
            _cache = {row["key"]: row["value"] for row in rows}
            _loaded_at = now
        return _cache


def invalidate() -> None:
    """Drop the in-process cache so the next read reloads the table."""
    global _cache
    with _lock:
        _cache = None


def get(key: str, default: Any = _MISSING) -> Any:
    """Return the JSON value of setting `key`. Unknown key: `default` if one was passed, else `KeyError`."""
    values = _all()
    if key in values:
        return values[key]
    if isinstance(default, _Missing):
        raise KeyError(f"unknown setting: {key}")
    return default


def set(key: str, value: Any, description: str | None = None) -> None:  # noqa: A001
    """Create or replace setting `key` with a JSON-serialisable `value`."""
    db.execute(
        """
        insert into settings (key, value, description) values (%s, %s, %s)
        on conflict (key) do update
          set value = excluded.value,
              description = coalesce(excluded.description, settings.description)
        """,
        (key, Jsonb(value), description),
    )
    invalidate()


def _wrong_type(key: str, expected: str, value: Any) -> TypeError:
    return TypeError(f"setting {key} must be {expected}, got {type(value).__name__}")


def get_bool(key: str, default: bool | _Missing = _MISSING) -> bool:
    """Return setting `key` as a bool."""
    value = get(key, default)
    if not isinstance(value, bool):
        raise _wrong_type(key, "a boolean", value)
    return value


def get_int(key: str, default: int | _Missing = _MISSING) -> int:
    """Return setting `key` as an int (a float with no fractional part is accepted)."""
    value = get(key, default)
    if isinstance(value, bool):
        raise _wrong_type(key, "an integer", value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise _wrong_type(key, "an integer", value)


def get_float(key: str, default: float | _Missing = _MISSING) -> float:
    """Return setting `key` as a float (integers are accepted)."""
    value = get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _wrong_type(key, "a number", value)
    return float(value)


def get_str(key: str, default: str | _Missing = _MISSING) -> str:
    """Return setting `key` as a string."""
    value = get(key, default)
    if not isinstance(value, str):
        raise _wrong_type(key, "a string", value)
    return value


def get_dict(key: str, default: dict[str, Any] | _Missing = _MISSING) -> dict[str, Any]:
    """Return setting `key` as a dict (a copy, safe to modify)."""
    value = get(key, default)
    if not isinstance(value, dict):
        raise _wrong_type(key, "an object", value)
    return dict(value)


def get_list(key: str, default: list[Any] | _Missing = _MISSING) -> list[Any]:
    """Return setting `key` as a list (a copy, safe to modify)."""
    value = get(key, default)
    if not isinstance(value, list):
        raise _wrong_type(key, "an array", value)
    return list(value)


def get_dt(key: str, default: datetime | None | _Missing = _MISSING) -> datetime | None:
    """Return setting `key` as a timezone-aware UTC datetime, or None when the stored value is null.

    The stored value must be an ISO 8601 string; a value without an offset is read as UTC.
    """
    value = get(key, default)
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise _wrong_type(key, "an ISO 8601 time string or null", value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

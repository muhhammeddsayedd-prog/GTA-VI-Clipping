"""The app's display clock.

Everything is stored, compared and limited in UTC. `app.timezone` (default Africa/Cairo) only
decides how times are *shown* to people (Telegram, the daily report, the console) and which local
day the daily report covers. Use `fmt_local` for any time a human reads.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from sixstars import settings

DEFAULT_TIMEZONE = "Africa/Cairo"
TIMEZONE_KEY = "app.timezone"
DATE_TIME = "%Y-%m-%d %H:%M"
CACHE_TTL_S = 30.0  # same as the settings cache: a change shows within 30 s


def is_valid_zone(name: object) -> bool:
    """True for "UTC" or any IANA zone name this machine knows."""
    if not isinstance(name, str) or not name.strip():
        return False
    if name.upper() == "UTC":
        return True
    try:
        ZoneInfo(name)
    except Exception:  # unknown name, bad path, or no tz data
        return False
    return True


def zone_for(name: str) -> tzinfo:
    """The tzinfo for `name`; an invalid name falls back to the default zone."""
    if name.upper() == "UTC":
        return UTC
    if is_valid_zone(name):
        return ZoneInfo(name)
    return ZoneInfo(DEFAULT_TIMEZONE)


_cached: tuple[float, tzinfo] | None = None


def app_zone() -> tzinfo:
    """The display zone from `settings.app.timezone`.

    Display only, so it never raises: a wrong type, a missing database (unit tests) or a database
    outage gives the last zone read, else the default. A result is kept for `CACHE_TTL_S` so an outage
    costs at most one failed read per interval."""
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < CACHE_TTL_S:
        return _cached[1]
    try:
        zone = zone_for(settings.get_str(TIMEZONE_KEY, DEFAULT_TIMEZONE))
    except Exception:
        zone = _cached[1] if _cached is not None else ZoneInfo(DEFAULT_TIMEZONE)
    _cached = (now, zone)
    return zone


def reset_cache() -> None:
    """Forget the cached zone (tests, and right after the setting changes)."""
    global _cached
    _cached = None


def zone_name(zone: tzinfo) -> str:
    """The IANA name ("Africa/Cairo") or "UTC"."""
    return getattr(zone, "key", None) or "UTC"


def zone_label(zone: tzinfo) -> str:
    """A short human label: "Cairo", "New York", "UTC"."""
    name = zone_name(zone)
    if name.upper() == "UTC":
        return "UTC"
    return name.rsplit("/", 1)[-1].replace("_", " ")


def fmt_local(
    dt: datetime,
    fmt: str = "%H:%M",
    *,
    with_utc: bool = True,
    zone: tzinfo | None = None,
) -> str:
    """`dt` in the app zone with its label, e.g. "14:32 Cairo (12:32 UTC)".

    `with_utc` adds the UTC time in brackets; keep it on wherever the time relates to a UTC limit.
    A naive `dt` is taken as UTC.
    """
    zone = zone or app_zone()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    label = zone_label(zone)
    local = f"{dt.astimezone(zone).strftime(fmt)} {label}"
    if not with_utc or label == "UTC":
        return local
    return f"{local} ({dt.astimezone(UTC).strftime(fmt)} UTC)"

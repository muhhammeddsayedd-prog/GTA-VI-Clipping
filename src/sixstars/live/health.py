"""Health of the live sources (the platforms streams are recorded from), in the style of
`discovery/health.py`: a platform that refuses this machine is reported once, not once per stream.

From a data-centre address YouTube answers stream requests with a bot check. When a session start is
classified `blocked`, the platform goes into a quiet degraded state, kept in the settings key
`live.source_health` (written by code):

- sessions of that platform are not started while it backs off; their slots are released;
- the first block backs off for a sixth of `live.blocked_backoff_s`, every later one for the whole of it;
  after each back-off one session start probes the platform again;
- after `live.blocked_notify_after` blocks in a row: one `warn` notification with the fix. Nothing more is
  sent while the platform stays blocked;
- when a recording of that platform produces data again: one `info` notification, and the state is cleared.

The back-off also ends at once when the way the platform is reached has changed since the block (the
owner set `PROXY_URL` or mounted the cookies file and restarted the containers).

`note_blocked`, `note_ok` and `backing_off` are pure; `load`, `save` and `announce` touch the database.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sixstars import events, notify, settings
from sixstars.logging import get_logger

STATE_KEY: Final = "live.source_health"
ERROR_CHARS: Final = 300
FIRST_BACKOFF_SHARE: Final = 6  # the first block waits a sixth of the full back-off
FIXES: Final[dict[str, str]] = {
    "youtube": (
        "YouTube refuses live stream requests from this machine (usual for a data-centre address). Set "
        "PROXY_URL (a residential proxy) or mount YOUTUBE_COOKIES_FILE in the server's .env and restart "
        "the containers. Until then the live lane releases YouTube slots and probes again by itself; the "
        "VOD lane is not affected by this message."
    ),
    "twitch": (
        "Twitch refuses stream requests from this machine. Check the server's network, or set PROXY_URL "
        "and add twitch to downloads.proxy_platforms."
    ),
}
DEFAULT_FIX: Final = "Run `sixstars live probe <stream url>` on the server to see what is refused."

State = dict[str, dict[str, Any]]
_log = get_logger("live")


@dataclass(frozen=True, slots=True)
class Notice:
    """One notification (and event) the health step asks for."""

    severity: str  # warn | info
    platform: str
    title: str
    body: str
    event: str  # live.source_blocked | live.source_recovered


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def load() -> State:
    raw = settings.get(STATE_KEY, None)
    if not isinstance(raw, dict):
        return {}
    return {str(name): dict(entry) for name, entry in raw.items() if isinstance(entry, dict)}


def save(state: Mapping[str, Mapping[str, Any]]) -> None:
    settings.set(STATE_KEY, {name: dict(entry) for name, entry in state.items()})


def backing_off(
    state: Mapping[str, Mapping[str, Any]], platform: str, now: datetime, access: str | None = None
) -> datetime | None:
    """Pure: until when `platform` must not be tried, or None when a session may start (and so probe).
    `access` says how the platform is reached now; when it differs from what was recorded with the
    block, the back-off no longer applies."""
    entry = state.get(platform)
    if not entry:
        return None
    until = _time(entry.get("skip_until"))
    changed = access is not None and entry.get("access") not in (None, access)
    if until is None or now >= until or changed:
        return None
    return until


def note_blocked(
    state: Mapping[str, Mapping[str, Any]],
    platform: str,
    now: datetime,
    detail: str,
    *,
    notify_after: int,
    backoff_s: float,
    access: str | None = None,
) -> tuple[State, list[Notice]]:
    """Pure: record that a session start of `platform` was refused."""
    new: State = {name: dict(entry) for name, entry in state.items()}
    entry = new.get(platform) or {}
    count = int(entry.get("blocks") or 0) + 1
    wait_s = backoff_s if count >= notify_after else backoff_s / FIRST_BACKOFF_SHARE
    updated: dict[str, Any] = {
        "blocks": count,
        "since": entry.get("since") or now.isoformat(),
        "error": detail[:ERROR_CHARS],
        "notified": bool(entry.get("notified")),
        "skip_until": (now + timedelta(seconds=wait_s)).isoformat(),
        "access": access,
    }
    notices: list[Notice] = []
    if count >= notify_after and not updated["notified"]:
        updated["notified"] = True
        started = _time(updated["since"]) or now
        body = (
            f"{count} refused stream starts in a row since {started:%Y-%m-%d %H:%M} UTC. "
            f"Error: {updated['error'] or 'unknown'}. The live lane released the {platform} slots and "
            f"tries one stream again every {int(backoff_s // 60)} minutes. Fix: "
            f"{FIXES.get(platform, DEFAULT_FIX)}"
        )
        notices.append(
            Notice("warn", platform, f"Live source {platform} is blocked", body, "live.source_blocked")
        )
    new[platform] = updated
    return new, notices


def note_ok(
    state: Mapping[str, Mapping[str, Any]], platform: str, now: datetime
) -> tuple[State, list[Notice]]:
    """Pure: record that a recording of `platform` produced data."""
    new: State = {name: dict(entry) for name, entry in state.items()}
    entry = new.pop(platform, None)
    notices: list[Notice] = []
    if entry and entry.get("notified"):
        started = _time(entry.get("since")) or now
        body = (
            f"Recording works again after {int(entry.get('blocks') or 0)} refused starts "
            f"(blocked since {started:%Y-%m-%d %H:%M} UTC)."
        )
        notices.append(
            Notice("info", platform, f"Live source {platform} recovered", body, "live.source_recovered")
        )
    return new, notices


def announce(notices: Sequence[Notice]) -> None:
    """Write the event and send the notification of each notice."""
    for notice in notices:
        _log.warning("live_source_health", platform=notice.platform, state=notice.event)
        events.emit(
            notice.event,
            level="warn" if notice.severity == "warn" else "info",
            message=f"{notice.title}: {notice.body}"[:500],
            data={"platform": notice.platform},
            service="live",
        )
        notify.send("warn" if notice.severity == "warn" else "info", notice.title, notice.body)


def record_blocked(
    platform: str, now: datetime, detail: str, *, notify_after: int, backoff_s: float, access: str | None
) -> datetime | None:
    """Load, fold in one refused start, store and announce. Returns the end of the back-off."""
    new, notices = note_blocked(
        load(), platform, now, detail, notify_after=notify_after, backoff_s=backoff_s, access=access
    )
    save(new)
    announce(notices)
    return _time(new[platform].get("skip_until"))


def record_ok(platform: str, now: datetime) -> None:
    """Load, clear the platform's block (if any), store and announce the recovery."""
    state = load()
    if platform not in state:
        return
    new, notices = note_ok(state, platform, now)
    save(new)
    announce(notices)

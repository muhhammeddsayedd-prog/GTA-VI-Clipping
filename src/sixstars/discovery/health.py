"""Health of the discovery sources across passes: makes a repeated failure visible once, not every pass.

Each pass reports a status per source (`ok`, `empty`, `skipped`, `not_configured`, `error`). This module
keeps, in the settings key `discovery.source_health` (written by code), how many passes in a row each
source has failed, and decides:

- after `discovery.source_failure_notify_after` failed passes in a row: one `warn` notification that names
  the source, the error and the fix. Nothing more is sent while it keeps failing.
- when a source that was reported works again: one `info` notification.
- for the yt-dlp search: after `discovery.ytdlp_backoff_after_failures` failed passes it is skipped for
  `discovery.ytdlp_backoff_s`, and while it is known to be failing a pass tries one search (a probe) before
  running the other eleven. From a data-centre address YouTube closes the connection on every search, so
  without this each pass would spend twelve doomed subprocess calls.

`skipped` and `not_configured` neither count as a failure nor as a recovery, with one exception: a pass
that a source sits out because it is backing off counts towards the warning (the source is still not
working), but not towards the next back-off. The back-off also ends at once when the way the source
reaches YouTube has changed since it failed (the owner set `PROXY_URL` or mounted the cookies file and
restarted the containers): the next pass probes instead of waiting out the hour. `step` and `gate` are
pure; `load`, `save` and `announce` touch the database.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sixstars import events, notify, settings
from sixstars.clock import DATE_TIME, fmt_local
from sixstars.logging import get_logger

STATE_KEY: Final = "discovery.source_health"
YTDLP_SOURCE: Final = "youtube_search"
ERROR_CHARS: Final = 300
FAILED: Final = "error"
WORKING: Final[frozenset[str]] = frozenset({"ok", "empty"})

# What the owner can do about a source that keeps failing (shown in the notification).
FIXES: Final[dict[str, str]] = {
    "youtube_search": (
        "YouTube refuses yt-dlp searches from this machine (usual for a data-centre address). Set "
        "PROXY_URL (a residential proxy) in the server's .env and restart the containers. Until then "
        "discovery uses the Data API search, channel feeds and Apify, and retries yt-dlp by itself."
    ),
    "youtube_search_api": (
        "Check YOUTUBE_API_KEY and the search.list quota of the project in the Google Cloud console "
        "(100 calls a day by default); lower discovery.search_api_daily_calls if the project has less."
    ),
    "youtube_api": "Check YOUTUBE_API_KEY and the YouTube Data API quota in the Google Cloud console.",
    "youtube_rss": "YouTube channel feeds cannot be reached from this machine: check the server's network.",
    "apify": "Check APIFY_TOKEN and the balance of the Apify account.",
    "twitch": "Check TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (https://dev.twitch.tv/console/apps).",
}
DEFAULT_FIX: Final = "Run `sixstars discovery doctor` on the server to see which path is refused."

_log = get_logger("discovery")


@dataclass(frozen=True)
class Notice:
    """One notification (and event) the health step asks for."""

    severity: str  # warn | info
    source: str
    title: str
    body: str
    event: str  # discovery.source_failing | discovery.source_recovered


@dataclass(frozen=True)
class Gate:
    """What the yt-dlp search may do in this pass."""

    skip: bool = False
    probe: bool = False  # try one search first; run the others only when it works
    detail: str = ""


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def load() -> dict[str, dict[str, Any]]:
    """The stored state: `{source: {failures, passes, since, error, notified, skip_until}}`. `failures`
    counts the passes in which the source ran and failed, `passes` those plus the passes it sat out while
    backing off."""
    raw = settings.get(STATE_KEY, None)
    if not isinstance(raw, dict):
        return {}
    return {str(name): dict(entry) for name, entry in raw.items() if isinstance(entry, dict)}


def save(state: Mapping[str, Mapping[str, Any]]) -> None:
    settings.set(STATE_KEY, {name: dict(entry) for name, entry in state.items()})


def gate(
    state: Mapping[str, Mapping[str, Any]], source: str, now: datetime, access: str | None = None
) -> Gate:
    """Pure: may `source` run in this pass, and should it probe first? `access` says how the source
    reaches the platform now (`direct`, `proxy`, ...): when it differs from what was recorded with the
    failure, the back-off no longer applies."""
    entry = state.get(source)
    if not entry or int(entry.get("failures") or 0) <= 0:
        return Gate()
    failures = int(entry["failures"])
    until = _time(entry.get("skip_until"))
    changed = access is not None and entry.get("access") not in (None, access)
    if until is not None and now < until and not changed:
        error = str(entry.get("error") or "")
        detail = f"backing off until {until:%H:%M} UTC after {failures} failed attempts: {error}"
        return Gate(skip=True, detail=detail[:ERROR_CHARS])
    return Gate(probe=True)


def step(
    state: Mapping[str, Mapping[str, Any]],
    reports: Sequence[tuple[str, str, str]],
    now: datetime,
    *,
    notify_after: int,
    backoff: Mapping[str, tuple[int, int]] | None = None,
    access: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[Notice]]:
    """Pure: fold this pass's `(source, status, detail)` reports into the state.

    `backoff` maps a source to `(failed passes before it backs off, seconds to back off)`; `access` maps
    a source to how it reached the platform in this pass (stored with a failure, see `gate`). Returns the
    new state and the notices to send.
    """
    new: dict[str, dict[str, Any]] = {name: dict(entry) for name, entry in state.items()}
    notices: list[Notice] = []

    def warn_if_due(source: str, entry: dict[str, Any]) -> None:
        if int(entry["passes"]) < notify_after or entry.get("notified"):
            return
        entry["notified"] = True
        started = _time(entry.get("since")) or now
        body = (
            f"{entry['passes']} passes in a row since {fmt_local(started, DATE_TIME)}. "
            f"Error: {entry.get('error') or 'unknown'}. Fix: {FIXES.get(source, DEFAULT_FIX)}"
        )
        title = f"Discovery source {source} keeps failing"
        notices.append(Notice("warn", source, title, body, "discovery.source_failing"))

    for source, status, detail in reports:
        entry = new.get(source)
        if status == FAILED:
            failures = int((entry or {}).get("failures") or 0) + 1
            updated: dict[str, Any] = {
                "failures": failures,
                "passes": int((entry or {}).get("passes") or 0) + 1,
                "since": (entry or {}).get("since") or now.isoformat(),
                "error": detail[:ERROR_CHARS],
                "notified": bool((entry or {}).get("notified")),
                "skip_until": None,
                "access": (access or {}).get(source),
            }
            after, seconds = (backoff or {}).get(source, (0, 0))
            if after > 0 and seconds > 0 and failures >= after:
                updated["skip_until"] = (now + timedelta(seconds=seconds)).isoformat()
            warn_if_due(source, updated)
            new[source] = updated
        elif status in WORKING and entry is not None:
            if entry.get("notified"):
                started = _time(entry.get("since")) or now
                passes = int(entry.get("passes") or entry.get("failures") or 0)
                since = fmt_local(started, DATE_TIME)
                body = f"Working again after {passes} passes (failing since {since})."
                title = f"Discovery source {source} recovered"
                notices.append(Notice("info", source, title, body, "discovery.source_recovered"))
            del new[source]
        elif status == "skipped" and entry is not None:
            until = _time(entry.get("skip_until"))
            if until is not None and now < until:  # sat out because it backs off: still not working
                entry["passes"] = int(entry.get("passes") or entry.get("failures") or 0) + 1
                warn_if_due(source, entry)
    return new, notices


def announce(notices: Sequence[Notice]) -> None:
    """Write the event and send the notification of each notice."""
    for notice in notices:
        _log.warning(
            "discovery_source_health", source=notice.source, state=notice.event, detail=notice.body[:300]
        )
        events.emit(
            notice.event,
            level="warn" if notice.severity == "warn" else "info",
            message=f"{notice.title}: {notice.body}"[:500],
            data={"source": notice.source},
        )
        notify.send("warn" if notice.severity == "warn" else "info", notice.title, notice.body)


def update(
    state: Mapping[str, Mapping[str, Any]],
    reports: Sequence[tuple[str, str, str]],
    now: datetime,
    *,
    notify_after: int,
    backoff: Mapping[str, tuple[int, int]] | None = None,
    access: Mapping[str, str] | None = None,
) -> list[Notice]:
    """Run `step`, store the state when it changed and announce the notices. Returns the notices."""
    new, notices = step(state, reports, now, notify_after=notify_after, backoff=backoff, access=access)
    if new != {name: dict(entry) for name, entry in state.items()}:
        save(new)
    announce(notices)
    return notices

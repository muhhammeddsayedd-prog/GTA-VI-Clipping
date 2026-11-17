"""Whether the YouTube claim pre-check can run right now.

"Cannot run" is a normal, defined state (decision D103: there is no OAuth refresh token yet), not an
error. The gate then applies the stricter music rule. As soon as `YouTubeClient.has_oauth()` is true the
state becomes `available` and risky clips are pre-checked: no code or setting has to change.
"""

from datetime import UTC, datetime

from sixstars import db
from sixstars.clients.youtube import YouTubeClient
from sixstars.gate.models import PrecheckState

EVENT_UPLOADED = "clip.precheck_uploaded"


def state_for(*, enabled: bool, has_oauth: bool, used_today: int, daily_max: int) -> PrecheckState:
    """Pure: the pre-check state before a clip has been pre-checked."""
    if not enabled:
        return PrecheckState.DISABLED
    if not has_oauth:
        return PrecheckState.UNAVAILABLE
    if used_today >= daily_max:
        return PrecheckState.QUOTA_USED
    return PrecheckState.AVAILABLE


def uploads_today(now: datetime) -> int:
    """Pre-check uploads made since 00:00 UTC."""
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    row = db.fetch_one(
        "select count(*) as n from events where type = %s and created_at >= %s", (EVENT_UPLOADED, day_start)
    )
    return int(row["n"]) if row else 0


def setting(key: str) -> object:
    """One setting value, read from the table without the cache."""
    row = db.fetch_one("select value from settings where key = %s", (key,))
    if row is None:
        raise KeyError(f"setting {key} is missing")
    return row["value"]


def current_state(client: YouTubeClient, now: datetime) -> PrecheckState:
    """The state from `precheck.enabled`, the client's OAuth token and today's uploads (settings are read
    uncached). A client that cannot even be asked counts as no OAuth."""
    enabled = setting("precheck.enabled")
    daily_max = setting("precheck.daily_max")
    if not isinstance(enabled, bool) or isinstance(daily_max, bool) or not isinstance(daily_max, int):
        raise TypeError("precheck.enabled must be a boolean and precheck.daily_max an integer")
    try:
        has_oauth = bool(client.has_oauth())
    except Exception:  # noqa: BLE001 - an unusable client is "unavailable", never a reason to skip the rule
        has_oauth = False
    return state_for(enabled=enabled, has_oauth=has_oauth, used_today=uploads_today(now), daily_max=daily_max)

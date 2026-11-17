"""Network helpers shared by the chat adapters: the reconnect backoff schedule and the proxy decision.

No proxy is needed for chat. When `PROXY_URL` is set, an adapter uses it only for the platforms listed in
the `downloads.proxy_platforms` setting; adapters take the proxy as an argument so they stay usable
without a database.
"""

from typing import Final

from sixstars import settings
from sixstars.chat.models import Platform
from sixstars.config import env

# Seconds to wait before reconnect attempt 1, 2, 3, ...; the last value repeats (M03 milestone file).
RECONNECT_BACKOFF_S: Final[tuple[float, ...]] = (1.0, 2.0, 5.0, 10.0, 30.0)


def backoff_delay(failures: int) -> float:
    """Seconds to wait after `failures` consecutive failed attempts (0 = the first reconnect)."""
    index = min(max(failures, 0), len(RECONNECT_BACKOFF_S) - 1)
    return RECONNECT_BACKOFF_S[index]


def proxy_for(platform: Platform) -> str | None:
    """Return the proxy URL to use for `platform`, or None for a direct connection.

    Without `PROXY_URL` the answer is None and the database is not read. With it, the proxy applies only
    to the platforms in the `downloads.proxy_platforms` setting. The returned URL embeds credentials: never
    log it.
    """
    secret = env().PROXY_URL
    if secret is None:
        return None
    platforms = settings.get_list("downloads.proxy_platforms")
    if platform not in platforms:
        return None
    return secret.get_secret_value()

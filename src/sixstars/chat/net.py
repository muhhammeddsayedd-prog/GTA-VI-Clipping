"""Network helpers shared by the chat adapters: the reconnect backoff schedule and the proxy decision.

`cookies_file()` is the one place that decides whether YouTube cookies are used: `YOUTUBE_COOKIES_FILE`
names a file the owner may or may not have mounted, and a path without a file means "no cookies".

No proxy is needed for chat. When `PROXY_URL` is set, an adapter uses it only for the platforms listed in
the `downloads.proxy_platforms` setting; adapters take the proxy as an argument so they stay usable
without a database.
"""

from pathlib import Path
from typing import Final

from sixstars import settings
from sixstars.chat.models import Platform
from sixstars.config import env
from sixstars.logging import get_logger

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


_missing_cookies_logged: set[str] = set()


def usable_cookies(path: Path | None) -> Path | None:
    """`path` when it names an existing file, else None. A missing file is logged once per process (the
    default `.env` names `/secrets/youtube_cookies.txt`, which is absent until the owner mounts it)."""
    if path is None:
        return None
    if path.is_file():
        return path
    if str(path) not in _missing_cookies_logged:
        _missing_cookies_logged.add(str(path))
        get_logger("chat").warning(
            "youtube_cookies_file_missing",
            path=str(path),
            effect="YOUTUBE_COOKIES_FILE names no file: YouTube is used without cookies",
        )
    return None


def cookies_file() -> Path | None:
    """The YouTube cookies file to use: `YOUTUBE_COOKIES_FILE` when that file exists, else None. Every
    path in `chat/`, `vod/` and `discovery/` that can send cookies asks here."""
    return usable_cookies(env().YOUTUBE_COOKIES_FILE)

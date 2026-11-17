"""Network helpers shared by the chat adapters: the reconnect backoff schedule and the proxy decision.

`cookies_file()` is the one place that decides whether YouTube cookies are used: `YOUTUBE_COOKIES_FILE`
names a file the owner may or may not have mounted, and a path without a file means "no cookies".

No proxy is needed for chat. When `PROXY_URL` is set, an adapter uses it only for the platforms listed in
the `downloads.proxy_platforms` setting; adapters take the proxy as an argument so they stay usable
without a database.

The proxy can fail by itself (no balance, suspended account, wrong credentials, a provider policy that
forbids the target, or it cannot be reached). `proxy_refusal` is the one place that recognises this in the
error text of yt-dlp, streamlink and httpx, so every lane treats it as "blocked for now", never as a fault
of the video; `redact_proxy` removes the proxy URL and any credentials inside a URL from text that is
logged, stored or sent.
"""

import re
from dataclasses import dataclass
from http import HTTPStatus
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


# ── a proxy that refuses ─────────────────────────────────────

# Matched against the lower-cased error text: yt-dlp / urllib3 / requests ("Unable to connect to proxy",
# "Tunnel connection failed: 407 ..."), curl ("CONNECT tunnel failed, response 407", "Received HTTP code
# 407 from proxy after CONNECT"), httpx and streamlink (`ProxyError`), ffmpeg ("407 Proxy Authentication
# Required"), and our own summary ("proxy refused: ...") so a stored reason is recognised again.
PROXY_MARKERS: Final[tuple[str, ...]] = (
    "unable to connect to proxy",
    "cannot connect to proxy",
    "tunnel connection failed",
    "connect tunnel failed",
    "from proxy after connect",
    "proxy authentication required",
    "proxyerror",
    "proxy refused",
)
PROXY_FIX: Final = (
    "The fix is on the proxy account (balance, credentials, the provider's policy for this target), "
    "not cookies."
)
REFUSAL_MESSAGE_CHARS: Final = 80
_STATUS_AND_TEXT: Final = re.compile(
    r"(?:tunnel connection failed|proxy refused|proxyerror)[\s:('\"]*(\d{3})\b[ \t]*([^'\"()\[\]\r\n]*)",
    re.IGNORECASE,
)
_STATUS_ONLY: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"connect tunnel failed, response (\d{3})", re.IGNORECASE),
    re.compile(r"http code (\d{3}) from proxy", re.IGNORECASE),
    re.compile(r"\b(407)\s+proxy authentication required", re.IGNORECASE),
)
# Why the proxy gave no answer at all (first match wins).
_UNREACHABLE: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("connection refused", ("connection refused", "actively refused")),
    (
        "name resolution failed",
        (
            "name or service not known",
            "getaddrinfo failed",
            "name resolution",
            "failed to resolve",
            "nodename nor servname",
        ),
    ),
    ("timed out", ("timed out", "timeout")),
    ("connection closed", ("connection reset", "connection aborted", "remote end closed")),
)
_URL_CREDENTIALS: Final = re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^/\s@'\"]+@", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ProxyRefusal:
    """The proxy itself failed. `status` is the HTTP status it answered with (None when it could not be
    reached at all); `message` is its status text, or why it could not be reached."""

    status: int | None
    message: str

    @property
    def summary(self) -> str:
        """`407 Proxy Authentication Required`, or `no answer (connection refused)`."""
        if self.status is None:
            return f"no answer ({self.message})"
        return f"{self.status} {self.message}".strip()

    @property
    def described(self) -> str:
        """The line used in reasons, events and notifications: `proxy refused: <code> <text>`."""
        return f"proxy refused: {self.summary}"


def _status_text(status: int, text: str) -> str:
    cleaned = " ".join(text.split()).strip(" .,;:-")[:REFUSAL_MESSAGE_CHARS]
    if cleaned:
        return cleaned
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def proxy_refusal(text: str) -> ProxyRefusal | None:
    """Pure: the proxy failure this error text reports, or None when it reports something else.

    Case-insensitive. Recognises a proxy that answered with a refusal (407 wrong credentials or suspended
    account, 402 no balance, 403 provider policy, ...) and one that could not be reached.
    """
    lowered = text.lower()
    if not any(marker in lowered for marker in PROXY_MARKERS):
        return None
    found = _STATUS_AND_TEXT.findall(text)
    if found:
        status = int(found[-1][0])
        return ProxyRefusal(status, _status_text(status, found[-1][1]))
    for pattern in _STATUS_ONLY:
        match = pattern.search(text)
        if match is not None:
            status = int(match.group(1))
            return ProxyRefusal(status, _status_text(status, ""))
    for why, phrases in _UNREACHABLE:
        if any(phrase in lowered for phrase in phrases):
            return ProxyRefusal(None, why)
    return ProxyRefusal(None, "no status code")


def redact_proxy(text: str, proxy: str | None = None) -> str:
    """Remove `proxy` (the configured URL) and the `user:password@` part of any URL from text that will
    be logged, stored or sent."""
    if proxy:
        text = text.replace(proxy, "<proxy>")
    return _URL_CREDENTIALS.sub(r"\1<redacted>@", text)


def request_error(exc: BaseException) -> str:
    """How a failed HTTP request is named in a `ChatSourceError`: the class of the error, and for a proxy
    error also what the proxy answered (credentials removed), so the refusal can be recognised."""
    name = type(exc).__name__
    if "proxy" not in name.lower():
        return name
    return f"{name}: {redact_proxy(' '.join(str(exc).split()))}"[:300]


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

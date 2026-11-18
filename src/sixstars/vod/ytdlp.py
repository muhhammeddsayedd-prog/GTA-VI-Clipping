"""Runs yt-dlp for the VOD lane and says precisely why a run failed. Implemented in M08.

Every yt-dlp call of the lane (window downloads, caption tracks, audio for the transcript fallback, video
metadata for the CLI) goes through `run`: one subprocess with a timeout, the proxy and cookies added when
they are configured, and a failure turned into one of five kinds:

- `blocked`: the platform refuses this machine for now (YouTube's "Sign in to confirm you're not a bot",
  a rate limit). Nothing is wrong with the video; a proxy or cookies fix it. Never a permanent failure.
- `proxy`: the proxy itself refused or could not be reached (no balance, suspended account, wrong
  credentials, a provider policy: 407 / 402 / 403 on the tunnel). Handled like `blocked` (the exception
  is a `DownloadBlockedError`), but the fix is on the proxy account, not cookies.
- `forbidden`: the media server answered 403. Often a passing fault, but from a data-centre address it
  can be the same refusal as `blocked`; the caller decides from what else happened.
- `unavailable`: the video itself cannot be had (private, removed, members only, age gate, region).
- `timeout` / `error`: anything else; worth a retry.

The proxy URL embeds credentials, so commands are never logged and the proxy is scrubbed from error text.

A video job uses one sticky proxy session for all its calls (`vod/proxy.py`): the `Access` it passes down
carries that session's URL, the PO-token provider (which mints from the same address, the plugin forwards
`--proxy`) and a `TrafficMeter` that counts the calls made through the proxy.
"""

import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from sixstars.chat.net import cookies_file, proxy_for, proxy_refusal, redact_proxy
from sixstars.logging import get_logger

FailureKind = Literal["blocked", "proxy", "forbidden", "unavailable", "timeout", "error"]
# (exit code, stdout, stderr); exit code -1 means the timeout killed the process, -2 that it did not start.
Runner = Callable[[Sequence[str], float], tuple[int, str, str]]

SOCKET_TIMEOUT_S: Final = 30
NETWORK_RETRIES: Final = 3
STDERR_TAIL_CHARS: Final = 600
EXIT_TIMEOUT: Final = -1
EXIT_NOT_STARTED: Final = -2

# Matched against the lower-cased output with typographic apostrophes made plain.
BLOCKED_MARKERS: Final[tuple[str, ...]] = (
    "sign in to confirm you're not a bot",
    "confirm you're not a bot",
    "http error 429",
    "too many requests",
    "this content isn't available, try again later",
    "the page needs to be reloaded",
    "your ip is likely being blocked",
    "rate-limited by youtube",
)
FORBIDDEN_MARKERS: Final[tuple[str, ...]] = ("http error 403", "403 forbidden")
UNAVAILABLE_MARKERS: Final[tuple[str, ...]] = (
    "private video",
    "video unavailable",
    "this video is unavailable",
    "this video is not available",
    "this video has been removed",
    "has been terminated",
    "members-only",
    "available to this channel's members",
    "join this channel to get access",
    "sign in to confirm your age",
    "not available in your country",
    "not made this video available in your country",
    "this live event will begin",
    "premieres in",
    "does not exist",
    "http error 404",
    "unsupported url",
    "is not a valid url",
)
REASONS: Final[dict[str, str]] = {
    "blocked": "the platform refused this machine (bot check or rate limit)",
    "proxy": "the proxy refused or could not be reached",
    "forbidden": "the media server answered 403 Forbidden",
    "unavailable": "the video is not available",
    "timeout": "yt-dlp timed out",
    "error": "yt-dlp failed",
}


@dataclass(frozen=True, slots=True)
class Failure:
    """Why a yt-dlp run failed. `detail` is the last error line of its output (proxy scrubbed)."""

    kind: FailureKind
    detail: str


class YtDlpError(RuntimeError):
    """A yt-dlp run failed. `kind` says how to react."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(f"{REASONS[failure.kind]}: {failure.detail}")
        self.kind: FailureKind = failure.kind
        self.detail = failure.detail


class DownloadBlockedError(YtDlpError):
    """The platform refuses this machine for now; a proxy or cookies are needed."""


class ProxyRefusedError(DownloadBlockedError):
    """The proxy itself refused or could not be reached; the fix is on the proxy account."""


class VideoUnavailableError(YtDlpError):
    """The video cannot be downloaded by anyone without an account that may see it."""


class TrafficMeter:
    """Counts what one video job sent through the proxy: yt-dlp calls and bytes. Thread-safe (the windows
    of a video download in parallel)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.bytes = 0

    def count_call(self) -> None:
        with self._lock:
            self.calls += 1

    def add_bytes(self, size: int) -> None:
        with self._lock:
            self.bytes += max(0, size)


@dataclass(frozen=True, slots=True)
class Access:
    """How yt-dlp reaches a platform: an optional proxy and an optional cookies file.

    `cookies_held` means a cookies file is configured but this attempt goes without it (cookies are a
    fallback after the proxy alone, `proxy.cookies_mode`); `described` still says "set", because it names
    the configuration and the blocked-downloads back-off compares it. `sticky_port` is the proxy session
    of the job (None: `PROXY_URL` as it is). `pot_url` is the PO-token provider, used with the proxy only.
    """

    proxy: str | None = None
    cookies_file: Path | None = None
    cookies_held: bool = False
    sticky_port: int | None = None
    pot_url: str | None = None
    meter: TrafficMeter | None = field(default=None, compare=False)

    @property
    def described(self) -> str:
        """Which aids are configured, without their values (for logs and notifications). Never the port:
        the back-off of blocked downloads compares this text."""
        proxy = "set" if self.proxy else "not set"
        cookies = "set" if self.cookies_file is not None or self.cookies_held else "not set"
        return f"PROXY_URL {proxy}, YOUTUBE_COOKIES_FILE {cookies}"


def access_for(platform: str) -> Access:
    """The proxy (`PROXY_URL`, only for platforms in `downloads.proxy_platforms`) and the cookies file
    (`YOUTUBE_COOKIES_FILE`, YouTube only, only when the file exists) for `platform`."""
    proxy = proxy_for("twitch" if platform == "twitch" else "youtube")
    cookies = cookies_file() if platform == "youtube" else None
    return Access(proxy=proxy, cookies_file=cookies)


def normalise(text: str) -> str:
    """Lower case, with the typographic apostrophes YouTube uses made plain."""
    return text.lower().replace("’", "'").replace("‘", "'")


def last_error_line(text: str) -> str:
    """The last `ERROR:` line of yt-dlp's output, else its last non-empty line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if line.upper().startswith("ERROR")]
    chosen = errors[-1] if errors else (lines[-1] if lines else "")
    return chosen[-STDERR_TAIL_CHARS:]


def classify_failure(code: int, output: str) -> Failure:
    """Turn the exit code and output of a failed yt-dlp run into a `Failure`. Pure.

    The bot check is tested first: its text also says "Sign in", like the age gate, but it is about this
    machine and not about the video. A failure of the proxy is tested before everything when it is the
    last error (the tunnel's "403 Forbidden" or "429" is the proxy's answer, not the platform's), and
    again before the media server's 403 when it is anywhere in the output; a proxy hiccup that yt-dlp
    retried past does not hide a bot check or a video that is gone.
    """
    text = normalise(output)
    detail = last_error_line(output) or f"exit code {code}"
    if proxy_refusal(detail) is not None:
        return Failure("proxy", detail)
    if any(marker in text for marker in BLOCKED_MARKERS):
        return Failure("blocked", detail)
    if any(marker in text for marker in UNAVAILABLE_MARKERS):
        return Failure("unavailable", detail)
    if proxy_refusal(output) is not None:
        lines = [line.strip() for line in output.splitlines() if proxy_refusal(line) is not None]
        return Failure("proxy", lines[-1][-STDERR_TAIL_CHARS:] if lines else detail)
    if any(marker in text for marker in FORBIDDEN_MARKERS):
        return Failure("forbidden", detail)
    if code == EXIT_TIMEOUT:
        return Failure("timeout", detail)
    return Failure("error", detail)


def error_for(failure: Failure) -> YtDlpError:
    """The exception that carries `failure`."""
    if failure.kind == "proxy":
        return ProxyRefusedError(failure)
    if failure.kind == "blocked":
        return DownloadBlockedError(failure)
    if failure.kind == "unavailable":
        return VideoUnavailableError(failure)
    return YtDlpError(failure)


def scrub(text: str, access: Access) -> str:
    """Remove the proxy URL and any credentials inside a URL from text that will be logged or stored."""
    return redact_proxy(text, access.proxy)


def base_command(access: Access) -> list[str]:
    """The start of every yt-dlp command of the lane."""
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-progress",
        "--socket-timeout",
        str(SOCKET_TIMEOUT_S),
        "--retries",
        str(NETWORK_RETRIES),
        "--extractor-retries",
        str(NETWORK_RETRIES),
    ]
    if access.proxy:
        command += ["--proxy", access.proxy]
        if access.pot_url:
            # bgutil's HTTP provider; it mints the token through the same proxy yt-dlp uses.
            command += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={access.pot_url}"]
    if access.cookies_file is not None:
        command += ["--cookies", str(access.cookies_file)]
    return command


def run_subprocess(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
    """Run a command to the end. A timeout kills it and gives exit code -1; a start failure gives -2."""
    try:
        done = subprocess.run(
            list(command), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return EXIT_TIMEOUT, "", f"timed out after {timeout_s:.0f} s"
    except OSError as exc:
        return EXIT_NOT_STARTED, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")


def run(
    arguments: Sequence[str],
    *,
    access: Access,
    timeout_s: float,
    what: str,
    runner: Runner | None = None,
) -> str:
    """Run yt-dlp with `arguments` after the base command and return its standard output.

    Raises `DownloadBlockedError`, `VideoUnavailableError` or `YtDlpError` (kind `forbidden`, `timeout`
    or `error`) when it fails. One structured log line per run; the command itself is never logged.
    """
    log = get_logger("vod")
    started = time.monotonic()
    if access.proxy and access.meter is not None:
        access.meter.count_call()
    code, stdout, stderr = (runner or run_subprocess)([*base_command(access), *arguments], timeout_s)
    duration_ms = int((time.monotonic() - started) * 1000)
    if code == 0:
        log.info("ytdlp_done", what=what, duration_ms=duration_ms)
        return stdout
    failure = classify_failure(code, scrub(f"{stdout}\n{stderr}", access))
    log.warning(
        "ytdlp_failed",
        what=what,
        kind=failure.kind,
        exit_code=code,
        duration_ms=duration_ms,
        detail=failure.detail,
    )
    raise error_for(failure)

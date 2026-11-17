"""YouTube search through yt-dlp: the free, frequent source of discovery (no API quota, no Apify charge).

This is the one network call of discovery that lives outside `sixstars.clients` (like the chat adapters).
It runs `yt-dlp --flat-playlist --dump-json` on a YouTube results URL, with a timeout, retries with
backoff and one structured log line per search. `FakeYtDlpSearch` is the test double.

Verified against yt-dlp 2026.10.26 and YouTube on 2026-11-17:
- The `ytsearchdateN:` prefix no longer exists in yt-dlp ("Unsupported url scheme"). Filters go through a
  results URL with an `sp` parameter instead (extractor `youtube:search_url`, "YouTube search URLs with
  sorting and filter support": https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).
- A flat search row carries `id`, `title`, `channel`, `channel_id` (None for multi-channel collaborations),
  `duration` (None while live), `view_count` (None while live), `concurrent_view_count` (live only) and
  `live_status` (`is_live`, `was_live`, `is_upcoming` or None). It carries no upload time, like count,
  comment count or subscriber count: those come from the Data API (`videos.list`).
- One search of 40 results takes about 3 s (two result pages).

The `sp` value is the base64 of a small protobuf: field 1 = sort order, field 2 = filters (1 upload date,
2 type, 3 duration, 8 live). The values below reproduce what the YouTube web filter menu sends
(`EgJAAQ==` for "Live", `EgIIAg==` for "Today", ...); they are not a documented API and are checked by the
live test `tests/live/test_discovery_live.py`.
"""

import base64
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import quote

from sixstars.clients import _http
from sixstars.clients._http import ClientError
from sixstars.logging import get_logger

SERVICE: Final = "ytdlp"
RESULTS_URL: Final = "https://www.youtube.com/results"
ATTEMPTS: Final = 2
BACKOFF_S: Final[tuple[float, ...]] = (2.0, 5.0)
SOCKET_TIMEOUT_S: Final = 10
DEFAULT_TIMEOUT_S: Final = 25.0

Sort = Literal["relevance", "rating", "date", "views"]
SORT_CODES: Final[dict[str, int]] = {"relevance": 0, "rating": 1, "date": 2, "views": 3}
DATE_TODAY: Final = 2
DATE_WEEK: Final = 3
DATE_MONTH: Final = 4
TYPE_VIDEO: Final = 1

# `runner(command, timeout_s)` returns (exit code, stdout, stderr).
Runner = Callable[[Sequence[str], float], tuple[int, str, str]]


class YtDlpError(ClientError):
    """yt-dlp failed on every attempt of one search."""


@dataclass(frozen=True)
class FlatVideo:
    """One row of a flat YouTube search listing."""

    video_id: str
    title: str
    channel_id: str | None
    channel_name: str
    live_status: str | None  # is_live | was_live | is_upcoming | None
    viewers: int | None
    views: int | None
    duration_s: int | None

    @property
    def is_live(self) -> bool:
        return self.live_status == "is_live"


def date_code_for(hours: int | None) -> int | None:
    """The smallest upload-date filter that covers a look-back of `hours`."""
    if hours is None:
        return None
    if hours <= 24:
        return DATE_TODAY
    if hours <= 24 * 7:
        return DATE_WEEK
    return DATE_MONTH


def build_sp(
    *, live: bool = False, date_code: int | None = None, sort: str = "relevance", videos_only: bool = True
) -> str:
    """Encode the search filters as YouTube's `sp` query value."""
    filters = b""
    if date_code:
        filters += bytes([0x08, date_code])
    if videos_only:
        filters += bytes([0x10, TYPE_VIDEO])
    if live:
        filters += bytes([0x40, 0x01])
    message = b""
    code = SORT_CODES.get(sort, 0)
    if code:
        message += bytes([0x08, code])
    if filters:
        message += bytes([0x12, len(filters)]) + filters
    return base64.b64encode(message).decode("ascii")


def build_search_url(term: str, sp: str) -> str:
    return f"{RESULTS_URL}?search_query={quote(term)}&sp={quote(sp)}"


def build_command(url: str, limit: int, proxy: str | None) -> list[str]:
    """The yt-dlp command of one flat search (the proxy URL embeds credentials: never log the command)."""
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--no-progress",
        "--playlist-end",
        str(limit),
        "--socket-timeout",
        str(SOCKET_TIMEOUT_S),
        "--extractor-retries",
        "2",
    ]
    if proxy:
        command += ["--proxy", proxy]
    command.append(url)
    return command


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def parse_rows(stdout: str) -> list[FlatVideo]:
    """Parse the JSON Lines yt-dlp printed. Rows that are not videos (channels, playlists) are skipped."""
    found: dict[str, FlatVideo] = {}
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = row.get("id")
        if not isinstance(video_id, str) or not video_id or row.get("ie_key") != "Youtube":
            continue
        if video_id in found:
            continue
        status = row.get("live_status")
        channel_id = row.get("channel_id")
        found[video_id] = FlatVideo(
            video_id=video_id,
            title=str(row.get("title") or ""),
            channel_id=channel_id if isinstance(channel_id, str) and channel_id else None,
            channel_name=str(row.get("channel") or row.get("uploader") or ""),
            live_status=status if isinstance(status, str) else None,
            viewers=_int(row.get("concurrent_view_count")),
            views=_int(row.get("view_count")),
            duration_s=_int(row.get("duration")),
        )
    return list(found.values())


def run_subprocess(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
    """Run yt-dlp. A timeout kills it and returns code -1 with whatever it had printed so far."""
    try:
        done = subprocess.run(
            list(command), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else ""
        return -1, partial, f"timed out after {timeout_s:.0f} s"
    except OSError as exc:
        return -2, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")


class YtDlpSearch:
    """Flat YouTube searches. Safe to call from several threads (each search is its own process)."""

    def __init__(
        self, proxy: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S, runner: Runner | None = None
    ) -> None:
        self._proxy = proxy
        self._timeout_s = timeout_s
        self._runner = runner or run_subprocess

    def is_configured(self) -> bool:
        """yt-dlp needs no credentials."""
        return True

    def search(
        self,
        term: str,
        *,
        live: bool = False,
        within_hours: int | None = None,
        sort: str = "relevance",
        limit: int = 40,
    ) -> list[FlatVideo]:
        """Search YouTube for `term`. `live=True` returns streams that are live now (the date filter and
        the sort order are then ignored); otherwise videos uploaded within about `within_hours`.

        Rows that yt-dlp printed before failing are kept: a partial listing is better than none. An empty
        listing is a normal answer. Raises `YtDlpError` when every attempt failed with nothing printed.
        """
        log = get_logger(SERVICE)
        sp = build_sp(live=True) if live else build_sp(date_code=date_code_for(within_hours), sort=sort)
        command = build_command(build_search_url(term, sp), limit, self._proxy)
        error = ""
        for attempt in range(1, ATTEMPTS + 1):
            started = time.monotonic()
            code, stdout, stderr = self._runner(command, self._timeout_s)
            rows = parse_rows(stdout)
            duration_ms = int((time.monotonic() - started) * 1000)
            if code == 0 or rows:
                log.info(
                    "ytdlp_search",
                    term=term,
                    live=live,
                    sort=sort,
                    results=len(rows),
                    exit_code=code,
                    attempt=attempt,
                    duration_ms=duration_ms,
                )
                return rows[:limit]
            error = (stderr.strip().splitlines() or [f"exit code {code}"])[-1][:300]
            log.warning(
                "ytdlp_search_failed",
                term=term,
                live=live,
                exit_code=code,
                attempt=attempt,
                duration_ms=duration_ms,
                error=error,
            )
            if attempt < ATTEMPTS:
                _http.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
        raise YtDlpError(f"yt-dlp search failed for {term!r}: {error}")


class FakeYtDlpSearch(YtDlpSearch):
    """Test double: canned results per `(term, live)`, or `default_live` / `default_recent` for any term.
    Every call is recorded in `calls`; `error` makes every search raise."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []
        self.results: dict[tuple[str, bool], list[FlatVideo]] = {}
        self.default_live: list[FlatVideo] = []
        self.default_recent: list[FlatVideo] = []
        self.error: Exception | None = None

    def search(
        self,
        term: str,
        *,
        live: bool = False,
        within_hours: int | None = None,
        sort: str = "relevance",
        limit: int = 40,
    ) -> list[FlatVideo]:
        self.calls.append(
            {"term": term, "live": live, "within_hours": within_hours, "sort": sort, "limit": limit}
        )
        if self.error is not None:
            raise self.error
        default = self.default_live if live else self.default_recent
        return list(self.results.get((term, live), default))[:limit]

"""`sixstars discovery doctor`: checks every source path once and says which work from this machine.

A diagnostic of our own configured paths. Each check makes one cheap, read-only request the way the
system itself would (same clients, same proxy and cookies rules) and reports `ok`, `blocked`,
`not configured`, `error` or `skipped` with the class of the error:

| Check | What it does | Cost |
|---|---|---|
| `ytdlp_search` | one yt-dlp live search for the first search term, 5 results | free |
| `data_api` | `videos.list` for one video | 1 quota unit |
| `data_api_search` | `search.list` (only with `--search-api`) | 1 of the 100 daily search calls |
| `rss` | one channel feed | free |
| `chat_replay` | the watch page of one video and the first page of its chat replay | free |
| `window_download` | a 5-second window of one video at low height, deleted afterwards | free |
| `twitch_token` | an app token and the game lookup | free |
| `apify` | only whether the token is set (a run costs money) | free |

`blocked` means the platform refused this machine (bot check, rate limit, a connection closed without an
answer): the fix is the owner's `PROXY_URL` or `YOUTUBE_COOKIES_FILE`, never a workaround in code.
Nothing is written except the usual `api_usage` rows of the clients.
"""

import asyncio
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from sixstars import db
from sixstars.chat import youtube_innertube as innertube
from sixstars.chat.models import ChatSourceError
from sixstars.chat.net import proxy_for
from sixstars.clients._http import ApiError, NotConfiguredError
from sixstars.clients.apify import ApifyClient
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.config import env
from sixstars.discovery.config import DiscoveryConfig
from sixstars.discovery.sources import search_api_queries
from sixstars.discovery.ytsearch import YtDlpSearch
from sixstars.logging import get_logger
from sixstars.vod import sections, ytdlp
from sixstars.vod.ytdlp import Access

OK: Final = "ok"
BLOCKED: Final = "blocked"
ERROR: Final = "error"
NOT_CONFIGURED: Final = "not configured"
SKIPPED: Final = "skipped"

# A short public video that has been online since 2005 ("Me at the zoo", 19 s), used when the database
# knows no video yet, and its channel for the feed check.
DEFAULT_VIDEO: Final = "jNQXAC9IVRw"
DEFAULT_CHANNEL: Final = "UC4QobU6STFB0P71PMvOGN5A"
WINDOW_S: Final = 5.0
WINDOW_MAX_HEIGHT: Final = 360
WINDOW_TIMEOUT_S: Final = 120.0
SEARCH_RESULTS: Final = 5
DETAIL_CHARS: Final = 300
# Text of a refusal that is about this machine, not about the request (lower case). The first three are
# what the staging server got on every yt-dlp search.
BLOCK_MARKERS: Final[tuple[str, ...]] = (
    "remote end closed connection",
    "connection aborted",
    "remotedisconnected",
    "connection reset",
    "cookie consent page",
    "http 429",
    "returned http 403",
    *ytdlp.BLOCKED_MARKERS,
)

_log = get_logger("discovery")


@dataclass(frozen=True)
class Check:
    """The result of one check."""

    name: str
    status: str  # ok | blocked | error | not configured | skipped
    detail: str
    error_class: str = ""
    seconds: float = 0.0


def looks_blocked(text: str) -> bool:
    """Is this error text a refusal of this machine (bot check, rate limit, closed connection)?"""
    lowered = ytdlp.normalise(text)
    return any(marker in lowered for marker in BLOCK_MARKERS)


def failure(name: str, exc: BaseException) -> Check:
    """Turn an exception into a `blocked`, `not configured` or `error` check."""
    text = " ".join(str(exc).split())[:DETAIL_CHARS]
    kind = type(exc).__name__
    if isinstance(exc, NotConfiguredError):
        return Check(name, NOT_CONFIGURED, text, kind)
    if isinstance(exc, ytdlp.DownloadBlockedError) or looks_blocked(text):
        return Check(name, BLOCKED, text, kind)
    if isinstance(exc, ApiError):
        return Check(name, ERROR, text, f"{kind} {exc.status}")
    return Check(name, ERROR, text or kind, kind)


def timed(name: str, check: Callable[[], Check]) -> Check:
    """Run one check; whatever it raises becomes a result, so one broken path never hides the others."""
    started = time.monotonic()
    try:
        result = check()
    except Exception as exc:
        result = failure(name, exc)
    seconds = round(time.monotonic() - started, 1)
    _log.info("doctor_check", check=result.name, status=result.status, error_class=result.error_class)
    return Check(result.name, result.status, result.detail, result.error_class, seconds)


# ── what to probe ────────────────────────────────────────────


def known_stream() -> str | None:
    """The newest past YouTube stream the database knows (it should have a chat replay), else None."""
    row = db.fetch_one(
        "select source_id from videos where platform = 'youtube' and has_chat "
        "order by created_at desc limit 1"
    )
    return str(row["source_id"]) if row is not None else None


def known_channel() -> str | None:
    row = db.fetch_one(
        "select channel_id from creators where platform = 'youtube' order by updated_at desc limit 1"
    )
    return str(row["channel_id"]) if row is not None else None


# ── the checks ───────────────────────────────────────────────


def check_environment() -> Check:
    """Which of the owner-supplied aids are present (names only, never values)."""
    settings = env()
    proxy = "set" if settings.PROXY_URL is not None else "not set"
    if settings.PROXY_URL is not None and proxy_for("youtube") is None:
        proxy = "set, but youtube is not in downloads.proxy_platforms"
    cookies = settings.YOUTUBE_COOKIES_FILE
    if cookies is None:
        cookie_state = "not set"
    else:
        cookie_state = "set" if cookies.is_file() else f"names a file that does not exist ({cookies})"
    twitch = "set" if settings.TWITCH_CLIENT_ID and settings.TWITCH_CLIENT_SECRET else "not set"
    parts = [
        f"APP_ENV {settings.APP_ENV}",
        f"PROXY_URL {proxy}",
        f"YOUTUBE_COOKIES_FILE {cookie_state}",
        f"YOUTUBE_API_KEY {'set' if settings.YOUTUBE_API_KEY is not None else 'not set'}",
        f"Twitch keys {twitch}",
        f"APIFY_TOKEN {'set' if settings.APIFY_TOKEN is not None else 'not set'}",
    ]
    return Check("environment", OK, "; ".join(parts))


def check_ytdlp_search(search: YtDlpSearch, term: str) -> Check:
    rows = search.search(term, live=True, limit=SEARCH_RESULTS)
    return Check("ytdlp_search", OK, f"{len(rows)} live results for {term!r}")


def check_data_api(youtube: YouTubeClient, video_id: str) -> tuple[Check, str | None]:
    """`videos.list` for one id. Also returns that video's channel id (for the feed check)."""
    if not youtube.is_configured():
        return Check("data_api", NOT_CONFIGURED, "YOUTUBE_API_KEY is not set"), None
    items = youtube.videos_list([video_id], parts=("snippet",))
    if not items:
        return Check("data_api", OK, f"videos.list answered; video {video_id} is unknown to it"), None
    channel = (items[0].get("snippet") or {}).get("channelId")
    detail = f"videos.list answered for {video_id} (1 quota unit)"
    return Check("data_api", OK, detail), str(channel) if channel else None


def check_data_api_search(youtube: YouTubeClient, config: DiscoveryConfig) -> Check:
    if not youtube.is_configured():
        return Check("data_api_search", NOT_CONFIGURED, "YOUTUBE_API_KEY is not set")
    queries = search_api_queries(config, "live")
    if not queries:
        return Check("data_api_search", SKIPPED, "env.search_terms is empty")
    items = youtube.search_list(queries[0], event_type="live", order="viewCount", max_results=SEARCH_RESULTS)
    return Check("data_api_search", OK, f"search.list: {len(items)} live results (1 search call)")


def check_rss(youtube: YouTubeClient, channel_id: str) -> Check:
    entries = youtube.channel_feed(channel_id)
    return Check("rss", OK, f"{len(entries)} entries in the feed of {channel_id}")


async def _chat_first_page(video_id: str, proxy: str | None, cookies: Path | None) -> Check:
    async with innertube.new_client(proxy, cookies) as http:
        page = await innertube.fetch_watch_page(http, video_id)
        if page.continuation is None:
            if page.unplayable_reason is not None:
                reason = f"watch page of {video_id} is unplayable: {page.unplayable_reason}"
                status = BLOCKED if looks_blocked(reason) else ERROR
                return Check("chat_replay", status, reason[:DETAIL_CHARS], "VideoUnavailableError")
            detail = (
                f"watch page of {video_id} readable; this video has no chat replay "
                "(pass --video <id of a past stream> to test one)"
            )
            return Check("chat_replay", OK, detail)
        endpoint = innertube.REPLAY_ENDPOINT if page.is_replay else "get_live_chat"
        chat = await innertube.post_chat(http, page, endpoint, page.continuation)
        actions = 0 if chat is None else len(chat.actions)
        kind = "chat replay" if page.is_replay else "live chat"
        return Check("chat_replay", OK, f"{kind} of {video_id} readable (first page: {actions} actions)")


def check_chat_replay(video_id: str, access: Access) -> Check:
    try:
        return asyncio.run(_chat_first_page(video_id, access.proxy, access.cookies_file))
    except ChatSourceError as exc:
        return failure("chat_replay", exc)


def check_window_download(video_id: str, access: Access, runner: ytdlp.Runner | None = None) -> Check:
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory(prefix="sixstars-doctor-") as temp:
        done = sections.download_section(
            url,
            sections.Section(0.0, WINDOW_S),
            Path(temp),
            "doctor",
            access=access,
            max_height=WINDOW_MAX_HEIGHT,
            timeout_s=WINDOW_TIMEOUT_S,
            runner=runner,
        )
        detail = f"{done.duration_s:.1f} s of {video_id} downloaded at {done.height}p and deleted"
    return Check("window_download", OK, detail)


def check_twitch(twitch: TwitchClient, game: str) -> Check:
    if not twitch.is_configured():
        return Check("twitch_token", NOT_CONFIGURED, "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set")
    game_id = twitch.get_game_id(game)
    found = f"game id {game_id}" if game_id is not None else "no category of that name yet"
    return Check("twitch_token", OK, f"app token accepted; {game!r}: {found}")


def check_apify(apify: ApifyClient) -> Check:
    if not apify.is_configured():
        return Check("apify", NOT_CONFIGURED, "APIFY_TOKEN is not set")
    return Check("apify", OK, "token set (not called: a run costs money)")


# ── the run ──────────────────────────────────────────────────


@dataclass
class DoctorDeps:
    """The clients the doctor uses; tests pass fakes."""

    youtube: YouTubeClient
    twitch: TwitchClient
    apify: ApifyClient
    search: YtDlpSearch
    access: Access
    runner: ytdlp.Runner | None = None  # the yt-dlp runner of the window download
    chat_check: Callable[[str, Access], Check] = check_chat_replay

    @classmethod
    def default(cls, config: DiscoveryConfig) -> "DoctorDeps":
        from sixstars.discovery.run import DiscoveryClients

        clients = DiscoveryClients.default(config)
        assert clients.youtube is not None and clients.twitch is not None
        assert clients.apify is not None and clients.search is not None
        return cls(
            youtube=clients.youtube,
            twitch=clients.twitch,
            apify=clients.apify,
            search=clients.search,
            access=ytdlp.access_for("youtube"),
        )


def run_doctor(
    *, video: str | None = None, search_api: bool = False, deps: DoctorDeps | None = None
) -> list[Check]:
    """Run every check once and return the results in a fixed order."""
    config = DiscoveryConfig.from_settings()
    deps = deps or DoctorDeps.default(config)
    stream = innertube.extract_video_id(video) if video else known_stream()
    probe = stream or DEFAULT_VIDEO
    term = config.search_terms[0] if config.search_terms else config.target_game
    checks = [timed("environment", check_environment)]
    checks.append(timed("ytdlp_search", lambda: check_ytdlp_search(deps.search, term)))

    channel: list[str | None] = [None]

    def data_api() -> Check:
        result, channel[0] = check_data_api(deps.youtube, probe)
        return result

    checks.append(timed("data_api", data_api))
    if search_api:
        checks.append(timed("data_api_search", lambda: check_data_api_search(deps.youtube, config)))
    feed = known_channel() or channel[0] or DEFAULT_CHANNEL
    checks.append(timed("rss", lambda: check_rss(deps.youtube, feed)))
    checks.append(timed("chat_replay", lambda: deps.chat_check(probe, deps.access)))
    checks.append(timed("window_download", lambda: check_window_download(probe, deps.access, deps.runner)))
    checks.append(timed("twitch_token", lambda: check_twitch(deps.twitch, config.target_game)))
    checks.append(timed("apify", lambda: check_apify(deps.apify)))
    return checks


def as_rows(checks: list[Check]) -> list[dict[str, Any]]:
    return [asdict(check) for check in checks]


def format_table(checks: list[Check]) -> str:
    """The table the command prints."""
    width = max(len(check.name) for check in checks)
    lines = [f"{'check':<{width}}  {'status':<14}  {'time':>6}  detail"]
    for check in checks:
        label = check.status.upper() if check.status in (OK, BLOCKED, ERROR) else check.status
        error = f"[{check.error_class}] " if check.error_class else ""
        lines.append(f"{check.name:<{width}}  {label:<14}  {check.seconds:>5.1f}s  {error}{check.detail}")
    refused = [check.name for check in checks if check.status == BLOCKED]
    if refused:
        lines.append("")
        lines.append(
            f"Blocked from this machine: {', '.join(refused)}. Fix: set PROXY_URL (a residential proxy) "
            "or YOUTUBE_COOKIES_FILE in the .env and restart the containers; see the operator table in "
            "development-plan/progress-notes/hardening-sources.md."
        )
    return "\n".join(lines)

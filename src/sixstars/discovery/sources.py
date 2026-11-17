"""Discovery sources: gathers candidate creators and videos for the target game from Twitch, YouTube and
Apify. Implemented in M09.

Every fetcher returns normalised `Candidate` objects plus a `SourceReport`. A source that fails is
reported and skipped; the pass goes on with the others.

Cost design (the reason the YouTube route differs from `docs/06-discovery.md`): the Apify actor costs
$0.003 per video and the plan allows $19 a month, so Apify cannot run every 10 minutes. The frequent work
uses free or nearly free sources:

1. yt-dlp search (`ytsearch.py`): live streams and recent videos per search term. Free.
2. Channel RSS feeds of creators we already know (a rotating slice per pass). Free.
3. YouTube Data API `videos.list`, 1 quota unit per 50 videos, for what the listings lack: start and
   publish times, likes, comments, language, live state. Capped by `discovery.youtube_quota_daily_units`.
4. Apify: one small run every `discovery.apify_interval_s`, alternating live and recent, started in one
   pass and read in a later one, behind a daily and a monthly spend guard. Discovery works without it.

Sources added by the hardening task (the staging server's address cannot run yt-dlp searches: YouTube
closes the connection on every one):

5. Data API `search.list` (`search_api_step`): the official search, used by default only when the yt-dlp
   search failed, found nothing or is backing off in this pass (`discovery.search_api_mode` = `fallback`).
   Since 2026-06-01 `search.list` has its own quota bucket of 100 calls a day, one unit per call
   (https://developers.google.com/youtube/v3/determine_quota_cost); it no longer draws on the 10,000
   units of `videos.list`. The budget is counted in calls: at most `search_api_calls_per_pass` per pass,
   one call per `search_api_interval_s`, `search_api_daily_calls` per UTC day. Calls alternate between
   the modes (`live`, `recent`). Measured on 2026-11-17 with the GTA 5 terms: for videos of the last 48
   hours one OR query over all terms works well (50 of 50 results usable with `videoDuration=long`,
   against 24 of 50 without the filter, the rest being Shorts), but for live streams the OR query found
   4 streams with 50 or more viewers where the single term "GTA 5" found 17. So the modes in
   `search_api_combined_modes` use the OR query and the others take one term per call, in turn.
6. Known live streams (`known_live_stubs`): streams seen live within `discovery.live_recheck_window_s`
   and every stream that holds a live slot are looked up again with `videos.list` (1 unit per 50), so a
   stream found by an hourly search is still tracked every pass (viewers, growth, "still live?") and a
   stream that has just ended becomes a video candidate.

Data API fields used (checked against real responses on 2026-11-17):
https://developers.google.com/youtube/v3/docs/videos#resource : `snippet.publishedAt`, `snippet.channelId`,
`snippet.channelTitle`, `snippet.liveBroadcastContent` (`live` | `upcoming` | `none`),
`snippet.defaultAudioLanguage`, `contentDetails.duration` (ISO 8601, `P0D` while live),
`statistics.viewCount|likeCount|commentCount` (strings), `liveStreamingDetails.actualStartTime|
actualEndTime|concurrentViewers` (absent for a normal upload).
"""

import html
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sixstars import db, settings, usage
from sixstars.clients._http import ApiError, ClientError
from sixstars.clients.apify import ApifyClient, build_youtube_search_input, normalise_youtube_item
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.youtube import CATEGORY_GAMING, IDS_PER_REQUEST_MAX, YouTubeClient
from sixstars.config import env
from sixstars.discovery import health, scoring, store
from sixstars.discovery.config import DiscoveryConfig
from sixstars.discovery.models import FORBIDDEN_TIERS, Candidate
from sixstars.discovery.ytsearch import FlatVideo, YtDlpSearch
from sixstars.logging import get_logger

TWITCH_GAME_CACHE_S: Final = 24 * 3600
APIFY_GIVE_UP_S: Final = 3600  # a run still unfinished after this is forgotten
APIFY_STATE_KEY: Final = "discovery.apify_last_run"
RSS_CURSOR_KEY: Final = "discovery.rss_cursor"
TWITCH_GAME_KEY: Final = "discovery.twitch_game_cache"
SEARCH_API_STATE_KEY: Final = "discovery.search_api_state"
SEARCH_API_SOURCE: Final = "youtube_search_api"
SEARCH_API_MODES: Final[tuple[str, ...]] = ("live", "recent", "completed")
SEARCH_API_JITTER_S: Final = 30  # a pass that comes this much early still counts as "interval reached"
QUOTA_RESET_UTC: Final = (8, 10)  # midnight Pacific is 07:00 or 08:00 UTC; wait until 08:10 to be sure
ERROR_CHARS: Final = 300
RECHECK_ORIGIN: Final = "recheck"
VIDEO_PARTS: Final = ("snippet", "contentDetails", "statistics", "liveStreamingDetails")
_ISO_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")

_log = get_logger("discovery")


@dataclass
class SourceReport:
    """What one source did in a pass. `status` is ok, empty, skipped, not_configured or error."""

    name: str
    status: str
    found: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "found": self.found, "detail": self.detail}


@dataclass
class Gathered:
    """Everything the sources returned in one pass."""

    candidates: list[Candidate] = field(default_factory=list)
    reports: list[SourceReport] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    youtube_quota_units: int = 0
    apify_cost_usd: float = 0.0
    searches: int = 0
    search_api_calls: int = 0
    notes: list[str] = field(default_factory=list)  # things the summary says once (budget spent, ...)

    def skip(self, reason: str, count: int = 1) -> None:
        if count:
            self.skipped[reason] = self.skipped.get(reason, 0) + count


@dataclass
class Stub:
    """A YouTube video as the cheap listings know it, before the Data API fills the gaps."""

    video_id: str
    title: str = ""
    channel_id: str | None = None
    channel_name: str = ""
    live_status: str | None = None
    viewers: int | None = None
    views: int | None = None
    duration_s: int | None = None
    published_at: datetime | None = None
    likes: int | None = None
    comments: int | None = None
    followers: int | None = None
    origin: str = "ytdlp"
    needs_title_match: bool = False  # found through a channel feed: keep only when the title names the game
    description: str = ""  # only the Data API search gives one (used by the language check)


# ── small parsers ────────────────────────────────────────────


def parse_iso_duration(value: Any) -> int | None:
    """Seconds of an ISO 8601 duration such as `PT1H2M3S` or `P1DT2H`; None when unreadable."""
    if not isinstance(value, str):
        return None
    match = _ISO_DURATION.match(value)
    if match is None:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def short_language(value: Any) -> str | None:
    """`en-US` -> `en`; None when the API reported nothing."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold().split("-")[0]


def youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


# ── Twitch ───────────────────────────────────────────────────


def twitch_game_id(client: TwitchClient, name: str, now: datetime) -> str | None:
    """Game id of the target game, looked up once and cached in settings for 24 hours."""
    cached = settings.get(TWITCH_GAME_KEY, None)
    if isinstance(cached, dict) and cached.get("name") == name and cached.get("id"):
        at = parse_time(cached.get("at"))
        if at is not None and (now - at).total_seconds() < TWITCH_GAME_CACHE_S:
            return str(cached["id"])
    game_id = client.get_game_id(name)
    if game_id is not None:
        settings.set(TWITCH_GAME_KEY, {"name": name, "id": game_id, "at": now.isoformat()})
    return game_id


def twitch_candidates(client: TwitchClient, config: DiscoveryConfig, now: datetime) -> list[Candidate] | None:
    """Live streams (by viewers) and the day's most viewed archives of the target game. None when Twitch
    has no category of that name (yet): before the game's release that is normal, not an error."""
    game_id = twitch_game_id(client, config.target_game, now)
    if game_id is None:
        return None
    streams = client.get_streams(game_id, max_items=config.twitch_max_streams)
    videos = client.get_videos_by_game(
        game_id, period="day", sort="views", type="archive", first=config.twitch_max_videos
    )
    found = [
        Candidate(
            platform="twitch",
            source_type="live",
            source_id=stream.id,
            url=f"https://www.twitch.tv/{stream.user_login}",
            title=stream.title,
            channel_id=stream.user_id,
            channel_handle=stream.user_login,
            channel_name=stream.user_name,
            origin="twitch",
            language=short_language(stream.language),
            viewers=stream.viewer_count,
            started_at=stream.started_at,
            has_chat=True,
        )
        for stream in streams
    ]
    live_stream_ids = {stream.id for stream in streams}
    for video in videos:
        if video.stream_id is not None and video.stream_id in live_stream_ids:
            continue  # the archive of a stream that is still running
        found.append(
            Candidate(
                platform="twitch",
                source_type="vod",
                source_id=video.id,
                url=video.url or f"https://www.twitch.tv/videos/{video.id}",
                title=video.title,
                channel_id=video.user_id,
                channel_handle=video.user_login,
                channel_name=video.user_name,
                origin="twitch",
                language=short_language(video.language),
                views=video.view_count,
                published_at=video.published_at or video.created_at,
                started_at=video.created_at,
                duration_s=video.duration_s,
                has_chat=True,
            )
        )
    return found


# ── YouTube: cheap listings ──────────────────────────────────


def _merge(stubs: dict[str, Stub], new: Stub) -> None:
    old = stubs.get(new.video_id)
    if old is None:
        stubs[new.video_id] = new
        return
    for name in ("channel_id", "viewers", "views", "duration_s", "published_at", "likes", "comments"):
        if getattr(old, name) is None:
            setattr(old, name, getattr(new, name))
    old.followers = old.followers if old.followers is not None else new.followers
    old.title = old.title or new.title
    old.channel_name = old.channel_name or new.channel_name
    old.description = old.description or new.description
    if old.origin == RECHECK_ORIGIN:
        old.origin = new.origin  # a source that found it now is the better label
    if new.live_status == "is_live" or old.live_status is None:
        old.live_status = new.live_status or old.live_status
    old.needs_title_match = old.needs_title_match and new.needs_title_match


def _from_flat(video: FlatVideo) -> Stub:
    return Stub(
        video_id=video.video_id,
        title=video.title,
        channel_id=video.channel_id,
        channel_name=video.channel_name,
        live_status=video.live_status,
        viewers=video.viewers,
        views=video.views,
        duration_s=video.duration_s,
        origin="ytdlp",
    )


def search_youtube(
    search: YtDlpSearch,
    config: DiscoveryConfig,
    pool: ThreadPoolExecutor,
    gate: health.Gate | None = None,
) -> tuple[list[Stub], SourceReport, int]:
    """Run the yt-dlp searches of one pass in parallel: per term one live search and one recent-video
    search per sort order. Returns the stubs, the report and the number of searches made.

    `gate` comes from the source health: while the source backs off nothing is run, and while it is known
    to be failing one search is tried first and the others only run when that one works."""
    tasks: list[tuple[str, bool, str]] = []
    for term in config.search_terms:
        tasks.append((term, True, "relevance"))
        tasks.extend((term, False, sort) for sort in config.youtube_recent_sorts)
    if gate is not None and gate.skip:
        return [], SourceReport("youtube_search", "skipped", 0, gate.detail), 0

    def one(task: tuple[str, bool, str]) -> list[FlatVideo] | Exception:
        term, live, sort = task
        try:
            return search.search(
                term,
                live=live,
                within_hours=config.vod_lookback_h,
                sort=sort,
                limit=config.youtube_search_results,
            )
        except Exception as exc:  # one failed search must not lose the others
            return exc

    results: list[list[FlatVideo] | Exception] = []
    if gate is not None and gate.probe and tasks:
        probe = one(tasks[0])
        if isinstance(probe, Exception):
            detail = f"{str(probe)[:ERROR_CHARS]} (probe: 1 search instead of {len(tasks)})"
            return [], SourceReport("youtube_search", "error", 0, detail), 1
        results.append(probe)
        tasks_left = tasks[1:]
    else:
        tasks_left = tasks
    results.extend(pool.map(one, tasks_left))
    stubs: list[Stub] = []
    failures = 0
    for result in results:
        if isinstance(result, Exception):
            failures += 1
            continue
        stubs.extend(_from_flat(video) for video in result)
    if tasks and failures == len(tasks):
        first = next(result for result in results if isinstance(result, Exception))
        return [], SourceReport("youtube_search", "error", 0, str(first)[:ERROR_CHARS]), len(tasks)
    detail = f"{len(tasks)} searches" + (f", {failures} failed" if failures else "")
    status = "ok" if stubs else "empty"
    return stubs, SourceReport("youtube_search", status, len(stubs), detail), len(tasks)


def rss_channels(limit: int) -> list[str]:
    """The next slice of known YouTube creators to read feeds for: preferred creators first is not needed
    because the cursor walks every allowed creator in turn (ordered by channel id, wrapping round)."""
    if limit <= 0:
        return []
    cursor = settings.get(RSS_CURSOR_KEY, None)
    start = cursor if isinstance(cursor, str) else ""
    base = (
        "select channel_id from creators where platform = 'youtube' and not (tier = any(%s)) "
        "and channel_id > %s order by channel_id limit %s"
    )
    forbidden = sorted(FORBIDDEN_TIERS)
    rows = db.fetch_all(base, (forbidden, start, limit))
    if len(rows) < limit and start:
        seen = {row["channel_id"] for row in rows}
        more = db.fetch_all(base, (forbidden, "", limit - len(rows)))
        rows += [row for row in more if row["channel_id"] not in seen]
    channels = [str(row["channel_id"]) for row in rows]
    if channels:
        settings.set(RSS_CURSOR_KEY, channels[-1])
    return channels


def read_feeds(
    client: YouTubeClient,
    channels: Sequence[str],
    config: DiscoveryConfig,
    now: datetime,
    pool: ThreadPoolExecutor,
) -> tuple[list[Stub], SourceReport]:
    """New uploads and streams of known creators from their RSS feeds (no quota). Only entries newer than
    the look-back whose title names the game are kept."""
    if not channels:
        return [], SourceReport("youtube_rss", "skipped", 0, "no known creators yet")
    oldest = now - timedelta(hours=config.vod_lookback_h)

    def one(channel_id: str) -> list[Stub] | Exception:
        try:
            entries = client.channel_feed(channel_id)
        except Exception as exc:
            return exc
        return [
            Stub(
                video_id=entry.video_id,
                title=entry.title,
                channel_id=entry.channel_id or channel_id,
                published_at=entry.published_at,
                origin="rss",
                needs_title_match=True,
            )
            for entry in entries
            if entry.published_at >= oldest and scoring.matches_terms(entry.title, game_terms(config))
        ]

    results = list(pool.map(one, channels))
    failures = sum(1 for result in results if isinstance(result, Exception))
    stubs = [stub for result in results if not isinstance(result, Exception) for stub in result]
    detail = f"{len(channels)} feeds" + (f", {failures} failed" if failures else "")
    if failures == len(channels):
        return [], SourceReport("youtube_rss", "error", 0, detail)
    return stubs, SourceReport("youtube_rss", "ok" if stubs else "empty", len(stubs), detail)


# ── YouTube: Data API search (search.list) ───────────────────


def search_api_modes(config: DiscoveryConfig) -> list[str]:
    return [mode for mode in config.search_api_modes if mode in SEARCH_API_MODES]


def search_api_queries(config: DiscoveryConfig, mode: str) -> list[str]:
    """The `q` values `mode` rotates through: one OR query over every term (each term quoted, joined with
    the documented `|` operator) for the modes in `discovery.search_api_combined_modes`, else one query
    per term."""
    terms = [term.strip() for term in config.search_terms if term.strip()]
    if mode in config.search_api_combined_modes and len(terms) > 1:
        return ["|".join(f'"{term}"' for term in terms)]
    return terms


def search_api_pick(config: DiscoveryConfig, number: int) -> tuple[str, str] | None:
    """The `(mode, query)` of call number `number` (0, 1, 2, ...): the modes alternate, and each mode walks
    through its own queries. None when there is nothing to search."""
    modes = search_api_modes(config)
    if not modes:
        return None
    mode = modes[number % len(modes)]
    queries = search_api_queries(config, mode)
    if not queries:
        return None
    return mode, queries[(number // len(modes)) % len(queries)]


def search_api_plan(config: DiscoveryConfig, calls: int) -> list[tuple[str, str]]:
    """The first `calls` picks of the rotation (for tests and the notes)."""
    picks = [search_api_pick(config, number) for number in range(calls)]
    return [pick for pick in picks if pick is not None]


def next_quota_reset(now: datetime) -> datetime:
    """The first moment after `now` at which Google has certainly reset the daily quota."""
    hour, minute = QUOTA_RESET_UTC
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return reset if reset > now else reset + timedelta(days=1)


def search_api_may_call(
    config: DiscoveryConfig,
    state: Mapping[str, Any],
    now: datetime,
    calls_today: int,
    primary_working: bool,
) -> tuple[bool, str]:
    """Pure guard: may one `search.list` call be made now? Returns `(yes, reason when no)`.

    The reason starts with "daily budget" exactly when the day's calls are used up."""
    if config.search_api_mode == "off" or config.search_api_calls_per_pass <= 0:
        return False, "switched off"
    if search_api_pick(config, 0) is None:
        return False, "no search terms or modes"
    if config.search_api_mode == "fallback" and primary_working:
        return False, "not needed: the yt-dlp search works"
    paused = parse_time(state.get("quota_until"))
    if paused is not None and now < paused:
        return False, f"Google reported the quota as used up; next try after {paused:%H:%M} UTC"
    if calls_today >= config.search_api_daily_calls:
        return False, f"daily budget spent ({calls_today} of {config.search_api_daily_calls} calls)"
    last = parse_time(state.get("last_call_at"))
    if last is not None:
        waited = (now - last).total_seconds()
        if 0 <= waited < config.search_api_interval_s - SEARCH_API_JITTER_S:
            left = config.search_api_interval_s - waited
            return False, f"interval not reached (next call in about {left / 60:.0f} min)"
    return True, ""


def _stub_from_search(item: Mapping[str, Any]) -> Stub | None:
    """A stub from a `search.list` result (https://developers.google.com/youtube/v3/docs/search#resource).
    Titles arrive HTML-escaped in search results."""
    video_id = (item.get("id") or {}).get("videoId")
    snippet = item.get("snippet") or {}
    if not isinstance(video_id, str) or not video_id:
        return None
    state = snippet.get("liveBroadcastContent")
    return Stub(
        video_id=video_id,
        title=html.unescape(str(snippet.get("title") or "")),
        channel_id=str(snippet.get("channelId") or "") or None,
        channel_name=html.unescape(str(snippet.get("channelTitle") or "")),
        live_status={"live": "is_live", "upcoming": "is_upcoming"}.get(str(state)),
        published_at=parse_time(snippet.get("publishedAt")),
        origin="ytapi",
        description=html.unescape(str(snippet.get("description") or "")),
    )


def is_quota_error(exc: Exception) -> bool:
    """Did Google refuse the call because the quota is used up? (403 `quotaExceeded` /
    `dailyLimitExceeded`, or 429 `rateLimitExceeded`: https://developers.google.com/youtube/v3/docs/errors)"""
    if not isinstance(exc, ApiError) or exc.status not in (403, 429):
        return False
    text = str(exc).casefold()
    return "quota" in text or "limit" in text


def search_api_step(
    client: YouTubeClient | None,
    config: DiscoveryConfig,
    now: datetime,
    primary_working: bool,
    calls_today: Callable[[], int] = store.youtube_search_calls_today,
) -> tuple[list[Stub], SourceReport | None, int, str | None]:
    """One step of the Data API search: make the calls this pass may make and advance the rotation.

    Returns the stubs, the report (None when the source is switched off), the number of calls made and a
    note for the summary. The note is given once per UTC day, on the first pass that finds the daily
    budget spent; after that the report alone says so and nothing is notified again.
    """
    if config.search_api_mode == "off":
        return [], None, 0, None
    if client is None or not client.is_configured():
        report = SourceReport(SEARCH_API_SOURCE, "not_configured", 0, "YOUTUBE_API_KEY is not set")
        return [], report, 0, None
    raw = settings.get(SEARCH_API_STATE_KEY, None)
    state: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    before = dict(state)
    used = calls_today()
    stubs: list[Stub] = []
    made: list[str] = []
    reason = ""
    error: str | None = None
    for _ in range(config.search_api_calls_per_pass):
        allowed, reason = search_api_may_call(config, state, now, used + len(made), primary_working)
        if not allowed:
            break
        number = int(state.get("index") or 0)
        pick = search_api_pick(config, number)
        if pick is None:
            break
        mode, query = pick
        state.update({"index": number + 1, "last_call_at": now.isoformat(), "last_mode": mode})
        try:
            items = client.search_list(
                query,
                event_type=mode if mode in ("live", "completed") else None,
                order=config.search_api_orders.get(mode, "relevance"),
                published_after=None if mode == "live" else now - timedelta(hours=config.vod_lookback_h),
                video_category_id=config.search_api_category_id or None,
                relevance_language=config.search_api_relevance_language or None,
                video_duration=config.search_api_durations.get(mode) or None,
                max_results=IDS_PER_REQUEST_MAX,
            )
        except ClientError as exc:
            made.append(mode)
            error = str(exc)[:ERROR_CHARS]
            if is_quota_error(exc):
                state["quota_until"] = next_quota_reset(now).isoformat()
            _log.warning("discovery_search_list_failed", mode=mode, error=error)
            break
        made.append(mode)
        found = [stub for stub in (_stub_from_search(item) for item in items) if stub is not None]
        stubs.extend(found)
        _log.info("discovery_search_list", mode=mode, results=len(found), calls_today=used + len(made))
    note: str | None = None
    today = now.strftime("%Y-%m-%d")
    if not made and reason.startswith("daily budget") and state.get("budget_noted_day") != today:
        state["budget_noted_day"] = today
        note = (
            f"search.list {reason}: no more Data API searches today (UTC); discovery continues on channel "
            "feeds, Apify and, when it works, yt-dlp"
        )
    if state != before:
        settings.set(SEARCH_API_STATE_KEY, state)
    total = used + len(made)
    budget = f"{total} of {config.search_api_daily_calls} calls today"
    if error is not None:
        report = SourceReport(SEARCH_API_SOURCE, "error", len(stubs), f"{error} ({budget})")
    elif made:
        detail = f"{' + '.join(made)} search, {budget}"
        report = SourceReport(SEARCH_API_SOURCE, "ok" if stubs else "empty", len(stubs), detail)
    else:
        detail = reason if reason.startswith("daily budget") else f"{reason}; {budget}"
        report = SourceReport(SEARCH_API_SOURCE, "skipped", 0, detail)
    return stubs, report, len(made), note


# ── YouTube: streams we already know are live ────────────────


def known_live_stubs(config: DiscoveryConfig, now: datetime) -> list[Stub]:
    """Stubs for the YouTube streams to look at again in this pass (see point 6 of the module docstring).
    They carry no live state: only `videos.list` can say whether the stream still runs."""
    rows = store.recent_live_sources(now, config.live_recheck_window_s, config.live_recheck_max)
    return [
        Stub(
            video_id=str(row["source_id"]),
            title=str(row.get("title") or ""),
            channel_id=str(row["channel_id"]),
            channel_name=str(row.get("channel_name") or ""),
            origin=RECHECK_ORIGIN,
        )
        for row in rows
    ]


# ── YouTube: Apify supplement ────────────────────────────────


def apify_projected_cost(config: DiscoveryConfig) -> float:
    """Most a run can cost: results per term x terms x price per result."""
    return len(config.search_terms) * config.apify_max_results * config.apify_price_per_result_usd


def apify_may_start(
    config: DiscoveryConfig, state: dict[str, Any] | None, now: datetime, spent_day: float, spent_month: float
) -> tuple[bool, str]:
    """Pure guard: may a new Apify run start now? Returns `(yes, reason when no)`."""
    cost = apify_projected_cost(config)
    if config.apify_interval_s <= 0 or cost <= 0:
        return False, "switched off"
    started = parse_time((state or {}).get("started_at"))
    if started is not None and (now - started).total_seconds() < config.apify_interval_s:
        return False, "interval not reached"
    if spent_day + cost > config.apify_daily_budget_usd:
        return False, f"daily budget ({spent_day:.2f} of {config.apify_daily_budget_usd:.2f} USD spent)"
    if spent_month + cost > config.apify_monthly_budget_usd:
        return False, f"monthly budget ({spent_month:.2f} of {config.apify_monthly_budget_usd:.2f} USD spent)"
    return True, ""


def apify_step(
    client: ApifyClient, config: DiscoveryConfig, now: datetime
) -> tuple[list[Stub], SourceReport, float]:
    """One step of the asynchronous Apify supplement: read the run a previous pass started (if it has
    finished), then start the next run when the interval and both budgets allow it. Never waits for a run.
    Returns the stubs read, the report and the dollars recorded in this pass."""
    raw = settings.get(APIFY_STATE_KEY, None)
    state: dict[str, Any] | None = dict(raw) if isinstance(raw, dict) else None
    stubs: list[Stub] = []
    cost = 0.0
    notes: list[str] = []
    if state is not None and state.get("run_id") and not state.get("collected"):
        started = parse_time(state.get("started_at"))
        stale = started is not None and (now - started).total_seconds() > APIFY_GIVE_UP_S
        try:
            run = client.get_run(str(state["run_id"]))
        except ClientError:
            if stale:  # a run we can no longer read must not block the next ones for ever
                settings.set(APIFY_STATE_KEY, {**state, "collected": True, "status": "LOST"})
            raise
        if run.finished or stale:
            cost = run.cost_usd
            usage.record("apify", "discovery_search", units=1, cost_usd=cost, ref_id=run.id)
            if run.succeeded and run.dataset_id is not None:
                limit = config.apify_max_results * max(1, len(config.search_terms)) + len(config.search_terms)
                for row in client.get_items(run.dataset_id, limit=max(1, limit)):
                    item = normalise_youtube_item(row)
                    if item is None:
                        continue
                    stubs.append(
                        Stub(
                            video_id=item.video_id,
                            title=item.title,
                            channel_id=item.channel_id or None,
                            channel_name=item.channel_name,
                            live_status="is_live" if item.is_live else None,
                            viewers=item.viewers,
                            views=item.views,
                            duration_s=item.duration_s,
                            published_at=item.published_at,
                            likes=item.likes,
                            comments=item.comments,
                            followers=item.subscribers,
                            origin="apify",
                        )
                    )
            state = {**state, "collected": True, "status": run.status, "cost_usd": cost}
            settings.set(APIFY_STATE_KEY, state)
            notes.append(f"read run {run.status.lower()} ({len(stubs)} videos, {cost:.3f} USD)")
        else:
            notes.append("previous run still running")
            return stubs, SourceReport("apify", "ok", 0, "; ".join(notes)), cost
    spent_day, spent_month = store.apify_spend()
    allowed, reason = apify_may_start(config, state, now, spent_day, spent_month)
    if not allowed:
        notes.append(f"no new run: {reason}")
        status = "ok" if stubs else "skipped"
        return stubs, SourceReport("apify", status, len(stubs), "; ".join(notes)), cost
    live = (state or {}).get("mode") != "live"  # alternate, starting with a live search
    payload = build_youtube_search_input(
        list(config.search_terms), live, config.vod_lookback_h, config.apify_max_results, sort=None
    )
    run_id = client.start_run(
        env().APIFY_YOUTUBE_ACTOR, payload, max_charge_usd=round(apify_projected_cost(config), 4)
    )
    settings.set(
        APIFY_STATE_KEY,
        {
            "run_id": run_id,
            "started_at": now.isoformat(),
            "mode": "live" if live else "recent",
            "collected": False,
        },
    )
    notes.append(f"started {'live' if live else 'recent'} run")
    return stubs, SourceReport("apify", "ok", len(stubs), "; ".join(notes)), cost


# ── YouTube: Data API enrichment ─────────────────────────────


def quota_needed(count: int) -> int:
    """Quota units of one `videos.list` over `count` ids (1 unit per request of 50)."""
    return -(-count // IDS_PER_REQUEST_MAX)


def language_of(
    reported: str | None,
    *,
    title: str,
    channel_name: str,
    description: str = "",
    tags: Sequence[str] = (),
    config: DiscoveryConfig | None = None,
) -> tuple[str | None, str | None]:
    """`(language, signal)`: the reported language corrected by the script and term check of
    `scoring.language_signal` (settings `discovery.non_latin_share`, `discovery.non_english_terms`)."""
    signal = scoring.language_signal(
        title=title,
        channel_name=channel_name,
        description=description,
        tags=tags,
        terms=config.non_english_terms if config is not None else (),
        share_threshold=config.non_latin_share if config is not None else scoring.NON_LATIN_SHARE,
    )
    language = scoring.detect_language(reported, signal)
    return language, signal if language == "und" else None


def candidate_from_api(
    item: dict[str, Any], stub: Stub | None, now: datetime, config: DiscoveryConfig | None = None
) -> Candidate | None:
    """Build a candidate from a `videos.list` item (the stub only supplies what the API lacks). Upcoming
    broadcasts give None."""
    snippet = item.get("snippet") or {}
    statistics = item.get("statistics") or {}
    live_details = item.get("liveStreamingDetails") or {}
    video_id = str(item.get("id") or "")
    channel_id = str(snippet.get("channelId") or (stub.channel_id if stub else "") or "")
    state = snippet.get("liveBroadcastContent")
    if not video_id or not channel_id or state == "upcoming":
        return None
    title = str(snippet.get("title") or (stub.title if stub else ""))
    name = str(snippet.get("channelTitle") or (stub.channel_name if stub else ""))
    # `defaultAudioLanguage` is what the uploader typed once, not what is spoken: Hindi streams marked
    # `en` were seen live. The script and term check may overrule a reported English.
    tags = snippet.get("tags")
    language, signal = language_of(
        short_language(snippet.get("defaultAudioLanguage")) or short_language(snippet.get("defaultLanguage")),
        title=title,
        channel_name=name,
        description=str(snippet.get("description") or ""),
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else (),
        config=config,
    )
    started = parse_time(live_details.get("actualStartTime"))
    common: dict[str, Any] = {
        "platform": "youtube",
        "source_id": video_id,
        "url": youtube_url(video_id),
        "title": title,
        "channel_id": channel_id,
        "channel_handle": name or channel_id,
        "channel_name": name,
        "origin": stub.origin if stub else "ytdlp",
        "language": language,
        "language_signal": signal,
        "likes": _count(statistics.get("likeCount")),
        "comments": _count(statistics.get("commentCount")),
        "published_at": parse_time(snippet.get("publishedAt")),
        "started_at": started,
        "followers": stub.followers if stub else None,
    }
    if state == "live":
        viewers = _count(live_details.get("concurrentViewers"))
        if viewers is None and stub is not None:
            viewers = stub.viewers
        return Candidate(source_type="live", viewers=viewers, has_chat=True, **common)
    was_live = bool(live_details) and (started is not None or live_details.get("actualEndTime") is not None)
    ended = parse_time(live_details.get("actualEndTime"))
    if ended is not None:
        # a finished stream is "published" for our purposes when it ended, not when it was scheduled
        common["published_at"] = ended
    return Candidate(
        source_type="vod",
        views=_count(statistics.get("viewCount")),
        duration_s=parse_iso_duration((item.get("contentDetails") or {}).get("duration")),
        has_chat=was_live,
        **common,
    )


def candidate_from_stub(stub: Stub, config: DiscoveryConfig | None = None) -> Candidate | None:
    """Build a candidate from the listing alone (used when the Data API is off budget or failing).
    Upcoming broadcasts, rows without a channel id and streams that were only due for a second look
    (nothing but the API can say whether they still run) give None."""
    if not stub.channel_id or stub.live_status == "is_upcoming" or stub.origin == RECHECK_ORIGIN:
        return None
    live = stub.live_status == "is_live"
    language, signal = language_of(
        None,
        title=stub.title,
        channel_name=stub.channel_name,
        description=stub.description,
        config=config,
    )
    return Candidate(
        platform="youtube",
        source_type="live" if live else "vod",
        source_id=stub.video_id,
        url=youtube_url(stub.video_id),
        title=stub.title,
        channel_id=stub.channel_id,
        channel_handle=stub.channel_name or stub.channel_id,
        channel_name=stub.channel_name,
        origin=stub.origin,
        language=language,
        language_signal=signal,
        viewers=stub.viewers if live else None,
        views=None if live else stub.views,
        likes=stub.likes,
        comments=stub.comments,
        published_at=stub.published_at,
        duration_s=None if live else stub.duration_s,
        has_chat=True if live else stub.live_status == "was_live",
        followers=stub.followers,
    )


def enrich_youtube(
    client: YouTubeClient | None,
    stubs: dict[str, Stub],
    config: DiscoveryConfig,
    now: datetime,
    units_used_today: Callable[[], float] = store.youtube_units_today,
) -> tuple[list[Candidate], SourceReport, int, int]:
    """Turn stubs into candidates with one batched `videos.list`, inside the daily quota budget. Without
    a key, over budget or on an API error the listing data alone is used. Returns the candidates, the
    report, the quota units spent and the number of off-topic videos dropped."""
    if not stubs:
        return [], SourceReport("youtube_api", "skipped", 0, "nothing to look up"), 0, 0
    fallback = [
        candidate for stub in stubs.values() if (candidate := candidate_from_stub(stub, config)) is not None
    ]
    if client is None or not client.is_configured():
        return fallback, SourceReport("youtube_api", "not_configured", 0, "YOUTUBE_API_KEY is not set"), 0, 0
    needed = quota_needed(len(stubs))
    used = units_used_today()
    if used + needed > config.youtube_quota_daily_units:
        detail = f"daily quota budget reached ({used:.0f} of {config.youtube_quota_daily_units} units)"
        _log.warning("discovery_quota_budget_reached", used=used, budget=config.youtube_quota_daily_units)
        return fallback, SourceReport("youtube_api", "skipped", 0, detail), 0, 0
    try:
        items = client.videos_list(list(stubs), parts=VIDEO_PARTS)
    except ClientError as exc:
        _log.warning("discovery_videos_list_failed", error=str(exc)[:ERROR_CHARS])
        return fallback, SourceReport("youtube_api", "error", 0, str(exc)[:ERROR_CHARS]), 0, 0
    found = []
    off_topic = 0
    for item in items:
        stub = stubs.get(str(item.get("id")))
        candidate = candidate_from_api(item, stub, now, config)
        if candidate is None:
            continue
        if on_topic(item, stub, config):
            found.append(candidate)
        else:
            off_topic += 1
    detail = f"{needed} quota units" + (f", {off_topic} off topic" if off_topic else "")
    return found, SourceReport("youtube_api", "ok", len(found), detail), needed, off_topic


def game_terms(config: DiscoveryConfig) -> tuple[str, ...]:
    """The words that identify the game in a title: the search terms and the game's full name."""
    return (*config.search_terms, config.target_game)


def on_topic(item: dict[str, Any], stub: Stub | None, config: DiscoveryConfig) -> bool:
    """Is this video about the target game? YouTube pads a search with unrelated videos when it has few
    matches (seen live: rain sounds and a horror game in a "GTA" live search), so a result is kept only
    when its title, tags or description name the game, or when it is at least in the Gaming category
    (role-play streams often do not name the game anywhere). A video found through a channel feed was
    not matched by any search and must name the game."""
    snippet = item.get("snippet") or {}
    tags = snippet.get("tags") or []
    text = " ".join(
        [str(snippet.get("title") or ""), *(str(tag) for tag in tags), str(snippet.get("description") or "")]
    )
    if scoring.matches_terms(text, game_terms(config)):
        return True
    if stub is not None and stub.needs_title_match:
        return False
    return str(snippet.get("categoryId") or "") == CATEGORY_GAMING


def keep_youtube(candidate: Candidate, stub: Stub | None, config: DiscoveryConfig) -> bool:
    """Listing-only check (used when the Data API gave nothing): a feed entry is kept only when its title
    names the game; search results are trusted."""
    if stub is None or not stub.needs_title_match:
        return True
    return scoring.matches_terms(candidate.title, game_terms(config))


def youtube_candidates(
    *,
    youtube: YouTubeClient | None,
    search: YtDlpSearch | None,
    apify: ApifyClient | None,
    config: DiscoveryConfig,
    now: datetime,
    gathered: Gathered,
    source_health: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Run the whole YouTube route and add its candidates and reports to `gathered`. `source_health` is
    the stored health state (`health.load()`): it lets the yt-dlp search back off after repeated failures."""
    stubs: dict[str, Stub] = {}
    primary_working = False
    with ThreadPoolExecutor(max_workers=config.ytdlp_workers, thread_name_prefix="discovery") as pool:
        if search is not None and config.search_terms:
            gate = health.gate(source_health or {}, health.YTDLP_SOURCE, now, search.access)
            found, report, searches = search_youtube(search, config, pool, gate)
            gathered.searches += searches
            gathered.reports.append(report)
            if searches:
                usage.record("ytdlp", "youtube_search", units=searches)
            primary_working = report.status == "ok"
            for stub in found:
                _merge(stubs, stub)
        # The official search: by default only when the yt-dlp search gave nothing in this pass.
        try:
            found, api_report, calls, note = search_api_step(youtube, config, now, primary_working)
        except Exception as exc:  # a broken state row or database hiccup must not lose the pass
            found, calls, note = [], 0, None
            api_report = SourceReport(SEARCH_API_SOURCE, "error", 0, str(exc)[:ERROR_CHARS])
        gathered.search_api_calls += calls
        if api_report is not None:
            gathered.reports.append(api_report)
        if note:
            gathered.notes.append(note)
        for stub in found:
            _merge(stubs, stub)
        if youtube is not None:
            try:
                channels = rss_channels(config.rss_max_channels_per_run)
                found, report = read_feeds(youtube, channels, config, now, pool)
            except Exception as exc:
                found, report = [], SourceReport("youtube_rss", "error", 0, str(exc)[:200])
            gathered.reports.append(report)
            for stub in found:
                _merge(stubs, stub)
    if apify is not None and apify.is_configured():
        try:
            found, report, cost = apify_step(apify, config, now)
        except ClientError as exc:
            found, report, cost = [], SourceReport("apify", "error", 0, str(exc)[:200]), 0.0
        gathered.apify_cost_usd += cost
        for stub in found:
            _merge(stubs, stub)
    else:
        report = SourceReport("apify", "not_configured", 0, "APIFY_TOKEN is not set")
    gathered.reports.append(report)
    # Streams seen live before get a second look (only the Data API can give it).
    if config.live_recheck_max > 0 and youtube is not None and youtube.is_configured():
        try:
            for stub in known_live_stubs(config, now):
                _merge(stubs, stub)
        except Exception as exc:
            _log.warning("discovery_live_recheck_failed", error=type(exc).__name__)
    # Videos already queued need no second look (a stream that is live now is never in `videos`).
    finished = [("youtube", stub.video_id) for stub in stubs.values() if stub.live_status != "is_live"]
    known = store.known_videos(finished)
    for _platform, video_id in known:
        stubs.pop(video_id, None)
    gathered.skip("already_queued", len(known))
    candidates, report, units, off_topic = enrich_youtube(youtube, stubs, config, now)
    gathered.youtube_quota_units += units
    gathered.reports.append(report)
    if report.status != "ok":  # listing data only: apply the title check to feed entries
        kept = [c for c in candidates if keep_youtube(c, stubs.get(c.source_id), config)]
        off_topic = len(candidates) - len(kept)
        candidates = kept
    gathered.skip("off_topic", off_topic)
    gathered.candidates.extend(candidates)

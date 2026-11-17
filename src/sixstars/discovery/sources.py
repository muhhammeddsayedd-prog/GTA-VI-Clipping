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
   `search.list` (100 units per call) is never used.
4. Apify: one small run every `discovery.apify_interval_s`, alternating live and recent, started in one
   pass and read in a later one, behind a daily and a monthly spend guard. Discovery works without it.

Data API fields used (checked against real responses on 2026-11-17):
https://developers.google.com/youtube/v3/docs/videos#resource : `snippet.publishedAt`, `snippet.channelId`,
`snippet.channelTitle`, `snippet.liveBroadcastContent` (`live` | `upcoming` | `none`),
`snippet.defaultAudioLanguage`, `contentDetails.duration` (ISO 8601, `P0D` while live),
`statistics.viewCount|likeCount|commentCount` (strings), `liveStreamingDetails.actualStartTime|
actualEndTime|concurrentViewers` (absent for a normal upload).
"""

import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sixstars import db, settings, usage
from sixstars.clients._http import ClientError
from sixstars.clients.apify import ApifyClient, build_youtube_search_input, normalise_youtube_item
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.youtube import CATEGORY_GAMING, IDS_PER_REQUEST_MAX, YouTubeClient
from sixstars.config import env
from sixstars.discovery import scoring, store
from sixstars.discovery.config import DiscoveryConfig
from sixstars.discovery.models import FORBIDDEN_TIERS, Candidate
from sixstars.discovery.ytsearch import FlatVideo, YtDlpSearch
from sixstars.logging import get_logger

TWITCH_GAME_CACHE_S: Final = 24 * 3600
APIFY_GIVE_UP_S: Final = 3600  # a run still unfinished after this is forgotten
APIFY_STATE_KEY: Final = "discovery.apify_last_run"
RSS_CURSOR_KEY: Final = "discovery.rss_cursor"
TWITCH_GAME_KEY: Final = "discovery.twitch_game_cache"
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
    search: YtDlpSearch, config: DiscoveryConfig, pool: ThreadPoolExecutor
) -> tuple[list[Stub], SourceReport, int]:
    """Run the yt-dlp searches of one pass in parallel: per term one live search and one recent-video
    search per sort order. Returns the stubs, the report and the number of searches made."""
    tasks: list[tuple[str, bool, str]] = []
    for term in config.search_terms:
        tasks.append((term, True, "relevance"))
        tasks.extend((term, False, sort) for sort in config.youtube_recent_sorts)

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

    results = list(pool.map(one, tasks))
    stubs: list[Stub] = []
    failures = 0
    for result in results:
        if isinstance(result, Exception):
            failures += 1
            continue
        stubs.extend(_from_flat(video) for video in result)
    if tasks and failures == len(tasks):
        first = next(result for result in results if isinstance(result, Exception))
        return [], SourceReport("youtube_search", "error", 0, str(first)[:200]), len(tasks)
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


def candidate_from_api(item: dict[str, Any], stub: Stub | None, now: datetime) -> Candidate | None:
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
    language = short_language(snippet.get("defaultAudioLanguage")) or short_language(
        snippet.get("defaultLanguage")
    )
    language = language or scoring.title_language(title)
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


def candidate_from_stub(stub: Stub) -> Candidate | None:
    """Build a candidate from the listing alone (used when the Data API is off budget or failing).
    Upcoming broadcasts and rows without a channel id give None."""
    if not stub.channel_id or stub.live_status == "is_upcoming":
        return None
    live = stub.live_status == "is_live"
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
        language=scoring.title_language(stub.title),
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
    fallback = [candidate for stub in stubs.values() if (candidate := candidate_from_stub(stub)) is not None]
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
        _log.warning("discovery_videos_list_failed", error=str(exc)[:200])
        return fallback, SourceReport("youtube_api", "error", 0, str(exc)[:200]), 0, 0
    found = []
    off_topic = 0
    for item in items:
        stub = stubs.get(str(item.get("id")))
        candidate = candidate_from_api(item, stub, now)
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
) -> None:
    """Run the whole YouTube route and add its candidates and reports to `gathered`."""
    stubs: dict[str, Stub] = {}
    with ThreadPoolExecutor(max_workers=config.ytdlp_workers, thread_name_prefix="discovery") as pool:
        if search is not None and config.search_terms:
            found, report, searches = search_youtube(search, config, pool)
            gathered.searches += searches
            gathered.reports.append(report)
            usage.record("ytdlp", "youtube_search", units=searches)
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

"""Orchestrates one discovery pass end to end: sources, scoring, tiers and assignment, writing results to the
database. Implemented in M09.

`run_discovery()` is what `POST /v1/discovery/run` (M11) and `sixstars discovery run-once` call. One pass:

1. gather candidates from every configured source (a failing or unconfigured source is skipped);
2. drop blocked titles, short or old videos, and everything from forbidden creators;
3. upsert creators, fetch missing follower counts, score every candidate;
4. run permission checks (P6) for the creators about to be picked, queue `creator.permission` jobs for
   other new or stale creators;
5. store every observation in `candidates`;
6. plan live slots and queue videos for the VOD lane;
7. heartbeat `trigger:discovery`, event `discovery.run`, return the summary.

Discovery never posts anything, so `control.emergency_stop` and `launch.dry_run` do not change it.
"""

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sixstars import db, events, notify
from sixstars.chat.net import proxy_for
from sixstars.clients._http import ClientError
from sixstars.clients.anthropic_client import AnthropicClient
from sixstars.clients.apify import ApifyClient
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.discovery import assign, scoring, sources, store, tiers
from sixstars.discovery.config import DiscoveryConfig
from sixstars.discovery.models import Candidate, Creator, Scored
from sixstars.discovery.sources import Gathered, SourceReport
from sixstars.discovery.tiers import ChannelInfo
from sixstars.discovery.ytsearch import YtDlpSearch
from sixstars.logging import get_logger

HEARTBEAT: Final = "trigger:discovery"
TOP_N: Final = 10
PRELIMINARY_FACTOR: Final = 2  # permission checks look this many times deeper than the slots to fill

_log = get_logger("discovery")


@dataclass
class DiscoveryClients:
    """The external clients of one pass. Tests pass fakes; `default()` builds the real ones."""

    twitch: TwitchClient | None = None
    youtube: YouTubeClient | None = None
    apify: ApifyClient | None = None
    search: YtDlpSearch | None = None
    anthropic: AnthropicClient | None = None  # None: `ai.calls` builds its own on first use

    @classmethod
    def default(cls, config: DiscoveryConfig) -> "DiscoveryClients":
        return cls(
            twitch=TwitchClient(),
            youtube=YouTubeClient(),
            apify=ApifyClient(),
            search=YtDlpSearch(proxy=proxy_for("youtube"), timeout_s=config.ytdlp_timeout_s),
        )


@dataclass
class DiscoverySummary:
    """Result of one pass. `to_dict()` is the JSON body of `POST /v1/discovery/run`."""

    started_at: datetime
    target_game: str
    duration_s: float = 0.0
    sources: list[dict[str, Any]] = field(default_factory=list)
    live_seen: int = 0
    videos_seen: int = 0
    creators_seen: int = 0
    new_creators: int = 0
    candidates_stored: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    permission_checked: int = 0
    permission_jobs: int = 0
    sessions_assigned: int = 0
    sessions_released: int = 0
    sessions_active: int = 0
    videos_queued: int = 0
    vod_cutoff: float = 0.0
    vod_max: int = 0
    vod_widened: bool = False
    youtube_searches: int = 0
    youtube_quota_units: int = 0
    youtube_quota_units_today: float = 0.0
    apify_cost_usd: float = 0.0
    apify_spent_today_usd: float = 0.0
    apify_spent_month_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    timings_s: dict[str, float] = field(default_factory=dict)
    top_live: list[dict[str, Any]] = field(default_factory=list)
    top_videos: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        data = asdict(self)
        data["started_at"] = self.started_at.isoformat()
        return data

    def counts(self) -> dict[str, Any]:
        """The numbers stored on the `discovery.run` event (no candidate lists)."""
        data = self.to_dict()
        data.pop("top_live")
        data.pop("top_videos")
        return data


# ── pure helpers ─────────────────────────────────────────────


def filter_candidates(
    candidates: Sequence[Candidate], config: DiscoveryConfig, now: datetime
) -> tuple[list[Candidate], dict[str, int]]:
    """Drop duplicates, blocked titles, videos that are too short and videos older than the look-back.
    Returns the kept candidates and how many were dropped per reason."""
    kept: dict[tuple[str, str], Candidate] = {}
    skipped: dict[str, int] = {}
    oldest = now - timedelta(hours=config.vod_lookback_h)

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for candidate in candidates:
        if candidate.key in kept:
            continue
        if scoring.title_blocked(candidate.title, config.title_blocklist):
            skip("title_blocklist")
        elif candidate.source_type == "vod" and (
            candidate.duration_s is not None and candidate.duration_s < config.min_vod_duration_s
        ):
            skip("too_short")
        elif (
            candidate.source_type == "vod"
            and candidate.published_at is not None
            and candidate.published_at < oldest
        ):
            skip("too_old")
        else:
            kept[candidate.key] = candidate
    return list(kept.values()), skipped


@dataclass(frozen=True)
class History:
    """What the database knows that scoring needs."""

    viewers_then: dict[tuple[str, str], int]
    peak_viewers: dict[tuple[str, str], int]
    chat_rates: dict[str, float]
    clip_views: list[float]


def score_candidate(
    candidate: Candidate, creator: Creator, history: History, config: DiscoveryConfig, now: datetime
) -> Scored:
    """Score one candidate for its creator (pure)."""
    past = scoring.history_part(creator.avg_clip_views, history.clip_views, creator.claims_count)
    language = candidate.language or creator.language
    preferred = creator.tier == "preferred"
    measured = history.chat_rates.get(creator.id)
    if candidate.source_type == "live":
        score, parts = scoring.score_live(
            viewers=candidate.viewers,
            viewers_then=history.viewers_then.get(candidate.key),
            measured_chat_mpm=measured,
            language=language,
            audience_spread=creator.audience_spread,
            history=past,
            preferred=preferred,
            config=config.score,
        )
        return Scored(candidate, creator, score, parts)
    began = candidate.started_at or candidate.published_at
    age_hours = float(config.vod_lookback_h) if began is None else (now - began).total_seconds() / 3600.0
    followers = candidate.followers if candidate.followers is not None else creator.follower_count
    score, parts, vph = scoring.score_vod(
        views=candidate.views,
        age_hours=age_hours,
        likes=candidate.likes,
        comments=candidate.comments,
        followers=followers,
        duration_s=candidate.duration_s,
        has_chat=bool(candidate.has_chat),
        peak_viewers=history.peak_viewers.get(candidate.key),
        measured_chat_mpm=measured,
        language=language,
        audience_spread=creator.audience_spread,
        history=past,
        preferred=preferred,
        config=config.score,
    )
    return Scored(candidate, creator, score, parts, vph)


def top_rows(
    items: Sequence[Scored], picked: set[tuple[str, str]], limit: int = TOP_N
) -> list[dict[str, Any]]:
    """The best candidates as plain dicts for the summary and the CLI."""
    rows = []
    for item in assign.rank(items)[:limit]:
        candidate = item.candidate
        rows.append(
            {
                "score": item.score,
                "title": candidate.title,
                "channel": candidate.channel_name or candidate.channel_handle,
                "platform": candidate.platform,
                "viewers": candidate.viewers,
                "views": candidate.views,
                "views_per_hour": item.views_per_hour,
                "duration_s": candidate.duration_s,
                "language": candidate.language,
                "has_chat": candidate.has_chat,
                "tier": item.creator.tier,
                "checked": item.creator.description_checked_at is not None,
                "picked": item.key in picked,
                "url": candidate.url,
                "parts": item.parts,
            }
        )
    return rows


# ── steps ────────────────────────────────────────────────────


def gather(clients: DiscoveryClients, config: DiscoveryConfig, now: datetime) -> Gathered:
    """Ask every source. Twitch is skipped with one info log while its keys are absent (decision D100)."""
    gathered = Gathered()
    twitch = clients.twitch
    if twitch is None or not twitch.is_configured():
        _log.info("discovery_twitch_skipped", reason="TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set")
        gathered.reports.append(SourceReport("twitch", "not_configured", 0, "no Twitch keys"))
    else:
        try:
            found = sources.twitch_candidates(twitch, config, now)
            gathered.candidates.extend(found)
            gathered.reports.append(SourceReport("twitch", "ok" if found else "empty", len(found)))
        except ClientError as exc:
            _log.warning("discovery_twitch_failed", error=str(exc)[:200])
            gathered.reports.append(SourceReport("twitch", "error", 0, str(exc)[:200]))
    sources.youtube_candidates(
        youtube=clients.youtube,
        search=clients.search,
        apify=clients.apify,
        config=config,
        now=now,
        gathered=gathered,
    )
    return gathered


def fill_followers(
    creators: dict[tuple[str, str], Creator],
    candidates: Sequence[Candidate],
    clients: DiscoveryClients,
    config: DiscoveryConfig,
    gathered: Gathered,
) -> dict[tuple[str, str], ChannelInfo]:
    """Fetch follower counts for creators that have none (YouTube: one `channels.list` per 50 channels,
    inside the quota budget; Twitch: a few lookups per pass, biggest streams first). Updates `creators` in
    place and returns the channel texts fetched on the way (reused by the permission checks)."""
    info: dict[tuple[str, str], ChannelInfo] = {}
    wanted = [c for c in creators.values() if c.follower_count is None and not c.forbidden]
    youtube = clients.youtube
    yt_missing = [c for c in wanted if c.platform == "youtube"]
    if yt_missing and youtube is not None and youtube.is_configured():
        needed = sources.quota_needed(len(yt_missing))
        if store.youtube_units_today() + needed <= config.youtube_quota_daily_units:
            try:
                fetched = tiers.youtube_channel_info(youtube, [c.channel_id for c in yt_missing])
                gathered.youtube_quota_units += needed
                tiers.save_channel_facts(fetched, yt_missing)
                for creator in yt_missing:
                    channel = fetched.get(creator.channel_id)
                    if channel is not None:
                        info[creator.key] = channel
                        creators[creator.key] = replace(creator, follower_count=channel.followers)
            except ClientError as exc:
                _log.warning("discovery_channels_list_failed", error=str(exc)[:200])
    twitch = clients.twitch
    tw_missing = [c for c in wanted if c.platform == "twitch"]
    if tw_missing and twitch is not None and twitch.is_configured():
        audience: dict[tuple[str, str], int] = {}
        for candidate in candidates:
            size = candidate.viewers or candidate.views or 0
            audience[candidate.channel_key] = max(audience.get(candidate.channel_key, 0), size)
        tw_missing.sort(key=lambda c: -audience.get(c.key, 0))
        facts: dict[str, ChannelInfo] = {}
        try:
            for creator in tw_missing[: config.twitch_follower_lookups_per_run]:
                total = twitch.get_followers_total(creator.channel_id)
                if total is not None:
                    facts[creator.channel_id] = ChannelInfo(name="", description="", followers=total)
                    creators[creator.key] = replace(creator, follower_count=total)
        except ClientError as exc:
            _log.warning("discovery_twitch_followers_failed", error=str(exc)[:200])
        tiers.save_channel_facts(facts, tw_missing)
    return info


def load_history(
    scored_input: Sequence[tuple[Candidate, Creator]], config: DiscoveryConfig, now: datetime
) -> History:
    live = [candidate.key for candidate, _ in scored_input if candidate.source_type == "live"]
    past_streams = [
        candidate.key
        for candidate, _ in scored_input
        if candidate.source_type == "vod" and candidate.has_chat
    ]
    return History(
        viewers_then=store.viewers_before(live, now, config.growth_window_s, config.min_observation_gap_s),
        peak_viewers=store.peak_viewers(past_streams),
        chat_rates=store.chat_rates({creator.id for _, creator in scored_input}),
        clip_views=store.clip_view_population(),
    )


def check_permissions(
    scored: list[Scored],
    clients: DiscoveryClients,
    config: DiscoveryConfig,
    now: datetime,
    info: dict[tuple[str, str], ChannelInfo],
    vod_cutoff: float,
    vod_max: int,
) -> tuple[dict[str, Creator], int]:
    """Check the creators about to be picked inside the pass and queue jobs for the other creators that
    need a check. Returns the creators whose row changed (by id) and the number of jobs queued."""
    due: dict[str, Creator] = {}
    for item in assign.rank(scored):
        if tiers.needs_check(item.creator, now, config.permission_recheck_days):
            due.setdefault(item.creator.id, item.creator)
    if not due:
        return {}, 0
    live = [
        i
        for i in assign.rank(scored)
        if i.candidate.source_type == "live" and i.score >= config.live_score_cutoff
    ]
    vods = [i for i in assign.rank(scored) if i.candidate.source_type == "vod" and i.score >= vod_cutoff]
    near = live[: config.live_max_slots * PRELIMINARY_FACTOR] + vods[: vod_max * PRELIMINARY_FACTOR]
    urgent: dict[str, Creator] = {}
    for item in near:  # live first: a slot is worth more the sooner it is taken
        if item.creator.id in due and item.creator.description_checked_at is None:
            urgent.setdefault(item.creator.id, item.creator)
    now_list = list(urgent.values())[: config.permission_checks_per_run]
    checked = (
        tiers.check_creators(
            now_list,
            youtube=clients.youtube,
            twitch=clients.twitch,
            anthropic=clients.anthropic,
            now=now,
            known_info=info,
        )
        if now_list
        else {}
    )
    queued = 0
    with db.transaction():
        for creator_id in due:
            if queued >= config.permission_jobs_per_run:
                break
            if creator_id in checked:
                continue
            if tiers.enqueue_permission_job(creator_id) is not None:
                queued += 1
    return checked, queued


def assign_live(
    scored: Sequence[Scored], config: DiscoveryConfig, now: datetime, summary: DiscoverySummary
) -> set[tuple[str, str]]:
    """Plan and write the live slots; one event per assignment and release. Returns the assigned sources."""
    live = [item for item in scored if item.candidate.source_type == "live"]
    sessions = store.active_sessions()
    plan = assign.plan_slots(
        sessions,
        live,
        cutoff=config.live_score_cutoff,
        release_after_s=config.release_after_below_s,
        max_slots=config.live_max_slots,
        min_viewers=config.min_live_viewers,
        now=now,
        allowed=lambda creator: tiers.clip_allowed(creator, config.require_permission_check),
    )
    assigned, released = store.apply_slot_plan(plan, now)
    reasons = {release.session_id: release.reason for release in plan.release}
    for session_id in released:
        events.emit(
            "live.released",
            entity_type="live_session",
            entity_id=session_id,
            message=f"live slot released ({reasons.get(session_id, '')})",
            data={"reason": reasons.get(session_id)},
        )
    for session_id, item in assigned:
        events.emit(
            "live.assigned",
            entity_type="live_session",
            entity_id=session_id,
            message=f"live slot for {item.candidate.platform} {item.candidate.channel_name}",
            data={
                "source_id": item.candidate.source_id,
                "score": item.score,
                "viewers": item.candidate.viewers,
            },
        )
    summary.sessions_assigned = len(assigned)
    summary.sessions_released = len(released)
    summary.sessions_active = len(plan.keep) + len(assigned)
    return {item.key for _, item in assigned}


def queue_videos(
    scored: Sequence[Scored],
    config: DiscoveryConfig,
    vod_cutoff: float,
    vod_max: int,
    summary: DiscoverySummary,
) -> set[tuple[str, str]]:
    """Insert `videos` rows and `vod.process` jobs for the best new videos. Returns the queued sources."""
    vods = [item for item in scored if item.candidate.source_type == "vod"]
    keys = [item.key for item in vods]
    known = store.known_videos(keys) | store.live_lane_owned(keys)
    picks = assign.pick_videos(
        vods,
        cutoff=vod_cutoff,
        max_total=vod_max,
        max_per_creator=config.max_vod_per_creator_per_run,
        known=known,
        allowed=lambda creator: tiers.clip_allowed(creator, config.require_permission_check),
    )
    queued: set[tuple[str, str]] = set()
    for item in picks:
        video_id = store.queue_video(item)
        if video_id is None:
            continue
        queued.add(item.key)
        events.emit(
            "video.queued",
            entity_type="video",
            entity_id=video_id,
            message=f"{item.candidate.platform} {item.candidate.channel_name}: {item.candidate.title}"[:300],
            data={
                "source_id": item.candidate.source_id,
                "score": item.score,
                "has_chat": item.candidate.has_chat,
            },
        )
    summary.videos_queued = len(queued)
    return queued


# ── the pass ─────────────────────────────────────────────────


def _run(clients: DiscoveryClients | None, now: datetime) -> DiscoverySummary:
    clock = time.monotonic()
    config = DiscoveryConfig.from_settings()
    clients = clients or DiscoveryClients.default(config)
    summary = DiscoverySummary(started_at=now, target_game=config.target_game)
    lap = clock

    def timed(step: str) -> None:
        nonlocal lap
        summary.timings_s[step] = round(time.monotonic() - lap, 2)
        lap = time.monotonic()

    gathered = gather(clients, config, now)
    timed("sources")
    candidates, skipped = filter_candidates(gathered.candidates, config, now)
    for reason, count in skipped.items():
        gathered.skip(reason, count)

    creators = tiers.upsert_creators(candidates, config.excluded_channels)
    info = fill_followers(creators, candidates, clients, config, gathered)
    pairs = [(c, creators[c.channel_key]) for c in candidates if c.channel_key in creators]
    forbidden = sum(1 for _, creator in pairs if creator.forbidden)
    gathered.skip("forbidden_creator", forbidden)
    pairs = [(candidate, creator) for candidate, creator in pairs if not creator.forbidden]

    history = load_history(pairs, config, now)
    scored = [score_candidate(candidate, creator, history, config, now) for candidate, creator in pairs]
    timed("creators_and_history")

    vod_cutoff, vod_max, widened = assign.widen_for_low_queue(
        config.vod_score_cutoff,
        config.max_vod_per_run,
        store.approved_hourly_clips(),
        config.queue_low_watermark,
    )
    # Never pile up more work than the VOD lane can take: stop at `discovery.max_vod_backlog` waiting.
    vod_max = max(0, min(vod_max, config.max_vod_backlog - store.videos_waiting()))
    checked, jobs_queued = check_permissions(scored, clients, config, now, info, vod_cutoff, vod_max)
    if checked:
        rescored = []
        for item in scored:
            creator = checked.get(item.creator.id)
            if creator is None:
                rescored.append(item)
            elif creator.forbidden:
                gathered.skip("forbidden_creator")
            else:
                rescored.append(score_candidate(item.candidate, creator, history, config, now))
        scored = rescored
    timed("permissions")

    with db.transaction():  # one connection, and a pass is stored whole or not at all
        summary.candidates_stored = store.record_candidates(scored, now, config.min_observation_gap_s)
        picked = assign_live(scored, config, now, summary)
        picked |= queue_videos(scored, config, vod_cutoff, vod_max, summary)
        store.mark_picked(picked, now)
    timed("store_and_assign")

    live = [item for item in scored if item.candidate.source_type == "live"]
    vods = [item for item in scored if item.candidate.source_type == "vod"]
    summary.sources = [report.as_dict() for report in gathered.reports]
    summary.errors = [f"{r.name}: {r.detail}" for r in gathered.reports if r.status == "error"]
    summary.live_seen = len(live)
    summary.videos_seen = len(vods)
    summary.creators_seen = len(creators)
    summary.new_creators = sum(1 for creator in creators.values() if creator.is_new)
    summary.skipped = dict(gathered.skipped)
    summary.permission_checked = len(checked)
    summary.permission_jobs = jobs_queued
    summary.vod_cutoff, summary.vod_max, summary.vod_widened = vod_cutoff, vod_max, widened
    summary.youtube_searches = gathered.searches
    summary.youtube_quota_units = gathered.youtube_quota_units
    summary.youtube_quota_units_today = store.youtube_units_today()
    summary.apify_cost_usd = round(gathered.apify_cost_usd, 4)
    spent_day, spent_month = store.apify_spend()
    summary.apify_spent_today_usd, summary.apify_spent_month_usd = round(spent_day, 4), round(spent_month, 4)
    summary.top_live = top_rows(live, picked)
    summary.top_videos = top_rows(vods, picked)
    summary.duration_s = round(time.monotonic() - clock, 2)
    return summary


def run_discovery(clients: DiscoveryClients | None = None, now: datetime | None = None) -> DiscoverySummary:
    """Run one discovery pass and return its summary.

    `clients` and `now` exist for tests; production calls it with no arguments. A source that fails does
    not fail the pass. An unexpected error is recorded as a `discovery.failed` event and raised.
    """
    started = now or datetime.now(UTC)
    try:
        summary = _run(clients, started)
    except Exception as exc:
        _log.error("discovery_failed", error=type(exc).__name__, detail=str(exc)[:300])
        events.emit("discovery.failed", level="error", message=f"{type(exc).__name__}: {exc}"[:500])
        notify.send("warn", "Discovery pass failed", f"{type(exc).__name__}: {exc}"[:300])
        raise
    db.heartbeat(HEARTBEAT, {"live_seen": summary.live_seen, "videos_seen": summary.videos_seen})
    events.emit(
        "discovery.run",
        level="warn" if summary.errors else "info",
        message=(
            f"live {summary.live_seen}, videos {summary.videos_seen}, new creators {summary.new_creators}, "
            f"assigned {summary.sessions_assigned}, released {summary.sessions_released}, "
            f"queued {summary.videos_queued}"
        ),
        data=summary.counts(),
    )
    if summary.live_seen + summary.videos_seen == 0:
        notify.send(
            "warn",
            "Discovery found nothing",
            f"No live stream or video for {summary.target_game}. Sources: "
            + ", ".join(f"{s['name']}={s['status']}" for s in summary.sources),
        )
    _log.info(
        "discovery_run", **{k: v for k, v in summary.counts().items() if k not in ("sources", "skipped")}
    )
    return summary

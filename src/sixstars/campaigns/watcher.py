"""The campaign watcher (D118, PR 2). It asks each platform directly about the streamers of active
campaigns, by id, instead of hoping they show up in trending discovery (which reads only the top 300
GTA VI streams). Runs in the scheduler service every `campaigns.watch_interval_s` (60 s), so live
detection keeps working when n8n is down.

- Twitch: one Get Streams call by user id (up to 100) per pass. A live campaign stream of the campaign's
  game always gets a live slot: up to `live.campaign_slots` of them, taking the slot of the weakest
  trending session when the lane is full. A creator already recorded under trending is re-tagged, not
  restarted. When a campaign streamer without a slot goes offline, their archive is queued for the VOD
  lane at campaign priority.
- YouTube: the channel RSS feed every `campaigns.watch_youtube_s` (3 min) plus one `videos.list` for new
  or live ids. YouTube refuses live recording from the server, so a YouTube campaign stream is taken from
  its VOD when it ends, at campaign priority.
- Every streamer's state goes to `creator_live_status`; the pass beats `campaign:watcher`.

`plan` is pure; `run_once` does the I/O.
"""

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sixstars import db, events, settings
from sixstars.clients._http import ClientError
from sixstars.clients.twitch import TwitchClient, TwitchStream
from sixstars.clients.youtube import YouTubeClient
from sixstars.discovery import sources
from sixstars.discovery import store as dstore
from sixstars.discovery.models import Candidate, Scored
from sixstars.logging import get_logger

HEARTBEAT: Final = "campaign:watcher"
ACTIVE_SESSION_STATUSES: Final = ("assigned", "recording")
DEFAULTS: Final[dict[str, Any]] = {
    "campaigns.watch_interval_s": 60,
    "campaigns.watch_youtube_s": 180,
    "campaigns.feed_lookback_h": 48,
    "campaigns.vod_priority": 35,
    "live.campaign_slots": 4,
}
YOUTUBE_PARTS: Final = ("snippet", "liveStreamingDetails", "contentDetails", "statistics")
ARCHIVE_MAX_AGE: Final = timedelta(hours=24)

_log = get_logger("campaigns")
_feed_checked: dict[str, float] = {}  # YouTube channel id -> monotonic time of the last feed read
_settled: set[str] = set()  # YouTube video ids that are neither live nor a stream to queue


@dataclass(frozen=True)
class Watched:
    """A streamer of an active campaign (the highest-priority campaign when they are in several)."""

    creator_id: str
    platform: str
    channel_id: str
    handle: str
    campaign_id: str
    slug: str
    priority: int
    rate_per_1k: float
    budget_left: float | None
    required_game: str | None


@dataclass(frozen=True)
class LiveNow:
    """A campaign streamer live right now."""

    creator_id: str
    campaign_id: str
    platform: str
    stream_id: str
    login: str
    url: str
    game: str
    title: str
    viewers: int
    game_ok: bool
    value: float  # expected value: rate x budget left x viewers (ranks streams when slots are short)


@dataclass(frozen=True)
class ActiveSession:
    """A `live_sessions` row that holds a slot."""

    id: str
    creator_id: str
    campaign_id: str | None
    trend_score: float | None


@dataclass
class WatchPlan:
    start: list[LiveNow] = field(default_factory=list)
    retag: list[tuple[str, str]] = field(default_factory=list)  # (session id, campaign id)
    untag: list[str] = field(default_factory=list)  # session ids that fall back to trending rules
    preempt: list[str] = field(default_factory=list)  # trending sessions released for a campaign stream
    overflow: list[LiveNow] = field(default_factory=list)  # live, but no slot: taken from the VOD


# ── pure rules ───────────────────────────────────────────────


def game_ok(required: str | None, game: str) -> bool:
    """A campaign with no required game takes any game; otherwise the names match without case."""
    return required is None or required.strip().casefold() == game.strip().casefold()


def expected_value(rate_per_1k: float, budget_left: float | None, viewers: int) -> float:
    budget = 1.0 if budget_left is None else max(budget_left, 0.0)
    return rate_per_1k * budget * max(viewers, 1)


def pick_campaigns(rows: Iterable[Mapping[str, Any]]) -> list[Watched]:
    """One `Watched` per creator: the first row per creator wins (rows come best priority first)."""
    best: dict[str, Watched] = {}
    for row in rows:
        creator_id = str(row["creator_id"])
        if creator_id in best:
            continue
        best[creator_id] = Watched(
            creator_id=creator_id,
            platform=str(row["platform"]),
            channel_id=str(row["channel_id"]),
            handle=str(row["handle"]),
            campaign_id=str(row["campaign_id"]),
            slug=str(row["slug"]),
            priority=int(row["priority"]),
            rate_per_1k=float(row["rate_per_1k"]),
            budget_left=None if row["budget_left"] is None else float(row["budget_left"]),
            required_game=row["required_game"],
        )
    return list(best.values())


def live_from_twitch(streams: Sequence[TwitchStream], watched: Mapping[str, Watched]) -> list[LiveNow]:
    """`watched` is keyed by Twitch user id."""
    found: list[LiveNow] = []
    for stream in streams:
        who = watched.get(stream.user_id)
        if who is None:
            continue
        found.append(
            LiveNow(
                creator_id=who.creator_id,
                campaign_id=who.campaign_id,
                platform="twitch",
                stream_id=stream.id,
                login=stream.user_login,
                url=f"https://www.twitch.tv/{stream.user_login}",
                game=stream.game_name,
                title=stream.title,
                viewers=stream.viewer_count,
                game_ok=game_ok(who.required_game, stream.game_name),
                value=expected_value(who.rate_per_1k, who.budget_left, stream.viewer_count),
            )
        )
    return found


def plan(
    live: Sequence[LiveNow],
    sessions: Sequence[ActiveSession],
    watched_campaign: Mapping[str, str],
    *,
    max_slots: int,
    campaign_slots: int,
) -> WatchPlan:
    """Decide slot changes for this pass.

    `watched_campaign` maps creator id -> the campaign they are watched for now. A tagged session whose
    creator is no longer in an active campaign, or whose live stream left the campaign's game, is untagged
    (it falls back to trending rules); one whose winning campaign changed is re-tagged. Live campaign
    streams (right game, best expected value first) take a slot while fewer than `campaign_slots` are
    tagged; when all `max_slots` are busy, the trending session with the lowest score is released for it.
    Streams left without a slot are reported as overflow (their VOD is taken after the stream).
    """
    out = WatchPlan()
    live_by_creator = {item.creator_id: item for item in live}
    by_creator: dict[str, ActiveSession] = {}
    tagged = 0
    for session in sessions:
        by_creator.setdefault(session.creator_id, session)
        if session.campaign_id is None:
            continue
        current = watched_campaign.get(session.creator_id)
        stream = live_by_creator.get(session.creator_id)
        if current is None or (stream is not None and not stream.game_ok):
            out.untag.append(session.id)
            continue
        if current != session.campaign_id:
            out.retag.append((session.id, current))
        tagged += 1
    total = len(sessions)
    preemptable = sorted(
        (s for s in sessions if s.campaign_id is None),
        key=lambda s: (s.trend_score if s.trend_score is not None else -1.0, s.id),
    )
    for stream in sorted(live, key=lambda item: (-item.value, item.stream_id)):
        if not stream.game_ok:
            continue
        existing = by_creator.get(stream.creator_id)
        if existing is not None:
            if existing.campaign_id is None:
                out.retag.append((existing.id, stream.campaign_id))
                tagged += 1
                preemptable = [s for s in preemptable if s.id != existing.id]
            continue
        if tagged >= campaign_slots:
            out.overflow.append(stream)
            continue
        if total >= max_slots:
            if not preemptable:
                out.overflow.append(stream)
                continue
            victim = preemptable.pop(0)
            out.preempt.append(victim.id)
            total -= 1
        out.start.append(stream)
        total += 1
        tagged += 1
    return out


def went_offline(previous: Mapping[str, bool], now_live: set[str], creators: Iterable[str]) -> list[str]:
    """Creators who were live at the last pass and are not now."""
    return [creator for creator in creators if previous.get(creator) and creator not in now_live]


# ── database ─────────────────────────────────────────────────


def load_watched(now: datetime) -> list[Watched]:
    rows = db.fetch_all(
        """
        select c.id::text as creator_id, c.platform, c.channel_id, c.handle, k.id::text as campaign_id,
               k.slug, k.priority, k.rate_per_1k, k.budget_left, k.required_game
        from clip_campaigns k
        join clip_campaign_creators l on l.campaign_id = k.id
        join creators c on c.id = l.creator_id
        where k.status = 'active' and (k.ends_at is null or k.ends_at > %s)
          and (k.starts_at is null or k.starts_at <= %s)
          and c.tier not in ('restricted', 'blocked')
        order by k.priority desc, k.slug
        """,
        (now, now),
    )
    return pick_campaigns(rows)


def active_sessions() -> list[ActiveSession]:
    rows = db.fetch_all(
        "select id::text as id, creator_id::text as creator_id, campaign_id::text as campaign_id, "
        "trend_score "
        "from live_sessions where status = any(%s) order by assigned_at",
        (list(ACTIVE_SESSION_STATUSES),),
    )
    return [
        ActiveSession(
            row["id"],
            row["creator_id"],
            row["campaign_id"],
            None if row["trend_score"] is None else float(row["trend_score"]),
        )
        for row in rows
    ]


def previous_live() -> dict[str, bool]:
    return {
        row["creator_id"]: bool(row["live"])
        for row in db.fetch_all("select creator_id::text as creator_id, live from creator_live_status")
    }


def save_status(creator_id: str, stream: LiveNow | None, now: datetime) -> None:
    db.execute(
        """
        insert into creator_live_status
          (creator_id, live, stream_id, game, title, viewers, started_at, checked_at)
        values (%s, %s, %s, %s, %s, %s, case when %s then %s end, %s)
        on conflict (creator_id) do update set
          live = excluded.live, stream_id = excluded.stream_id, game = excluded.game, title = excluded.title,
          viewers = excluded.viewers,
          started_at = case when excluded.live and not creator_live_status.live then excluded.started_at
                            when excluded.live then creator_live_status.started_at end,
          checked_at = excluded.checked_at
        """,
        (
            creator_id,
            stream is not None,
            stream.stream_id if stream else None,
            stream.game if stream else None,
            stream.title if stream else None,
            stream.viewers if stream else None,
            stream is not None,
            now,
            now,
        ),
    )


def apply(out: WatchPlan, now: datetime) -> list[str]:
    """Write the plan in one transaction. Returns the ids of the sessions started."""
    started: list[str] = []
    with db.transaction():
        for session_id in out.untag:
            db.execute(
                "update live_sessions set campaign_id = null, updated_at = %s where id = %s",
                (now, session_id),
            )
        for session_id, campaign_id in out.retag:
            db.execute(
                "update live_sessions set campaign_id = %s, below_cutoff_since = null, updated_at = %s "
                "where id = %s",
                (campaign_id, now, session_id),
            )
        for session_id in out.preempt:
            db.execute(
                "update live_sessions set status = 'released', updated_at = %s "
                "where id = %s and campaign_id is null and status = any(%s)",
                (now, session_id, list(ACTIVE_SESSION_STATUSES)),
            )
        for stream in out.start:
            row = db.fetch_one(
                """
                insert into live_sessions
                  (creator_id, platform, source_id, channel_login, url, status, campaign_id, assigned_at,
                   updated_at)
                values (%s, %s, %s, %s, %s, 'assigned', %s, %s, %s)
                on conflict (platform, source_id) do update
                  set status = 'assigned', campaign_id = excluded.campaign_id, below_cutoff_since = null,
                      assigned_at = excluded.assigned_at, updated_at = excluded.updated_at
                  where live_sessions.status = 'released'
                returning id::text as id
                """,
                (
                    stream.creator_id,
                    stream.platform,
                    stream.stream_id,
                    stream.login,
                    stream.url,
                    stream.campaign_id,
                )
                + (now, now),
            )
            if row is not None:
                started.append(str(row["id"]))
    return started


def _creator_row(creator_id: str) -> dict[str, Any]:
    row = db.fetch_one("select * from creators where id = %s", (creator_id,))
    assert row is not None
    return row


def queue_vod(candidate: Candidate, who: Watched) -> str | None:
    """Queue a finished campaign stream for the VOD lane at campaign priority, tagged with its campaign.
    None when the video is already known or the live lane owns its replay."""
    key = candidate.key
    if dstore.known_videos([key]) or dstore.live_lane_owned([key]):
        return None
    creator = dstore.creator_from_row(_creator_row(who.creator_id))
    priority = settings.get_int("campaigns.vod_priority", int(DEFAULTS["campaigns.vod_priority"]))
    video_id = dstore.queue_video(
        Scored(candidate=candidate, creator=creator, score=0.0),
        priority=priority,
        campaign_id=who.campaign_id,
    )
    if video_id is not None:
        events.emit(
            "campaign.vod_queued",
            entity_type="video",
            entity_id=video_id,
            message=f"campaign {who.slug}: {candidate.platform} {candidate.source_id} queued "
            f"at priority {priority}",
            data={"campaign": who.slug, "source_id": candidate.source_id, "priority": priority},
            service="campaigns",
        )
    return video_id


# ── platforms ────────────────────────────────────────────────


def twitch_pass(client: TwitchClient, watched: Sequence[Watched]) -> list[LiveNow]:
    by_user = {w.channel_id: w for w in watched if w.platform == "twitch"}
    if not by_user or not client.is_configured():
        return []
    return live_from_twitch(client.get_streams_by_user(list(by_user)), by_user)


def twitch_archive(client: TwitchClient, who: Watched, now: datetime) -> Candidate | None:
    """The newest archive of a Twitch streamer who just went offline (made in the last 24 h)."""
    videos = client.get_videos_by_user(who.channel_id, type="archive", first=1)
    if not videos or now - videos[0].created_at > ARCHIVE_MAX_AGE:
        return None
    video = videos[0]
    return Candidate(
        platform="twitch",
        source_type="vod",
        source_id=video.id,
        url=video.url or f"https://www.twitch.tv/videos/{video.id}",
        title=video.title,
        channel_id=video.user_id,
        channel_handle=video.user_login,
        channel_name=video.user_name,
        origin="campaign",
        views=video.view_count,
        published_at=video.published_at or video.created_at,
        started_at=video.created_at,
        duration_s=video.duration_s,
        has_chat=True,
    )


def youtube_pass(
    client: YouTubeClient, watched: Sequence[Watched], now: datetime, live_ids: Mapping[str, str]
) -> tuple[list[LiveNow], list[tuple[Candidate, Watched]]]:
    """Read due feeds; return the live campaign streams and the finished streams to queue. `live_ids` maps
    creator id -> the YouTube video id they were live with at the last pass (re-checked every pass)."""
    channels = {w.channel_id: w for w in watched if w.platform == "youtube"}
    if not channels or not client.is_configured():
        return [], []
    every = settings.get_float("campaigns.watch_youtube_s", float(DEFAULTS["campaigns.watch_youtube_s"]))
    lookback = timedelta(hours=settings.get_float("campaigns.feed_lookback_h", 48.0))
    clock = time.monotonic()
    ids: dict[str, Watched] = {}
    for creator_id, video_id in live_ids.items():
        who = next((w for w in channels.values() if w.creator_id == creator_id), None)
        if who is not None:
            ids[video_id] = who
    for channel_id, who in channels.items():
        if clock - _feed_checked.get(channel_id, -1e9) < every:
            continue
        _feed_checked[channel_id] = clock
        for entry in client.channel_feed(channel_id):
            if now - entry.published_at <= lookback and entry.video_id not in _settled:
                ids.setdefault(entry.video_id, who)
    if not ids:
        return [], []
    known = dstore.known_videos([("youtube", video_id) for video_id in ids])
    wanted = [
        video_id for video_id in ids if ("youtube", video_id) not in known or video_id in live_ids.values()
    ]
    live: list[LiveNow] = []
    finished: list[tuple[Candidate, Watched]] = []
    for item in client.videos_list(wanted, parts=YOUTUBE_PARTS) if wanted else []:
        candidate = sources.candidate_from_api(item, None, now)
        video_id = str(item.get("id") or "")
        who = ids.get(video_id)
        if candidate is None or who is None:
            continue
        if candidate.source_type == "live":
            viewers = candidate.viewers or 0
            live.append(
                LiveNow(
                    who.creator_id,
                    who.campaign_id,
                    "youtube",
                    video_id,
                    who.channel_id,
                    candidate.url,
                    "",
                    candidate.title,
                    viewers,
                    True,
                    expected_value(who.rate_per_1k, who.budget_left, viewers),
                )
            )
        elif candidate.has_chat:
            finished.append((candidate, who))
        else:
            _settled.add(video_id)  # an upload, not a stream: never asked about again
    return live, finished


# ── one pass ─────────────────────────────────────────────────


def run_once(
    twitch: TwitchClient | None = None, youtube: YouTubeClient | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """One watcher pass. Returns a summary (also logged)."""
    now = now or datetime.now(UTC)
    twitch = twitch or TwitchClient()
    youtube = youtube or YouTubeClient()
    watched = load_watched(now)
    summary: dict[str, Any] = {"watched": len(watched)}
    # No early return when nobody is watched: sessions of a campaign that was just paused or ended still
    # have to be untagged. The platform passes make no calls without watched streamers.
    previous = previous_live()
    live_youtube_ids = {
        row["creator_id"]: row["stream_id"]
        for row in db.fetch_all(
            "select creator_id::text as creator_id, stream_id from creator_live_status "
            "where live and stream_id is not null"
        )
    }
    live: list[LiveNow] = []
    finished: list[tuple[Candidate, Watched]] = []
    try:
        live += twitch_pass(twitch, watched)
    except ClientError as exc:
        _log.warning("campaign_watch_twitch_failed", error=str(exc))
        summary["twitch_error"] = str(exc)
    try:
        yt_live, finished = youtube_pass(youtube, watched, now, live_youtube_ids)
        live += yt_live
    except ClientError as exc:
        _log.warning("campaign_watch_youtube_failed", error=str(exc))
        summary["youtube_error"] = str(exc)
    by_creator = {w.creator_id: w for w in watched}
    live_by_creator = {item.creator_id: item for item in live}
    checked = {w.creator_id for w in watched if w.platform == "twitch" and "twitch_error" not in summary}
    checked |= {w.creator_id for w in watched if w.platform == "youtube" and "youtube_error" not in summary}
    for creator_id in checked:
        save_status(creator_id, live_by_creator.get(creator_id), now)

    recordable = [item for item in live if item.platform == "twitch"]  # YouTube refuses live recording
    out = plan(
        recordable,
        active_sessions(),
        {w.creator_id: w.campaign_id for w in watched},
        max_slots=settings.get_int("live.max_slots"),
        campaign_slots=settings.get_int("live.campaign_slots", int(DEFAULTS["live.campaign_slots"])),
    )
    started = apply(out, now)

    queued: list[str] = []
    for candidate, who in finished:
        if (video_id := queue_vod(candidate, who)) is not None:
            queued.append(video_id)
    with_session = {s.creator_id for s in active_sessions()}
    offline = went_offline(previous, set(live_by_creator), checked)
    for creator_id in offline:
        who = by_creator[creator_id]
        if who.platform != "twitch" or creator_id in with_session:
            continue  # YouTube VODs come from the feed; a recorded stream's replay is queued by the live lane
        try:
            archive = twitch_archive(twitch, who, now)
        except ClientError as exc:
            _log.warning("campaign_archive_failed", creator=who.handle, error=str(exc))
            continue
        if archive is not None and (video_id := queue_vod(archive, who)) is not None:
            queued.append(video_id)

    for stream in out.start:
        events.emit(
            "campaign.stream_assigned",
            entity_type="creator",
            entity_id=stream.creator_id,
            message=f"{stream.platform} {stream.login} live ({stream.game}, {stream.viewers} viewers): "
            "campaign slot",
            data={"campaign_id": stream.campaign_id, "stream_id": stream.stream_id},
            service="campaigns",
        )
    for stream in out.overflow:
        _log.info("campaign_stream_no_slot", creator=stream.login, viewers=stream.viewers)
    summary.update(
        live=len(live),
        started=len(started),
        retagged=len(out.retag),
        untagged=len(out.untag),
        preempted=len(out.preempt),
        overflow=len(out.overflow),
        vods_queued=len(queued),
        offline=len(offline),
    )
    _log.info("campaign_watch", **summary)
    db.heartbeat(HEARTBEAT, summary)
    return summary

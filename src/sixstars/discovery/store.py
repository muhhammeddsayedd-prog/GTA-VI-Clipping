"""All SQL of discovery except the creator rows (those are in `tiers.py`): observation history in
`candidates`, live sessions, the `videos` queue and the spend counters read from `api_usage`.

Writes for many rows go to Postgres as one statement fed by a JSON array (`jsonb_to_recordset`), so a pass
with a few hundred candidates costs a handful of round trips.
"""

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db, jobs
from sixstars.discovery.assign import ACTIVE_STATUSES, Session, SlotPlan
from sixstars.discovery.models import FORBIDDEN_TIERS, Creator, Scored

VOD_JOB_TYPE = "vod.process"
VOD_JOB_PRIORITY = 30
# A past stream with one of these session states is replayed by the live lane itself (`replay.scan`).
LIVE_LANE_OWNED = ("recording", "ended", "replay_queued", "replay_done")


def _pairs(keys: Collection[tuple[str, str]]) -> Jsonb:
    return Jsonb([{"platform": platform, "source_id": source_id} for platform, source_id in keys])


# ── history ──────────────────────────────────────────────────


def viewers_before(
    keys: Collection[tuple[str, str]], now: datetime, window_s: int, min_age_s: int
) -> dict[tuple[str, str], int]:
    """Viewers of each live source about `window_s` ago: the observation closest to that moment, no older
    than one and a half windows and at least `min_age_s` old (so a stream we have known for only ten
    minutes is compared with its first observation instead of having no growth signal at all)."""
    if not keys:
        return {}
    target = now - timedelta(seconds=window_s)
    rows = db.fetch_all(
        """
        select distinct on (c.platform, c.source_id) c.platform, c.source_id, c.viewers
        from candidates c
        join jsonb_to_recordset(%s) as k(platform text, source_id text)
          on k.platform = c.platform and k.source_id = c.source_id
        where c.source_type = 'live' and c.viewers is not null and c.checked_at between %s and %s
        order by c.platform, c.source_id, abs(extract(epoch from c.checked_at - %s))
        """,
        (
            _pairs(keys),
            now - timedelta(seconds=window_s * 1.5),
            now - timedelta(seconds=min_age_s),
            target,
        ),
    )
    return {(row["platform"], row["source_id"]): int(row["viewers"]) for row in rows}


def peak_viewers(keys: Collection[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """Highest audience we saw for each source while it was live (YouTube keeps the video id afterwards)."""
    if not keys:
        return {}
    rows = db.fetch_all(
        """
        select c.platform, c.source_id, max(c.viewers) as peak
        from candidates c
        join jsonb_to_recordset(%s) as k(platform text, source_id text)
          on k.platform = c.platform and k.source_id = c.source_id
        where c.source_type = 'live' and c.viewers is not null
        group by c.platform, c.source_id
        """,
        (_pairs(keys),),
    )
    return {(row["platform"], row["source_id"]): int(row["peak"]) for row in rows}


def chat_rates(creator_ids: Collection[str]) -> dict[str, float]:
    """Measured chat messages per minute of each creator, averaged over the chat files we downloaded."""
    if not creator_ids:
        return {}
    rows = db.fetch_all(
        """
        select v.creator_id::text as creator_id,
               avg(f.messages_count::numeric * 60 / f.duration_s) as mpm
        from chat_files f
        join videos v on v.id = f.video_id
        where v.creator_id = any(%s::uuid[]) and f.duration_s > 0 and f.messages_count is not null
        group by v.creator_id
        """,
        (list(creator_ids),),
    )
    return {row["creator_id"]: float(row["mpm"]) for row in rows}


def clip_view_population() -> list[float]:
    """`avg_clip_views` of every creator that has one (the population of the history percentile)."""
    rows = db.fetch_all("select avg_clip_views from creators where avg_clip_views is not null")
    return [float(row["avg_clip_views"]) for row in rows]


def videos_waiting() -> int:
    """Videos queued for the VOD lane that it has not started yet (status `new`)."""
    row = db.fetch_one("select count(*) as n from videos where status = 'new'")
    return int(row["n"]) if row is not None else 0


def approved_hourly_clips() -> int:
    """Approved clips waiting in the hourly lane (compared with `posting.queue_low_watermark`)."""
    row = db.fetch_one("select count(*) as n from clips where status = 'approved' and lane = 'hourly'")
    return int(row["n"]) if row is not None else 0


# ── spend counters ───────────────────────────────────────────


def youtube_units_today() -> float:
    """YouTube Data API quota units recorded since 00:00 UTC (the real quota resets at midnight Pacific
    time; UTC is used like every other daily budget of the system)."""
    row = db.fetch_one(
        """
        select coalesce(sum(units), 0) as total from api_usage
        where provider = 'youtube' and operation like '%%.list' and operation <> 'search.list'
          and created_at >= date_trunc('day', now(), 'UTC')
        """
    )
    return float(row["total"]) if row is not None else 0.0


def youtube_search_calls_today() -> int:
    """`search.list` calls recorded since 00:00 UTC. They have their own quota bucket (100 calls a day by
    default), so they are counted apart from `youtube_units_today`."""
    row = db.fetch_one(
        """
        select count(*) as total from api_usage
        where provider = 'youtube' and operation = 'search.list'
          and created_at >= date_trunc('day', now(), 'UTC')
        """
    )
    return int(row["total"]) if row is not None else 0


def recent_live_sources(now: datetime, window_s: int, limit: int) -> list[dict[str, Any]]:
    """YouTube streams to look at again: every stream that holds a live slot, then the streams whose
    newest observation within `window_s` was live, best score first, `limit` in all. Forbidden creators
    are left out. Rows: `source_id`, `title`, `channel_id`, `channel_name`, `viewers`."""
    if limit <= 0:
        return []
    rows = db.fetch_all(
        """
        with latest as (
          select distinct on (c.source_id) c.source_id, c.source_type, c.title, c.viewers, c.trend_score,
                 c.creator_id
          from candidates c
          where c.platform = 'youtube' and c.checked_at between %(since)s and %(now)s
          order by c.source_id, c.checked_at desc
        ), wanted as (
          select l.source_id, l.title, l.viewers, l.trend_score, l.creator_id, false as held
          from latest l where l.source_type = 'live'
          union all
          select s.source_id, null, null, s.trend_score, s.creator_id, true
          from live_sessions s where s.platform = 'youtube' and s.status = any(%(active)s)
        )
        select distinct on (w.source_id) w.source_id, w.title, w.viewers, w.trend_score, w.held,
               cr.channel_id, coalesce(cr.display_name, cr.handle) as channel_name
        from wanted w join creators cr on cr.id = w.creator_id
        where not (cr.tier = any(%(forbidden)s))
        order by w.source_id, w.held desc
        """,
        {
            "since": now - timedelta(seconds=window_s),
            "now": now,
            "active": list(ACTIVE_STATUSES),
            "forbidden": sorted(FORBIDDEN_TIERS),
        },
    )
    rows.sort(key=lambda row: (not row["held"], -float(row["trend_score"] or 0), str(row["source_id"])))
    return [dict(row) for row in rows[:limit]]


def apify_spend() -> tuple[float, float]:
    """Apify dollars recorded today and this calendar month (UTC), for every use of Apify."""
    row = db.fetch_one(
        """
        select coalesce(sum(cost_usd) filter (where created_at >= date_trunc('day', now(), 'UTC')), 0) as day,
               coalesce(sum(cost_usd), 0) as month
        from api_usage
        where provider = 'apify' and created_at >= date_trunc('month', now(), 'UTC')
        """
    )
    return (float(row["day"]), float(row["month"])) if row is not None else (0.0, 0.0)


# ── candidates ───────────────────────────────────────────────


def _candidate_row(item: Scored) -> dict[str, Any]:
    candidate = item.candidate

    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "creator_id": item.creator.id,
        "source_type": candidate.source_type,
        "platform": candidate.platform,
        "source_id": candidate.source_id,
        "url": candidate.url,
        "title": candidate.title,
        "language": candidate.language,
        "viewers": candidate.viewers,
        "views": candidate.views,
        "views_per_hour": item.views_per_hour,
        "likes": candidate.likes,
        "comments": candidate.comments,
        "published_at": iso(candidate.published_at),
        "started_at": iso(candidate.started_at),
        "trend_score": item.score,
        "score_parts": item.parts,
    }


def record_candidates(scored: Sequence[Scored], now: datetime, min_gap_s: int) -> int:
    """Store one observation per source. A source already observed within `min_gap_s` gets its newest row
    updated instead, so a pass that is repeated at once adds nothing. Returns the number of new rows."""
    if not scored:
        return 0
    row = db.fetch_one(
        """
        with incoming as (
          select * from jsonb_to_recordset(%(rows)s) as x(
            creator_id uuid, source_type text, platform text, source_id text, url text, title text,
            language text, viewers int, views bigint, views_per_hour numeric, likes bigint, comments bigint,
            published_at timestamptz, started_at timestamptz, trend_score numeric, score_parts jsonb)
        ), recent as (
          select distinct on (c.platform, c.source_id) c.id, c.platform, c.source_id
          from candidates c
          join incoming i on i.platform = c.platform and i.source_id = c.source_id
          where c.checked_at > %(cutoff)s
          order by c.platform, c.source_id, c.checked_at desc
        ), updated as (
          update candidates c
          set source_type = i.source_type, url = i.url, title = i.title, language = i.language,
              viewers = i.viewers, views = i.views, views_per_hour = i.views_per_hour, likes = i.likes,
              comments = i.comments, published_at = i.published_at, started_at = i.started_at,
              trend_score = i.trend_score, score_parts = i.score_parts, checked_at = %(now)s
          from recent r
          join incoming i on i.platform = r.platform and i.source_id = r.source_id
          where c.id = r.id
          returning c.id
        ), inserted as (
          insert into candidates
            (creator_id, source_type, platform, source_id, url, title, language, viewers, views,
             views_per_hour, likes, comments, published_at, started_at, trend_score, score_parts, checked_at)
          select i.creator_id, i.source_type, i.platform, i.source_id, i.url, i.title, i.language,
                 i.viewers, i.views, i.views_per_hour, i.likes, i.comments, i.published_at, i.started_at,
                 i.trend_score, i.score_parts, %(now)s
          from incoming i
          where not exists (
            select 1 from recent r where r.platform = i.platform and r.source_id = i.source_id)
          returning id
        )
        select (select count(*) from inserted) as inserted, (select count(*) from updated) as updated
        """,
        {
            "rows": Jsonb([_candidate_row(item) for item in scored]),
            "cutoff": now - timedelta(seconds=min_gap_s),
            "now": now,
        },
    )
    return int(row["inserted"]) if row is not None else 0


def mark_picked(keys: Collection[tuple[str, str]], now: datetime) -> None:
    """Flag this pass's observation of each picked source."""
    if not keys:
        return
    db.execute(
        """
        update candidates c set picked = true
        from jsonb_to_recordset(%s) as k(platform text, source_id text)
        where c.platform = k.platform and c.source_id = k.source_id and c.checked_at = %s
        """,
        (_pairs(keys), now),
    )


# ── live sessions ────────────────────────────────────────────


def active_sessions() -> list[Session]:
    """Sessions that hold a live slot, with the creator's handle for the one-source-per-creator rule."""
    rows = db.fetch_all(
        """
        select s.id::text as id, s.creator_id::text as creator_id, s.platform, s.source_id, s.status,
               s.below_cutoff_since, c.handle, c.channel_id, c.tier
        from live_sessions s join creators c on c.id = s.creator_id
        where s.status = any(%s)
        order by s.assigned_at
        """,
        (list(ACTIVE_STATUSES),),
    )
    sessions = []
    for row in rows:
        squashed = "".join(char for char in str(row["handle"]).casefold() if char.isalnum())
        sessions.append(
            Session(
                id=row["id"],
                creator_id=row["creator_id"],
                platform=row["platform"],
                source_id=row["source_id"],
                status=row["status"],
                below_cutoff_since=row["below_cutoff_since"],
                identity=squashed or f"{row['platform']}:{row['channel_id']}",
                creator_tier=row["tier"],
            )
        )
    return sessions


def apply_slot_plan(plan: SlotPlan, now: datetime) -> tuple[list[tuple[str, Scored]], list[str]]:
    """Write the plan. Returns `(assigned (session id, candidate) pairs, released session ids)`; a session
    that another process changed in the meantime is left alone and not reported."""
    assigned: list[tuple[str, Scored]] = []
    released: list[str] = []
    with db.transaction():
        for keep in plan.keep:
            db.execute(
                """
                update live_sessions
                set trend_score = coalesce(%s, trend_score), below_cutoff_since = %s, updated_at = %s
                where id = %s and status = any(%s)
                """,
                (keep.score, keep.below_cutoff_since, now, keep.session_id, list(ACTIVE_STATUSES)),
            )
        for release in plan.release:
            done = db.execute(
                "update live_sessions set status = 'released', updated_at = %s "
                "where id = %s and status = any(%s)",
                (now, release.session_id, list(ACTIVE_STATUSES)),
            )
            if done:
                released.append(release.session_id)
        for item in plan.assign:
            candidate = item.candidate
            login = candidate.channel_handle if candidate.platform == "twitch" else candidate.channel_id
            row = db.fetch_one(
                """
                insert into live_sessions
                  (creator_id, platform, source_id, channel_login, url, status, trend_score, assigned_at,
                   updated_at)
                values (%s, %s, %s, %s, %s, 'assigned', %s, %s, %s)
                on conflict (platform, source_id) do update
                  set status = 'assigned', trend_score = excluded.trend_score, below_cutoff_since = null,
                      assigned_at = excluded.assigned_at, updated_at = excluded.updated_at
                  where live_sessions.status = 'released'
                returning id::text as id
                """,
                (item.creator.id, candidate.platform, candidate.source_id, login, candidate.url, item.score)
                + (now, now),
            )
            if row is not None:
                assigned.append((row["id"], item))
    return assigned, released


# ── videos ───────────────────────────────────────────────────


def known_videos(keys: Collection[tuple[str, str]]) -> set[tuple[str, str]]:
    """Sources that already have a `videos` row."""
    if not keys:
        return set()
    rows = db.fetch_all(
        """
        select v.platform, v.source_id
        from videos v
        join jsonb_to_recordset(%s) as k(platform text, source_id text)
          on k.platform = v.platform and k.source_id = v.source_id
        """,
        (_pairs(keys),),
    )
    return {(row["platform"], row["source_id"]) for row in rows}


def live_lane_owned(keys: Collection[tuple[str, str]]) -> set[tuple[str, str]]:
    """Past streams that the live lane recorded: it queues their replay itself, so discovery must not."""
    if not keys:
        return set()
    rows = db.fetch_all(
        """
        select s.platform, coalesce(s.vod_source_id, s.source_id) as source_id
        from live_sessions s
        join jsonb_to_recordset(%s) as k(platform text, source_id text)
          on k.platform = s.platform and k.source_id = coalesce(s.vod_source_id, s.source_id)
        where s.status = any(%s)
        """,
        (_pairs(keys), list(LIVE_LANE_OWNED)),
    )
    return {(row["platform"], row["source_id"]) for row in rows}


def vod_job_payload(video_id: str, item: Scored) -> dict[str, Any]:
    """Payload of a `vod.process` job (M08 reads the `videos` row through `video_id`)."""
    return {
        "video_id": video_id,
        "creator_id": item.creator.id,
        "platform": item.candidate.platform,
        "source_id": item.candidate.source_id,
        "url": item.candidate.url,
    }


def queue_video(item: Scored) -> str | None:
    """Insert the `videos` row (status `new`) and its `vod.process` job in one transaction. Returns the
    video id, or None when the video was already there."""
    candidate = item.candidate
    with db.transaction():
        row = db.fetch_one(
            """
            insert into videos
              (creator_id, platform, source_id, url, title, duration_s, published_at, trend_score, has_chat)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (platform, source_id) do nothing
            returning id::text as id
            """,
            (item.creator.id, candidate.platform, candidate.source_id, candidate.url, candidate.title)
            + (candidate.duration_s, candidate.published_at, item.score, candidate.has_chat),
        )
        if row is None:
            return None
        video_id = str(row["id"])
        jobs.enqueue(
            "normal",
            VOD_JOB_TYPE,
            vod_job_payload(video_id, item),
            priority=VOD_JOB_PRIORITY,
            dedupe_key=f"vod:{video_id}",
        )
        return video_id


def retire_creator_work(creator_id: str) -> int:
    """A creator became forbidden: skip their videos that have not started and cancel the queued jobs.
    Their live sessions are released by the next discovery pass. Returns the number of videos skipped."""
    rows = db.fetch_all(
        """
        update videos set status = 'skipped', skip_reason = 'creator forbids clipping', updated_at = now()
        where creator_id = %s and status = 'new'
        returning id::text as id
        """,
        (creator_id,),
    )
    for row in rows:
        jobs.cancel(f"vod:{row['id']}")
    return len(rows)


def creators_by_id(ids: Collection[str]) -> dict[str, Mapping[str, Any]]:
    rows = db.fetch_all("select * from creators where id = any(%s::uuid[])", (list(ids),))
    return {str(row["id"]): row for row in rows}


def creator_from_row(row: Mapping[str, Any], is_new: bool = False) -> Creator:
    """Build a `Creator` from a `creators` row dict."""

    def number(value: Any) -> float | None:
        return None if value is None else float(value)

    return Creator(
        id=str(row["id"]),
        platform=row["platform"],
        channel_id=row["channel_id"],
        handle=row["handle"],
        display_name=row["display_name"],
        tier=row["tier"],
        language=row["language"],
        follower_count=None if row["follower_count"] is None else int(row["follower_count"]),
        audience_spread=number(row["audience_spread"]),
        avg_clip_views=number(row["avg_clip_views"]),
        claims_count=int(row["claims_count"] or 0),
        description_checked_at=row["description_checked_at"],
        is_new=is_new,
    )

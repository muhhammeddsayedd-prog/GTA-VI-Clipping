"""Read queries and shaping helpers behind the admin console routes (`GET /v1/admin/*`, docs/26).

The first half of the module is pure (cursor coding, counts, slot states, search classification, small
statistics) and is unit-tested without a database. The second half is SQL through `sixstars.db`, plus calls
into the modules that already own a number (`api/status.py`, `guardian/*`, `posting/*`, `stats/report.py`,
`discovery/store.py`), so the console shows exactly what the pipeline itself computes.

Large JSON columns (`clips.transcript`, `spikes.chat_sample`, `jobs.payload`, ...) are only read by the
detail queries. Every list is keyset-paginated on `(<time column>, id)`, newest first.
"""

import base64
import binascii
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sixstars import db, settings
from sixstars.api import status as status_view
from sixstars.api.errors import ApiError
from sixstars.discovery import store as discovery_store
from sixstars.guardian import health as guardian_health
from sixstars.guardian import incidents
from sixstars.guardian import stop as guardian_stop
from sixstars.guardian.config import TRIGGER_PREFIX, TRIGGERS, seconds_map
from sixstars.logging import get_logger
from sixstars.posting import caps as posting_caps
from sixstars.posting import gate as posting_gate
from sixstars.posting.models import Account, Lane, PostRecord
from sixstars.posting.params import PostingParams
from sixstars.posting.precedence import spike_order
from sixstars.posting.selection import hourly_candidates
from sixstars.posting.slots import fire_at, next_slot, slot_start
from sixstars.posting.store import DbStore, account_from_row
from sixstars.stats import report as report_view

Row = dict[str, Any]

DEFAULT_LIMIT = 50
EVENTS_PER_ENTITY = 50  # docs/26: the last 50 events of an entity
RELATED_WINDOW = timedelta(minutes=10)  # docs/26: events/{id} shows the same entity within ±10 min
SEARCH_MAX_RESULTS = 20
SEARCH_MIN_UUID_HEX = 6
UP_NEXT = 5
RECENT_EVENTS = 10
DISCOVERY_RUNS = 20
CREATOR_CLIPS = 12
ACCOUNT_POSTS = 20
ACCOUNT_STATS_DAYS = 30
DECISIONS = 50
REJECT_REASONS = 20
LEVELS = ("debug", "info", "warn", "error", "critical")
WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
SPIKE_TIERS = ("S", "A", "B")
DECISION_TYPES = (
    "post.planned",
    "post.dry_run",
    "post.cancelled",
    "posting.slot_skipped",
    "posting.slot_empty",
    "posting.spike_deferred",
    "posting.spike_skipped",
)

# Settings that the code writes itself (state, caches, cursors); the console shows them read-only.
WRITTEN_BY_CODE = frozenset(
    {
        "discovery.apify_last_run",
        "discovery.rss_cursor",
        "discovery.twitch_game_cache",
        "discovery.search_api_state",
        "discovery.source_health",
        "live.source_health",
        "vod.source_health",
        "vod.paused_until",
    }
)

LIVE_STATUSES = ("assigned", "recording", "ended", "replay_queued", "replay_done", "failed", "released")
VIDEO_STATUSES = ("new", "processing", "done", "failed", "skipped")
SPIKE_STATUSES = ("detected", "confirmed", "rejected", "rendering", "clipped", "failed", "merged")
CLIP_STATUSES = (
    "rendering",
    "gating",
    "precheck_pending",
    "approved",
    "rejected",
    "posted",
    "removed",
    "expired",
)
POST_STATUSES = ("pending", "published", "failed", "skipped", "removed", "remove_failed")
CREATOR_TIERS = ("preferred", "auto", "restricted", "blocked")
SOURCE_TYPES = ("live", "vod")
TAKEDOWN_STATUSES = ("open", "actioned", "partial", "ignored")
SEVERITIES = ("info", "warn", "critical")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")
JOB_QUEUES = ("express", "normal")


# ── pure helpers ─────────────────────────────────────────────


def jsonify(value: Any) -> Any:
    """A database value as JSON: times as ISO-8601 UTC, numerics as floats, uuids as strings."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): jsonify(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonify(item) for item in value]
    return value


def encode_cursor(at: datetime, row_id: str | int) -> str:
    """An opaque cursor for the row after which the next page starts."""
    text = f"{jsonify(at)}|{row_id}"
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """The `(time, id)` of a cursor. Raises `ValueError` for anything `encode_cursor` did not make."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise ValueError("not a cursor") from exc
    stamp, sep, row_id = text.partition("|")
    if not sep or not row_id:
        raise ValueError("not a cursor")
    at = datetime.fromisoformat(stamp)
    if at.tzinfo is None:
        raise ValueError("not a cursor")
    return at, row_id


def clamp_limit(limit: int | None, max_limit: int, default: int = DEFAULT_LIMIT) -> int:
    """The page size: `default` when not given, never below 1 or above `max_limit`."""
    wanted = default if limit is None else limit
    return max(1, min(wanted, max(1, max_limit)))


def shape_counts(rows: Iterable[tuple[str | None, int]], keys: Sequence[str]) -> dict[str, int]:
    """`{key: n for every known key (0 when absent), ..., "all": total}`; unknown keys are kept too."""
    out = {key: 0 for key in keys}
    total = 0
    for key, n in rows:
        total += int(n)
        if key is not None:
            out[str(key)] = out.get(str(key), 0) + int(n)
    out["all"] = total
    return out


def post_slot_state(status: str, dry_run: bool) -> str:
    """The slot state shown for an hourly `posts` row."""
    if dry_run or status == "skipped":
        return "skipped"
    if status in ("published", "removed"):
        return "posted"
    if status in ("failed", "remove_failed"):
        return "failed"
    return "planned"  # pending


def slot_state(hour: int, current_hour: int, allowed: bool, post: tuple[str, bool] | None) -> str:
    """One hour of today's slot grid: `posted`, `failed`, `skipped`, `planned`, `not_allowed` or `empty`.

    `post` is `(status, dry_run)` of the account's hourly post in that slot, if any. Without a post, an
    hour the account may not post in is `not_allowed`, the current and later hours are `planned`, and an
    earlier hour is `empty`."""
    if post is not None:
        return post_slot_state(*post)
    if not allowed:
        return "not_allowed"
    if hour >= current_hour:
        return "planned"
    return "empty"


def hour_allowed(account: Account, hour: int) -> bool:
    """True when the hourly lane may post to `account` in UTC hour `hour` (ignoring caps and pauses)."""
    if not account.enabled or Lane.HOURLY not in account.lanes:
        return False
    return account.hourly_hours_utc is None or hour in account.hourly_hours_utc


def hour_buckets(times: Iterable[datetime], now: datetime, hours: int = 24) -> list[int]:
    """Counts per UTC hour for the last `hours` hours (the current one included), oldest first."""
    last = slot_start(now)
    first = last - timedelta(hours=hours - 1)
    out = [0] * hours
    for moment in times:
        index = int((slot_start(moment) - first).total_seconds() // 3600)
        if 0 <= index < hours:
            out[index] += 1
    return out


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile (`q` in 0..1), or None for no values."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, q))
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def daily_deltas(points: Iterable[tuple[datetime, int | None]]) -> list[dict[str, Any]]:
    """Views gained per UTC day from `views_total` snapshots: the last value of each day minus the last
    value of the day before. The first day has no earlier value and is left out."""
    last_of_day: dict[date, int] = {}
    for at, total in sorted(points, key=lambda item: item[0]):
        if total is not None:
            last_of_day[at.astimezone(UTC).date()] = int(total)
    days = sorted(last_of_day)
    return [
        {"day": day.isoformat(), "views": last_of_day[day] - last_of_day[previous]}
        for previous, day in zip(days, days[1:], strict=False)
    ]


def settings_group(key: str) -> str:
    """The group of a settings key: the part before the first dot."""
    return key.split(".", 1)[0]


_UUID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
_SERIES = re.compile(r"^#?(\d{1,9})$")
_UUID_PREFIX = re.compile(r"^[0-9a-fA-F-]+$")


def is_uuid(text: str) -> bool:
    return bool(_UUID.match(text))


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """What a search string can mean. Several readings may apply at once (`142857` is both)."""

    text: str
    series_no: int | None
    uuid_prefix: str | None


def classify_search(q: str) -> SearchQuery:
    """Series number (`#142` or `142`), uuid prefix (at least 6 hex characters) and the plain text."""
    text = q.strip()
    series = _SERIES.match(text)
    prefix = None
    if _UUID_PREFIX.match(text) and len(text.replace("-", "")) >= SEARCH_MIN_UUID_HEX and len(text) <= 36:
        prefix = text.lower()
    return SearchQuery(text=text, series_no=int(series.group(1)) if series else None, uuid_prefix=prefix)


def like_escape(text: str) -> str:
    """`text` with the LIKE wildcards escaped (the default escape character is a backslash)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def number(value: Any) -> float | None:
    """A JSON number setting as a float, or None when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


# ── statement-level helpers ──────────────────────────────────


def _event_columns(alias: str = "e") -> str:
    return (
        f"{alias}.id, {alias}.created_at, {alias}.level, {alias}.type, {alias}.service, "
        f"{alias}.entity_type, {alias}.entity_id, {alias}.message, {alias}.data"
    )


def creator_json(alias: str = "cr", nullable: bool = False) -> str:
    obj = (
        f"jsonb_build_object('id', {alias}.id, 'handle', {alias}.handle, 'display_name', "
        f"{alias}.display_name, 'platform', {alias}.platform, 'tier', {alias}.tier)"
    )
    return f"case when {alias}.id is null then null else {obj} end" if nullable else obj


def entity_events(entity_type: str, entity_id: str, limit: int = EVENTS_PER_ENTITY) -> list[Row]:
    return db.fetch_all(
        f"select {_event_columns()} from events e where e.entity_type = %s and e.entity_id = %s "
        "order by e.id desc limit %s",
        (entity_type, entity_id, limit),
    )


def events_of_types(types: Sequence[str], limit: int) -> list[Row]:
    return db.fetch_all(
        f"select {_event_columns()} from events e where e.type = any(%s) order by e.id desc limit %s",
        (list(types), limit),
    )


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"there is no {what} with this id")


def require_uuid(value: str, what: str) -> str:
    """`value` when it is a uuid; otherwise a 404 (an unknown id, not a bad request)."""
    if not is_uuid(value):
        raise not_found(what)
    return value


def bad_filter(name: str, value: str) -> ApiError:
    return ApiError(422, "invalid_filter", f"{name}={value!r} is not a known value")


# ── keyset lists ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ListSpec:
    """One paginated list: `columns` selected from `source`, ordered by `(time_col, id_col)` newest first.
    `status_col` is the route's main filter; `counts` groups by it."""

    columns: str
    source: str
    time_col: str
    id_col: str
    id_cast: str  # "uuid" or "bigint"
    status_col: str
    statuses: tuple[str, ...]


Filter = tuple[str, tuple[Any, ...]]


def run_list(
    spec: ListSpec,
    filters: Sequence[Filter],
    status: str | None,
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    """`{"items", "next_cursor", "counts"}` for one page."""
    if status is not None and status not in spec.statuses:
        raise bad_filter("status", status)
    where: list[str] = []
    params: list[Any] = []
    for sql, values in filters:
        where.append(sql)
        params.extend(values)
    counts_where = " and ".join(where) or "true"
    counts_rows = db.fetch_all(
        f"select {spec.status_col} as k, count(*) as n from {spec.source} where {counts_where} group by 1",
        params,
    )
    page_where = list(where)
    page_params = list(params)
    if status is not None:
        page_where.append(f"{spec.status_col} = %s")
        page_params.append(status)
    if cursor:
        try:
            at, row_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ApiError(422, "invalid_cursor", "the cursor is not one this API gave out") from exc
        if spec.id_cast == "bigint" and not row_id.isdigit():
            raise ApiError(422, "invalid_cursor", "the cursor is not one this API gave out")
        if spec.id_cast == "uuid" and not is_uuid(row_id):
            raise ApiError(422, "invalid_cursor", "the cursor is not one this API gave out")
        page_where.append(f"({spec.time_col}, {spec.id_col}) < (%s::timestamptz, %s::{spec.id_cast})")
        page_params.extend([at, row_id])
    rows = db.fetch_all(
        f"select {spec.columns}, {spec.time_col} as _cursor_at, {spec.id_col} as _cursor_id "
        f"from {spec.source} where {' and '.join(page_where) or 'true'} "
        f"order by {spec.time_col} desc, {spec.id_col} desc limit %s",
        [*page_params, limit + 1],
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last["_cursor_at"], str(last["_cursor_id"]))
    for row in rows:
        row.pop("_cursor_at", None)
        row.pop("_cursor_id", None)
    return {
        "items": rows,
        "next_cursor": next_cursor,
        "counts": shape_counts(((r["k"], int(r["n"])) for r in counts_rows), spec.statuses),
    }


# ── column sets ──────────────────────────────────────────────

SPIKE_COLUMNS = f"""
sp.id::text as id, sp.mode, {creator_json()} as creator, sp.live_session_id::text as live_session_id,
sp.video_id::text as video_id, sp.detected_at, sp.spike_wall, sp.spike_offset_s, sp.score_z, sp.parts,
sp.tier, sp.claude_confidence, sp.claude_reason, sp.status, sp.merged_into::text as merged_into,
sp.clip_id::text as clip_id, sc.series_no as clip_series_no, sp.spike_to_post_s, sp.window_start_s,
sp.window_end_s, sp.window_start_wall, sp.window_end_wall
"""
SPIKE_SOURCE = "spikes sp join creators cr on cr.id = sp.creator_id left join clips sc on sc.id = sp.clip_id"

CLIP_COLUMNS = f"""
c.id::text as id, c.series_no, c.spike_id::text as spike_id, {creator_json()} as creator, c.source_platform,
c.source_type, c.lane, c.duration_s, c.hook, c.title, c.topic, c.score, c.rank_score, c.music_ratio,
c.status, c.reject_reason, c.gate_profile, c.created_at, c.approved_at, c.posted_at,
(c.storage_path is not null) as has_file
"""
CLIP_SOURCE = "clips c join creators cr on cr.id = c.creator_id"

POST_COLUMNS = """
p.id::text as id, p.clip_id::text as clip_id, pc.series_no as clip_series_no, pc.hook as clip_hook,
p.account_id, p.platform, p.lane, p.slot_at, p.status, p.dry_run, p.post_url, p.platform_post_id,
p.published_at, p.views, p.likes, p.comments, p.shares, p.claim_status, p.error, p.attempts, p.created_at
"""
POST_SOURCE = "posts p join clips pc on pc.id = p.clip_id"

LIVE_COLUMNS = f"""
s.id::text as id, {creator_json()} as creator, s.platform, s.status, s.trend_score, s.url, s.channel_login,
s.reconnects, s.assigned_at, s.started_at, s.ended_at, s.below_cutoff_since,
(select jsonb_build_object('S', count(*) filter (where x.tier = 'S'),
                           'A', count(*) filter (where x.tier = 'A'),
                           'B', count(*) filter (where x.tier = 'B'))
   from spikes x where x.live_session_id = s.id) as spikes,
(select count(*) from clips xc join spikes x on x.id = xc.spike_id where x.live_session_id = s.id) as clips
"""
LIVE_SOURCE = "live_sessions s join creators cr on cr.id = s.creator_id"

VIDEO_COLUMNS = f"""
v.id::text as id, {creator_json()} as creator, v.platform, v.source_id, v.url, v.title, v.duration_s,
v.published_at, v.trend_score, v.has_chat, v.status, v.skip_reason, v.attempts, v.created_at, v.updated_at,
(select count(*) from clips xc join spikes x on x.id = xc.spike_id where x.video_id = v.id) as clips
"""
VIDEO_SOURCE = "videos v join creators cr on cr.id = v.creator_id"

CREATOR_COLUMNS = """
cr.id::text as id, cr.platform, cr.channel_id, cr.handle, cr.display_name, cr.language, cr.tier,
cr.tier_reason, cr.follower_count, cr.avg_clip_views, cr.claims_count, cr.description_checked_at,
cr.discovered_at,
(select count(*) from clips xc where xc.creator_id = cr.id and xc.status = 'posted') as clips_posted
"""

CANDIDATE_COLUMNS = f"""
ca.id, ca.creator_id::text as creator_id, {creator_json()} as creator, ca.source_type, ca.platform,
ca.source_id, ca.url, ca.title, ca.language, ca.viewers, ca.views, ca.views_per_hour, ca.likes,
ca.comments, ca.published_at, ca.started_at, ca.trend_score, ca.score_parts, ca.checked_at, ca.picked
"""

TAKEDOWN_COLUMNS = f"""
t.id::text as id, t.received_at, t.source, t.sender, t.subject, t.body_excerpt, t.is_takedown,
t.creator_id::text as creator_id, {creator_json(nullable=True)} as creator, t.matched_post_ids, t.actions,
t.status, t.message_id
"""
TAKEDOWN_SOURCE = "takedowns t left join creators cr on cr.id = t.creator_id"

JOB_COLUMNS = """
j.id, j.queue, j.type, j.priority, j.status, j.attempts, j.max_attempts, j.run_after, j.locked_by,
j.locked_at, j.last_error, j.dedupe_key, j.created_at, j.updated_at
"""

ALERT_COLUMNS = (
    "o.id, o.created_at, o.severity, o.title, o.body, o.sent_at, o.attempts, "
    "(o.sent_at is not null) as delivered"
)

LISTS: dict[str, ListSpec] = {
    "live-sessions": ListSpec(
        LIVE_COLUMNS, LIVE_SOURCE, "s.assigned_at", "s.id", "uuid", "s.status", LIVE_STATUSES
    ),
    "videos": ListSpec(
        VIDEO_COLUMNS, VIDEO_SOURCE, "v.created_at", "v.id", "uuid", "v.status", VIDEO_STATUSES
    ),
    "spikes": ListSpec(
        SPIKE_COLUMNS, SPIKE_SOURCE, "sp.detected_at", "sp.id", "uuid", "sp.status", SPIKE_STATUSES
    ),
    "clips": ListSpec(CLIP_COLUMNS, CLIP_SOURCE, "c.created_at", "c.id", "uuid", "c.status", CLIP_STATUSES),
    "posts": ListSpec(POST_COLUMNS, POST_SOURCE, "p.created_at", "p.id", "uuid", "p.status", POST_STATUSES),
    "creators": ListSpec(
        CREATOR_COLUMNS, "creators cr", "cr.discovered_at", "cr.id", "uuid", "cr.tier", CREATOR_TIERS
    ),
    "candidates": ListSpec(
        CANDIDATE_COLUMNS,
        "candidates ca join creators cr on cr.id = ca.creator_id",
        "ca.checked_at",
        "ca.id",
        "bigint",
        "ca.source_type",
        SOURCE_TYPES,
    ),
    "takedowns": ListSpec(
        TAKEDOWN_COLUMNS, TAKEDOWN_SOURCE, "t.received_at", "t.id", "uuid", "t.status", TAKEDOWN_STATUSES
    ),
    "alerts": ListSpec(
        ALERT_COLUMNS, "notify_outbox o", "o.created_at", "o.id", "bigint", "o.severity", SEVERITIES
    ),
    "jobs": ListSpec(JOB_COLUMNS, "jobs j", "j.created_at", "j.id", "bigint", "j.status", JOB_STATUSES),
    "events": ListSpec(_event_columns(), "events e", "e.created_at", "e.id", "bigint", "e.level", LEVELS),
}


# ── trace ────────────────────────────────────────────────────


def _ref(sql: str, row_id: str | None) -> Row | None:
    if row_id is None:
        return None
    return db.fetch_one(sql, (row_id,))


def build_trace(
    *,
    creator_id: str | None = None,
    live_session_id: str | None = None,
    video_id: str | None = None,
    spike_id: str | None = None,
    clip_id: str | None = None,
) -> dict[str, Any]:
    """Links from one entity to the others of its chain: creator, live session, video, spike, clip, posts.
    Missing links are filled in from the rows that are known (a clip names its spike, a spike its
    session or video, a video its session)."""
    if clip_id is not None and spike_id is None:
        row = db.fetch_one(
            "select spike_id::text as spike_id, creator_id::text as c from clips where id = %s", (clip_id,)
        )
        if row is not None:
            spike_id = row["spike_id"]
            creator_id = creator_id or row["c"]
    if spike_id is not None:
        row = db.fetch_one(
            "select live_session_id::text as l, video_id::text as v, clip_id::text as k, "
            "creator_id::text as c from spikes where id = %s",
            (spike_id,),
        )
        if row is not None:
            live_session_id = live_session_id or row["l"]
            video_id = video_id or row["v"]
            clip_id = clip_id or row["k"]
            creator_id = creator_id or row["c"]
    if video_id is not None:
        row = db.fetch_one(
            "select live_session_id::text as l, creator_id::text as c from videos where id = %s", (video_id,)
        )
        if row is not None:
            live_session_id = live_session_id or row["l"]
            creator_id = creator_id or row["c"]
    if live_session_id is not None and creator_id is None:
        row = db.fetch_one(
            "select creator_id::text as c from live_sessions where id = %s", (live_session_id,)
        )
        creator_id = row["c"] if row is not None else None
    posts = (
        db.fetch_all(
            "select id::text as id, account_id || case when dry_run then ' (dry run)' else '' end as label, "
            "status from posts where clip_id = %s order by created_at, id",
            (clip_id,),
        )
        if clip_id is not None
        else []
    )
    return {
        "creator": _ref(
            "select id::text as id, handle as label, tier as status from creators where id = %s", creator_id
        ),
        "live_session": _ref(
            "select id::text as id, channel_login as label, status from live_sessions where id = %s",
            live_session_id,
        ),
        "video": _ref(
            "select id::text as id, coalesce(title, source_id) as label, status from videos where id = %s",
            video_id,
        ),
        "spike": _ref(
            "select id::text as id, mode || ' spike ' || coalesce(tier, '?') as label, status "
            "from spikes where id = %s",
            spike_id,
        ),
        "clip": _ref(
            "select id::text as id, coalesce('#' || series_no || ' ', '') || coalesce(hook, title, '') "
            "as label, status from clips where id = %s",
            clip_id,
        ),
        "posts": posts,
    }


# ── incidents, heartbeats, triggers, jobs ────────────────────


def open_incidents() -> list[Row]:
    """The open incidents of `guardian/incidents.py`, with the title and data of their opening event."""
    keys = sorted(incidents.open_keys())
    if not keys:
        return []
    rows = db.fetch_all(
        """
        select distinct on (entity_id) entity_id as key, message as title, created_at as opened_at, data
        from events
        where type = %s and entity_type = %s and entity_id = any(%s)
        order by entity_id, id desc
        """,
        (incidents.OPENED, incidents.ENTITY, keys),
    )
    return sorted(rows, key=lambda row: row["opened_at"], reverse=True)


def heartbeats() -> list[Row]:
    stale_s = number(settings.get("guardian.heartbeat_stale_s", None))
    rows = db.fetch_all(
        "select name, last_seen, info, extract(epoch from now() - last_seen) as age_s from heartbeats "
        "order by name"
    )
    out = []
    for row in rows:
        name = str(row["name"])
        age_s = max(0, round(float(row["age_s"])))
        kind = "trigger" if name.startswith(TRIGGER_PREFIX) else "service"
        stale = kind == "service" and stale_s is not None and guardian_health.is_stale(age_s, stale_s)
        out.append(
            {
                "name": name,
                "kind": kind,
                "last_seen": row["last_seen"],
                "age_s": age_s,
                "stale": stale,
                "info": row["info"],
            }
        )
    return out


def triggers() -> list[dict[str, Any]]:
    """The four n8n triggers: last call, grace time (`guardian.failover_grace_s`), who fired it, stale."""
    grace = seconds_map("guardian.failover_grace_s")
    rows = db.fetch_all(
        "select name, last_seen, info, extract(epoch from now() - last_seen) as age_s from heartbeats "
        "where name = any(%s)",
        ([TRIGGER_PREFIX + name for name in TRIGGERS],),
    )
    seen = {str(row["name"]).removeprefix(TRIGGER_PREFIX): row for row in rows}
    out = []
    for name in TRIGGERS:
        row = seen.get(name)
        grace_s = grace.get(name)
        info = row["info"] if row is not None and isinstance(row["info"], dict) else {}
        age_s = float(row["age_s"]) if row is not None else None
        stale = grace_s is not None and (age_s is None or age_s > grace_s)
        out.append(
            {
                "name": name,
                "last_seen": row["last_seen"] if row is not None else None,
                "grace_s": grace_s,
                "source": (info.get("source") or "n8n") if row is not None else None,
                "stale": stale,
            }
        )
    return out


def job_totals() -> dict[str, int]:
    row = db.fetch_one(
        """
        select count(*) filter (where status = 'queued') as queued,
               count(*) filter (where status = 'running') as running,
               count(*) filter (where status = 'failed'
                                and updated_at >= now() - interval '24 hours') as failed_24h
        from jobs
        where status in ('queued', 'running')
           or (status = 'failed' and updated_at >= now() - interval '24 hours')
        """
    )
    assert row is not None
    return {key: int(row[key] or 0) for key in ("queued", "running", "failed_24h")}


# ── accounts and caps ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PostingView:
    """The posting state the planners would see now: accounts, params (None when the posting settings are
    broken), the 24 h history and the dry-run mode of a decision made now."""

    now: datetime
    rows: list[Row]
    accounts: list[Account]
    params: PostingParams | None
    history: list[PostRecord]
    dry: bool


def posting_view(now: datetime) -> PostingView:
    store = DbStore()
    rows = db.fetch_all(
        """
        select id, platform, upload_post_user, handle, lanes, cap_24h, spike_reserve, min_gap_s, privacy,
               hourly_hours_utc, enabled, paused_until, pause_reason
        from platform_accounts order by id
        """
    )
    accounts = [account_from_row(row) for row in rows]
    try:
        params: PostingParams | None = store.load_params()
    except (KeyError, TypeError, ValueError) as exc:
        get_logger("api").warning("admin_posting_params_unreadable", error=type(exc).__name__)
        params = None
    history = store.load_history(now)
    dry = True if params is None else posting_gate.is_dry(now, params)
    return PostingView(now, rows, accounts, params, history, dry)


def cap_row(view: PostingView, account: Account) -> dict[str, Any]:
    """The cap figures of `posting/caps.py` for one account (None values when the settings are broken)."""
    base: dict[str, Any] = {
        "account_id": account.id,
        "cap_24h": account.cap_24h,
        "paused_until": account.paused_until,
    }
    if view.params is None:
        return {
            **base,
            "used_24h": None,
            "free": None,
            "spike_reserve_remaining": None,
            "hourly_allowed": None,
            "spike_allowed": None,
            "gap_ok": None,
            "last_post_at": None,
        }
    state = posting_caps.cap_state(account, view.history, view.now, view.params, dry=view.dry)
    return {
        **base,
        "cap_24h": state.cap,
        "used_24h": state.used_24h,
        "free": state.free,
        "spike_reserve_remaining": state.reserve_left,
        "hourly_allowed": state.hourly_allowed,
        "spike_allowed": state.spike_allowed,
        "gap_ok": state.gap_ok,
        "last_post_at": state.last_post_at,
    }


def account_summaries(view: PostingView) -> list[dict[str, Any]]:
    out = []
    for row, account in zip(view.rows, view.accounts, strict=True):
        caps = cap_row(view, account)
        out.append(
            {
                "id": row["id"],
                "platform": row["platform"],
                "handle": row["handle"],
                "lanes": list(row["lanes"]),
                "enabled": row["enabled"],
                "paused_until": row["paused_until"],
                "pause_reason": row["pause_reason"],
                "cap_24h": row["cap_24h"],
                "spike_reserve": row["spike_reserve"],
                "used_24h": caps["used_24h"],
                "spike_reserve_remaining": caps["spike_reserve_remaining"],
                "last_post_at": caps["last_post_at"],
                "privacy": row["privacy"],
                "hourly_hours_utc": None
                if row["hourly_hours_utc"] is None
                else list(row["hourly_hours_utc"]),
                "min_gap_s": row["min_gap_s"],
            }
        )
    return out


# ── queue ────────────────────────────────────────────────────

QUEUE_COLUMNS = f"""
c.id::text as clip_id, c.series_no, c.hook, c.title, {creator_json()} as creator, c.lane, c.score,
c.rank_score, c.duration_s, c.created_at, c.approved_at, s.spike_wall
"""


def ranked_queue(view: PostingView) -> dict[str, list[dict[str, Any]]]:
    """The approved clips in the order the posting engine takes them (`selection.hourly_candidates`,
    `precedence.spike_order`), with the fields of a `QueueItem`."""
    store = DbStore()
    hourly_loaded = store.load_hourly_queue()
    spike_loaded = store.load_spike_clips()
    hourly = [c.clip for c in hourly_loaded]
    spikes = [c.clip for c in spike_loaded]
    if view.params is not None:
        hourly = hourly_candidates(hourly, view.now, view.params)
    spikes = spike_order(spikes)
    ids = [c.id for c in hourly] + [c.id for c in spikes]
    rows = (
        db.fetch_all(
            f"select {QUEUE_COLUMNS} from clips c join creators cr on cr.id = c.creator_id "
            "left join spikes s on s.id = c.spike_id where c.id = any(%s::uuid[])",
            (ids,),
        )
        if ids
        else []
    )
    by_id = {row["clip_id"]: row for row in rows}

    def item(clip_id: str) -> dict[str, Any] | None:
        row = by_id.get(clip_id)
        if row is None:
            return None
        expires_at = None
        if view.params is not None:
            if row["lane"] == "spike" and (row["spike_wall"] or row["approved_at"]) is not None:
                expires_at = (row["spike_wall"] or row["approved_at"]) + timedelta(
                    seconds=view.params.spike_max_age_s
                )
            elif row["approved_at"] is not None:
                expires_at = row["approved_at"] + timedelta(hours=view.params.max_clip_age_h)
        return {
            "clip_id": row["clip_id"],
            "series_no": row["series_no"],
            "hook": row["hook"],
            "title": row["title"],
            "creator": row["creator"],
            "lane": row["lane"],
            "score": row["score"],
            "rank_score": row["rank_score"],
            "duration_s": row["duration_s"],
            "created_at": row["created_at"],
            "age_s": max(0, round((view.now - row["created_at"]).total_seconds())),
            "expires_at": expires_at,
        }

    return {
        "hourly": [x for x in (item(c.id) for c in hourly) if x is not None],
        "spike": [x for x in (item(c.id) for c in spikes) if x is not None],
    }


def slot_grid(view: PostingView) -> list[dict[str, Any]]:
    """Today's 24 UTC hourly slots per account."""
    day_start = view.now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.fetch_all(
        """
        select id::text as id, account_id, slot_at, status, dry_run from posts
        where lane = 'hourly' and slot_at >= %s and slot_at < %s
        order by dry_run desc, created_at
        """,
        (day_start, day_start + timedelta(days=1)),
    )
    # Later rows win, and real rows come after dry-run rows (order above), so a real post is shown.
    posts: dict[tuple[str, int], Row] = {}
    for row in rows:
        posts[(row["account_id"], row["slot_at"].astimezone(UTC).hour)] = row
    current = view.now.astimezone(UTC).hour
    out = []
    for account in view.accounts:
        hours = []
        for hour in range(24):
            post = posts.get((account.id, hour))
            state = slot_state(
                hour,
                current,
                hour_allowed(account, hour),
                None if post is None else (str(post["status"]), bool(post["dry_run"])),
            )
            hours.append({"hour": hour, "state": state, "post_id": None if post is None else post["id"]})
        out.append({"account_id": account.id, "platform": account.platform.value, "hours": hours})
    return out


def next_slot_at(view: PostingView) -> datetime:
    """When the next hourly slot fires (its jittered time, as `posting/hourly.py` computes it)."""
    slot = next_slot(view.now)
    return fire_at(slot, view.params.jitter_max_s) if view.params is not None else slot


# ── overview pieces ──────────────────────────────────────────


def posts_24h(now: datetime) -> dict[str, Any]:
    rows = db.fetch_all(
        """
        select status, published_at from posts
        where not dry_run and status in ('published', 'failed')
          and coalesce(published_at, created_at) >= %s
        """,
        (slot_start(now) - timedelta(hours=23),),
    )
    since = now - timedelta(hours=24)
    published = [
        r["published_at"] for r in rows if r["status"] == "published" and r["published_at"] is not None
    ]
    return {
        "published": sum(1 for at in published if at >= since),
        "failed": db_count(
            "select count(*) as n from posts where not dry_run and status = 'failed' and created_at >= %s",
            (since,),
        ),
        "by_hour": hour_buckets(published, now),
    }


def db_count(sql: str, params: Sequence[Any] = ()) -> int:
    row = db.fetch_one(sql, params)
    return int(row["n"]) if row is not None and row["n"] is not None else 0


def express(since: datetime) -> dict[str, Any]:
    rows = db.fetch_all(
        "select detected_at as t, spike_to_post_s as s from spikes "
        "where spike_to_post_s is not null and detected_at >= %s order by detected_at",
        (since,),
    )
    values = [float(row["s"]) for row in rows]
    return {
        "p50_s": percentile(values, 0.5),
        "p90_s": percentile(values, 0.9),
        "series": [{"t": row["t"], "s": row["s"]} for row in rows],
    }


def spend_today(by_provider: Mapping[str, float]) -> dict[str, Any]:
    """Today's spend per budget of `guardian/health.py` (BUDGETS), then the providers without a budget."""
    lines: list[dict[str, Any]] = []
    covered: set[str] = set()
    for label, providers, key, _pauses in guardian_health.BUDGETS:
        covered.update(providers)
        lines.append(
            {
                "provider": label,
                "usd": round(sum(by_provider.get(p, 0.0) for p in providers), 4),
                "budget_usd": number(settings.get(key, None)),
            }
        )
    for provider in sorted(set(by_provider) - covered):
        lines.append({"provider": provider, "usd": round(by_provider[provider], 4), "budget_usd": None})
    budgets = [line["budget_usd"] for line in lines if line["budget_usd"] is not None]
    return {
        "total_usd": round(sum(by_provider.values()), 4),
        "budget_usd": round(sum(budgets), 4) if budgets else None,
        "by_provider": lines,
    }


def growth(now: datetime) -> dict[str, Any]:
    rows = db.fetch_all(
        """
        select a.id as account_id, a.platform, a.enabled,
          (select s.followers from account_stats s where s.account_id = a.id and s.followers is not null
            order by s.captured_at desc limit 1) as followers,
          (select s.followers from account_stats s where s.account_id = a.id and s.followers is not null
            and s.captured_at <= %s order by s.captured_at desc limit 1) as followers_before
        from platform_accounts a order by a.id
        """,
        (now - timedelta(hours=24),),
    )
    accounts = [
        {
            "account_id": row["account_id"],
            "platform": row["platform"],
            "followers": row["followers"],
            "delta_24h": None
            if row["followers"] is None or row["followers_before"] is None
            else int(row["followers"]) - int(row["followers_before"]),
        }
        for row in rows
    ]
    ypp = report_view._ypp(
        [{"platform": r["platform"], "enabled": r["enabled"], "followers": r["followers"]} for r in rows], now
    )
    return {
        "accounts": accounts,
        "ypp": {
            "subscribers": ypp["subscribers"],
            "subscribers_target": ypp["subscribers_target"],
            "shorts_views_90d": ypp["shorts_views_90d"],
            "views_target": ypp["shorts_views_target"],
        },
    }


def service_counts() -> tuple[int, int]:
    """Healthy and expected services, by the rules of the guardian health check."""
    expected = [str(name) for name in settings.get_list("guardian.expected_services", [])]
    stale_s = settings.get_float("guardian.heartbeat_stale_s")
    window_s = settings.get_float("guardian.restart_storm_window_s")
    states = guardian_health.service_health(
        guardian_health.load_beats(), expected, guardian_health.db_now(), stale_s, window_s
    )
    return sum(1 for state in states if not state.stale), len(states)


def overview() -> dict[str, Any]:
    snapshot = status_view.collect()
    now = datetime.fromisoformat(snapshot["generated_at"])
    view = posting_view(now)
    queue = ranked_queue(view)
    noisy = [str(t) for t in settings.get_list("admin.noisy_event_types", [])]
    recent = db.fetch_all(
        f"select {_event_columns()} from events e where e.level <> 'debug' and not (e.type = any(%s)) "
        "order by e.id desc limit %s",
        (noisy, RECENT_EVENTS),
    )
    services_ok, services_total = service_counts()
    trigger_rows = triggers()
    jobs = snapshot["jobs"]
    return {
        "now": now,
        "env": snapshot["env"],
        "posting_mode": snapshot["posting_mode"],
        "stop_reason": snapshot["stop_reason"],
        "launch_at": snapshot["launch_at"],
        "dry_run": snapshot["dry_run"],
        "posts_24h": posts_24h(now),
        "queue": {
            "hourly": snapshot["approved_queue"]["hourly"],
            "spike": snapshot["approved_queue"]["spike"],
            "low_watermark": snapshot["approved_queue"]["hourly_low_watermark"],
        },
        "express": express(now - timedelta(hours=24)),
        "spend": spend_today(snapshot["spend_today_usd"]["by_provider"]),
        "accounts": account_summaries(view),
        "up_next": (queue["spike"] + queue["hourly"])[:UP_NEXT],
        "recent_events": recent,
        "health": {
            "services_ok": services_ok,
            "services_total": services_total,
            "triggers_ok": sum(1 for t in trigger_rows if not t["stale"]),
            "triggers_total": len(trigger_rows),
            "open_incidents": open_incidents(),
            "jobs": {
                "queued": sum(jobs["queued"].values()),
                "running": sum(jobs["running"].values()),
                "failed_24h": sum(jobs["failed_last_24h"].values()),
            },
            "live": {
                "active": snapshot["live_sessions"]["active"],
                "max_slots": snapshot["live_sessions"]["max_slots"],
            },
        },
        "growth": growth(now),
    }


# ── pipeline ─────────────────────────────────────────────────


def pipeline(window: str, now: datetime) -> dict[str, Any]:
    since = now - WINDOWS[window]
    p = {"since": since}
    one = db.fetch_one(
        """
        select
          (select count(*) from candidates where checked_at >= %(since)s) as candidates,
          (select count(*) from candidates where checked_at >= %(since)s and picked) as picked,
          (select count(*) from live_sessions where status in ('assigned', 'recording')) as live_active,
          (select count(*) from live_sessions where assigned_at >= %(since)s) as live_sessions,
          (select count(*) from events where type = 'clip.rendered' and created_at >= %(since)s) as rendered,
          (select count(*) from events where type = 'clip.render_failed' and created_at >= %(since)s)
            as render_failed,
          (select count(*) from clips where approved_at >= %(since)s) as approved,
          (select count(*) from clips where status = 'rejected' and created_at >= %(since)s) as gate_rejected,
          (select count(*) from clips where status = 'approved') as waiting,
          (select count(*) from events where type = 'clip.expired' and created_at >= %(since)s) as expired,
          (select count(*) from posts
            where not dry_run and status = 'published' and published_at >= %(since)s)
            as published,
          (select count(*) from posts where not dry_run and status = 'failed' and created_at >= %(since)s)
            as post_failed
        """,
        p,
    )
    assert one is not None
    vod = dict(
        shape_counts(
            (
                (r["status"], int(r["n"]))
                for r in db.fetch_all(
                    "select status, count(*) as n from videos where created_at >= %(since)s group by status",
                    p,
                )
            ),
            VIDEO_STATUSES,
        )
    )
    vod.pop("all", None)
    spikes = db.fetch_one(
        """
        select count(*) as detected,
               count(*) filter (where status in ('confirmed', 'rendering', 'clipped')) as confirmed,
               count(*) filter (where status = 'rejected') as rejected,
               count(*) filter (where status = 'merged') as merged
        from spikes where detected_at >= %(since)s
        """,
        p,
    )
    assert spikes is not None
    funnel_rows = db.fetch_all(
        """
        select sp.tier, count(*) as detected,
               count(*) filter (where sp.status in ('confirmed', 'rendering', 'clipped')) as confirmed,
               count(*) filter (where sp.clip_id is not null) as clipped,
               count(*) filter (where exists (
                 select 1 from posts po
                 where po.clip_id = sp.clip_id and not po.dry_run and po.status = 'published'
               )) as posted
        from spikes sp where sp.detected_at >= %(since)s and sp.tier = any(%(tiers)s)
        group by sp.tier
        """,
        {**p, "tiers": list(SPIKE_TIERS)},
    )
    by_tier = {row["tier"]: row for row in funnel_rows}
    funnel = [
        {
            "tier": tier,
            **{
                k: int(by_tier[tier][k]) if tier in by_tier else 0
                for k in ("detected", "confirmed", "clipped", "posted")
            },
        }
        for tier in SPIKE_TIERS
    ]
    rejects = db.fetch_all(
        """
        select coalesce(reject_reason, 'unknown') as reason, count(*) as n from clips
        where status = 'rejected' and created_at >= %(since)s
        group by 1 order by n desc, reason limit %(limit)s
        """,
        {**p, "limit": REJECT_REASONS},
    )
    stages = db.fetch_one(
        """
        with chain as (
          select extract(epoch from sp.detected_at - sp.spike_wall) as detect,
                 extract(epoch from c.created_at - sp.detected_at) as queue,
                 c.render_ms / 1000.0 as render,
                 extract(epoch from c.approved_at - c.created_at) - coalesce(c.render_ms, 0) / 1000.0 as gate,
                 extract(epoch from (select min(po.published_at) from posts po
                                     where po.clip_id = c.id and not po.dry_run and po.status = 'published')
                                    - c.approved_at) as post
          from clips c join spikes sp on sp.id = c.spike_id
          where c.lane = 'spike' and c.created_at >= %(since)s
        )
        select percentile_cont(0.5) within group (order by detect) as detect,
               percentile_cont(0.5) within group (order by queue) as queue,
               percentile_cont(0.5) within group (order by render) as render,
               percentile_cont(0.5) within group (order by gate) as gate,
               percentile_cont(0.5) within group (order by post) as post
        from chain
        """,
        p,
    )
    assert stages is not None
    approved_by_hour = db.fetch_all(
        """
        select date_trunc('hour', approved_at, 'UTC') as t, count(*) as n from clips
        where approved_at >= %(since)s group by 1 order by 1
        """,
        p,
    )
    max_slots = settings.get("live.max_slots", None)
    return {
        "window": window,
        "nodes": {
            "discovery": {"candidates": one["candidates"], "picked": one["picked"]},
            "live": {
                "active": one["live_active"],
                "max_slots": max_slots if isinstance(max_slots, int) else None,
                "sessions": one["live_sessions"],
            },
            "vod": vod,
            "spikes": dict(spikes),
            "render": {"rendered": one["rendered"], "failed": one["render_failed"]},
            "gate": {"approved": one["approved"], "rejected": one["gate_rejected"]},
            "queue": {"waiting": one["waiting"], "expired": one["expired"]},
            "posts": {"published": one["published"], "failed": one["post_failed"]},
        },
        "funnel": funnel,
        "rejects": rejects,
        "express_stages": [
            {"stage": stage, "median_s": None if stages[stage] is None else round(float(stages[stage]), 1)}
            for stage in ("detect", "queue", "render", "gate", "post")
        ],
        "express_series": express(since)["series"],
        "approved_by_hour": approved_by_hour,
    }


# ── details ──────────────────────────────────────────────────


def fetch_detail(columns: str, source: str, id_col: str, row_id: str, extra: str = "") -> Row | None:
    sql = f"select {columns}{', ' + extra if extra else ''} from {source} where {id_col} = %s"
    return db.fetch_one(sql, (row_id,))


def blocked_live_platforms(now: datetime) -> dict[str, str]:
    """Live platforms in back-off (`live.source_health` entries whose `skip_until` is in the future)."""
    raw = settings.get("live.source_health", None)
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        until = entry.get("skip_until") if isinstance(entry, dict) else None
        if not isinstance(until, str):
            continue
        try:
            moment = datetime.fromisoformat(until)
        except ValueError:
            continue
        moment = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
        if moment > now:
            out[str(name)] = moment.astimezone(UTC).isoformat()
    return out


def spikes_of(column: str, row_id: str) -> list[Row]:
    return db.fetch_all(
        f"select {SPIKE_COLUMNS} from {SPIKE_SOURCE} where sp.{column} = %s order by sp.detected_at",
        (row_id,),
    )


def posts_where(sql: str, params: Sequence[Any], limit: int = 200) -> list[Row]:
    return db.fetch_all(
        f"select {POST_COLUMNS} from {POST_SOURCE} where {sql} "
        "order by p.created_at desc, p.id desc limit %s",
        [*params, limit],
    )


def search(q: str) -> list[dict[str, Any]]:
    """Up to `SEARCH_MAX_RESULTS` matches for the console's search box."""
    query = classify_search(q)
    if not query.text:
        return []
    results: list[dict[str, Any]] = []

    def add(rows: Iterable[Row]) -> None:
        for row in rows:
            if len(results) < SEARCH_MAX_RESULTS:
                results.append(dict(row))

    clip_label = "coalesce('#' || series_no || ' ', '') || coalesce(hook, title, '')"
    if query.series_no is not None:
        add(
            db.fetch_all(
                f"select 'clip' as kind, id::text as id, {clip_label} as label, status as sub, "
                "'/clips/' || id as href from clips where series_no = %s limit %s",
                (query.series_no, SEARCH_MAX_RESULTS),
            )
        )
    if query.uuid_prefix is not None:
        pattern = like_escape(query.uuid_prefix) + "%"
        tables = (
            ("clip", f"select id, {clip_label} as label, status as sub from clips", "/clips/"),
            (
                "spike",
                "select id, mode || ' spike ' || coalesce(tier, '?') as label, status as sub from spikes",
                "/spikes/",
            ),
            ("post", "select id, account_id as label, status as sub from posts", "/posts/"),
            (
                "video",
                "select id, coalesce(title, source_id) as label, status as sub from videos",
                "/videos/",
            ),
            (
                "live_session",
                "select id, channel_login as label, status as sub from live_sessions",
                "/live/",
            ),
            (
                "takedown",
                "select id, coalesce(subject, sender, source) as label, status as sub from takedowns",
                "/guardian/takedowns/",
            ),
        )
        for kind, select, href in tables:
            add(
                db.fetch_all(
                    f"select %s as kind, x.id::text as id, x.label, x.sub, %s || x.id as href "
                    f"from ({select}) x where x.id::text like %s limit %s",
                    (kind, href, pattern, SEARCH_MAX_RESULTS),
                )
            )
    contains = "%" + like_escape(query.text) + "%"
    add(
        db.fetch_all(
            "select 'creator' as kind, id::text as id, handle as label, platform || ' · ' || tier as sub, "
            "'/creators/' || id as href from creators where handle ilike %s or display_name ilike %s "
            "order by handle limit %s",
            (contains, contains, SEARCH_MAX_RESULTS),
        )
    )
    add(
        db.fetch_all(
            "select 'post' as kind, id::text as id, account_id as label, post_url as sub, "
            "'/posts/' || id as href from posts where post_url ilike %s order by created_at desc limit %s",
            (contains, SEARCH_MAX_RESULTS),
        )
    )
    add(
        db.fetch_all(
            "select 'setting' as kind, key as id, key as label, description as sub, "
            "'/settings?key=' || key as href from settings where key ilike %s order by key limit %s",
            (like_escape(query.text) + "%", SEARCH_MAX_RESULTS),
        )
    )
    return results


# ── page views ───────────────────────────────────────────────


def fresh_settings(keys: Sequence[str]) -> dict[str, Any]:
    """Settings read from the table now, not from the 30 s cache (a stop set a second ago must show)."""
    rows = db.fetch_all("select key, value from settings where key = any(%s)", (list(keys),))
    return {row["key"]: row["value"] for row in rows}


def guardian_view(now: datetime) -> dict[str, Any]:
    limit = settings.get_int("guardian.strike_limit")
    window_d = settings.get_float("guardian.strike_window_d")
    since = guardian_stop.window_start(now, window_d, guardian_stop.last_stop_at())
    strikes = db.fetch_all(
        f"""
        select t.received_at as at, {creator_json(nullable=True)} as creator, t.id::text as takedown_id
        from takedowns t left join creators cr on cr.id = t.creator_id
        where t.is_takedown and t.status in ('actioned', 'partial')
          and coalesce((t.actions->>'strike')::boolean, false) and t.received_at >= %s
        order by t.received_at desc
        """,
        (since,),
    )
    stop = fresh_settings([guardian_stop.STOP_KEY, guardian_stop.REASON_KEY])
    claims = db.fetch_one(
        """
        select count(*) filter (where claim_status = 'ok') as ok,
               count(*) filter (where claim_status = 'suspect') as suspect,
               count(*) filter (where status = 'published' and claim_status is null) as unverifiable,
               count(*) filter (where claim_status = 'removed_by_platform') as removed_by_platform
        from posts where not dry_run
        """
    )
    assert claims is not None
    beat = db.fetch_one("select last_seen from heartbeats where name = %s", (TRIGGER_PREFIX + "guardian",))
    takedown_rows = db.fetch_all("select status, count(*) as n from takedowns group by 1")
    takedowns = shape_counts(((r["status"], int(r["n"])) for r in takedown_rows), TAKEDOWN_STATUSES)
    takedowns.pop("all", None)
    reason = stop.get(guardian_stop.REASON_KEY)
    return {
        "strikes": {"count": len(strikes), "limit": limit, "window_d": window_d, "items": strikes},
        "emergency_stop": stop.get(guardian_stop.STOP_KEY) is True,
        "stop_reason": reason if isinstance(reason, str) else None,
        "manual_removals": posts_where("p.status = 'remove_failed' and not p.dry_run", ()),
        "claims": {"last_run_at": beat["last_seen"] if beat is not None else None, **dict(claims)},
        "takedown_counts": takedowns,
    }


def quotas() -> dict[str, Any]:
    """Today's quota use of the discovery sources, counted the way `discovery/store.py` counts it."""
    apify_day, apify_month = discovery_store.apify_spend()
    return {
        "youtube_units_today": discovery_store.youtube_units_today(),
        "youtube_units_limit": settings.get("discovery.youtube_quota_daily_units", None),
        "search_calls_today": discovery_store.youtube_search_calls_today(),
        "search_calls_limit": settings.get("discovery.search_api_daily_calls", None),
        "apify_today_usd": round(apify_day, 4),
        "apify_daily_budget_usd": number(settings.get("discovery.apify_daily_budget_usd", None)),
        "apify_month_usd": round(apify_month, 4),
        "apify_monthly_budget_usd": number(settings.get("discovery.apify_monthly_budget_usd", None)),
    }


def discovery_view() -> dict[str, Any]:
    values = fresh_settings(
        ["discovery.source_health", "live.source_health", "vod.source_health", "discovery.apify_last_run"]
    )
    return {
        "runs": events_of_types(("discovery.run", "discovery.failed"), DISCOVERY_RUNS),
        "source_health": {
            "discovery": values.get("discovery.source_health"),
            "live": values.get("live.source_health"),
            "vod": values.get("vod.source_health"),
        },
        "apify_last_run": values.get("discovery.apify_last_run"),
        "quotas": quotas(),
    }


def health_view() -> dict[str, Any]:
    return {
        "heartbeats": heartbeats(),
        "triggers": triggers(),
        "open_incidents": open_incidents(),
        "incidents": db.fetch_all(
            f"select {_event_columns()} from events e where e.type like 'health.%%' "
            "order by e.id desc limit %s",
            (EVENTS_PER_ENTITY,),
        ),
        "jobs": job_totals(),
    }


USAGE_RANGES = ("today", "7d", "month")
_USAGE_START = {
    "today": "date_trunc('day', now(), 'UTC')",
    "7d": "date_trunc('day', now(), 'UTC') - interval '6 days'",
    "month": "date_trunc('month', now(), 'UTC')",
}


def usage_view(range_name: str) -> dict[str, Any]:
    where = f"created_at >= {_USAGE_START[range_name]}"
    totals = db.fetch_all(
        f"select provider, round(sum(cost_usd), 4) as usd from api_usage where {where} "
        "group by 1 order by 2 desc, 1"
    )
    daily = db.fetch_all(
        f"select (created_at at time zone 'UTC')::date as day, provider, round(sum(cost_usd), 4) as usd "
        f"from api_usage where {where} group by 1, 2 order by 1, 2"
    )
    operations = db.fetch_all(
        f"""
        select provider, operation, model, count(*) as calls, sum(input_tokens) as input_tokens,
               sum(output_tokens) as output_tokens, sum(units) as units, round(sum(cost_usd), 4) as usd
        from api_usage where {where} group by 1, 2, 3 order by usd desc, calls desc
        """
    )
    return {
        "range": range_name,
        "totals": totals,
        "budgets": {
            "anthropic": number(settings.get("ai.daily_budget_usd", None)),
            "transcribe": number(settings.get("transcribe.daily_budget_usd", None)),
            "apify_daily": number(settings.get("discovery.apify_daily_budget_usd", None)),
            "apify_monthly": number(settings.get("discovery.apify_monthly_budget_usd", None)),
            "month_ceiling": number(settings.get("admin.month_ceiling_usd", None)),
        },
        "daily": daily,
        "operations": operations,
        "quotas": quotas(),
    }


def settings_rows() -> list[dict[str, Any]]:
    rows = db.fetch_all("select key, value, description, updated_at from settings order by key")
    return [
        {**row, "group": settings_group(str(row["key"])), "written_by_code": row["key"] in WRITTEN_BY_CODE}
        for row in rows
    ]


def account_detail(account_id: str, now: datetime) -> dict[str, Any] | None:
    view = posting_view(now)
    summary = next((item for item in account_summaries(view) if item["id"] == account_id), None)
    if summary is None:
        return None
    account = next(a for a in view.accounts if a.id == account_id)
    stats = db.fetch_all(
        """
        select captured_at, followers, views_total from account_stats
        where account_id = %s and captured_at >= %s order by captured_at
        """,
        (account_id, now - timedelta(days=ACCOUNT_STATS_DAYS)),
    )
    return {
        **summary,
        "caps": cap_row(view, account),
        "stats": stats,
        "views_by_day": daily_deltas((row["captured_at"], row["views_total"]) for row in stats),
        "posts": posts_where("p.account_id = %s", (account_id,), ACCOUNT_POSTS),
        "events": entity_events("account", account_id),
    }


def creator_detail(creator_id: str) -> dict[str, Any] | None:
    row = db.fetch_one(f"select {CREATOR_COLUMNS} from creators cr where cr.id = %s", (creator_id,))
    if row is None:
        return None
    row["layout"] = db.fetch_one(
        """
        select source_width, source_height, facecam_box, gameplay_box, confidence, detected_from,
               detected_at, expires_at
        from creator_layouts where creator_id = %s
        """,
        (creator_id,),
    )
    row["counts"] = db.fetch_one(
        """
        select (select count(*) from live_sessions where creator_id = %(id)s) as live_sessions,
               (select count(*) from videos where creator_id = %(id)s) as videos,
               (select count(*) from spikes where creator_id = %(id)s) as spikes,
               (select count(*) from clips where creator_id = %(id)s) as clips,
               (select count(*) from posts p join clips c on c.id = p.clip_id
                 where c.creator_id = %(id)s) as posts,
               (select count(*) from takedowns where creator_id = %(id)s) as takedowns
        """,
        {"id": creator_id},
    )
    row["clips"] = db.fetch_all(
        f"select {CLIP_COLUMNS} from {CLIP_SOURCE} where c.creator_id = %s "
        "order by c.created_at desc, c.id desc limit %s",
        (creator_id, CREATOR_CLIPS),
    )
    row["events"] = entity_events("creator", creator_id)
    row["trace"] = build_trace(creator_id=creator_id)
    return row


def event_detail(event_id: int) -> dict[str, Any] | None:
    row = db.fetch_one(f"select {_event_columns()} from events e where e.id = %s", (event_id,))
    if row is None:
        return None
    related: list[Row] = []
    if row["entity_type"] is not None and row["entity_id"] is not None:
        related = db.fetch_all(
            f"""
            select {_event_columns()} from events e
            where e.entity_type = %s and e.entity_id = %s and e.id <> %s
              and e.created_at between %s and %s
            order by e.id desc limit %s
            """,
            (
                row["entity_type"],
                row["entity_id"],
                event_id,
                row["created_at"] - RELATED_WINDOW,
                row["created_at"] + RELATED_WINDOW,
                EVENTS_PER_ENTITY,
            ),
        )
    return {**row, "related": related}


def jobs_by_queue() -> dict[str, dict[str, int]]:
    rows = db.fetch_all("select queue, status, count(*) as n from jobs group by 1, 2")
    out: dict[str, dict[str, int]] = {queue: {status: 0 for status in JOB_STATUSES} for queue in JOB_QUEUES}
    for row in rows:
        out.setdefault(str(row["queue"]), {})[str(row["status"])] = int(row["n"])
    return out

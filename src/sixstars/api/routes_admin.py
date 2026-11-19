"""Read-only routes of the admin console: `GET /v1/admin/*` (docs/26, M17).

Every route runs its queries in one read-only transaction with a statement timeout from
`admin.statement_timeout_ms`; a query that runs longer answers 504. Lists are keyset-paginated (`limit`,
`cursor`) and carry `counts` for the route's main filter. Unknown or malformed ids answer 404. The SQL
and the shaping live in `admin_queries.py`; this module only maps parameters and answers.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from sixstars import db, settings
from sixstars.api import admin_queries as q
from sixstars.api.errors import ApiError
from sixstars.clients._http import ApiError as ClientApiError
from sixstars.clients._http import ClientError, NotConfiguredError
from sixstars.logging import get_logger
from sixstars.storage import SIGNED_URL_TTL_S, ClipStorage

router = APIRouter(prefix="/v1/admin")

# Replaced in tests with `FakeClipStorage`.
storage_factory: Callable[[], ClipStorage] = ClipStorage
MISSING_OBJECT_STATUSES = frozenset(
    {400, 404}
)  # Storage answers 400 "not found" when signing a missing object

Limit = Annotated[int | None, Query(ge=1)]
Cursor = Annotated[str | None, Query(max_length=512)]


@contextmanager
def read_tx() -> Iterator[None]:
    """One read-only transaction with the admin statement timeout."""
    timeout_ms = settings.get_int("admin.statement_timeout_ms")
    try:
        with db.transaction():
            db.execute("set transaction read only")
            db.execute("select set_config('statement_timeout', %s, true)", (str(max(1, timeout_ms)),))
            yield
    except psycopg.errors.QueryCanceled as exc:
        get_logger("api").warning("admin_query_timeout", timeout_ms=timeout_ms)
        raise ApiError(504, "timeout", "the query took too long; narrow the filters") from exc


def sign_thumbs(keys: list[str]) -> dict[str, str | None]:
    """Signed URLs (1 h) for the clip frames of one answer, in one Storage request. Any failure gives
    `{}`: every row then falls back to its source thumbnail, the answer itself never fails."""
    if not keys:
        return {}
    try:
        return storage_factory().signed_urls(keys, SIGNED_URL_TTL_S)
    except Exception as exc:
        get_logger("api").warning("admin_thumbs_sign_failed", count=len(keys), error=type(exc).__name__)
        return {}


def answer(data: Any) -> JSONResponse:
    q.resolve_thumbs(data, sign_thumbs(q.thumb_keys(data)))
    return JSONResponse(q.jsonify(data))


def _now() -> datetime:
    return datetime.now(UTC)


def page(
    name: str,
    filters: list[q.Filter],
    status: str | None,
    limit: int | None,
    cursor: str | None,
    meta: Callable[[], dict[str, Any]] | None = None,
) -> JSONResponse:
    max_limit = settings.get_int("admin.list_max_limit")
    with read_tx():
        body = q.run_list(q.LISTS[name], filters, status, q.clamp_limit(limit, max_limit), cursor)
        if meta is not None:
            body["meta"] = meta()
    return answer(body)


def choice(name: str, value: str | None, allowed: tuple[str, ...]) -> str | None:
    if value is not None and value not in allowed:
        raise q.bad_filter(name, value)
    return value


def uuid_filter(name: str, value: str | None) -> str | None:
    if value is not None and not q.is_uuid(value):
        raise q.bad_filter(name, value)
    return value


def detail(row: q.Row | None, what: str) -> q.Row:
    if row is None:
        raise q.not_found(what)
    return row


# ── overview, pipeline ───────────────────────────────────────


@router.get("/overview")
def overview() -> JSONResponse:
    with read_tx():
        body = q.overview()
    return answer(body)  # outside the transaction: thumbnails are signed over the network


@router.get("/pipeline")
def pipeline(window: Literal["24h", "7d", "30d"] = "24h") -> JSONResponse:
    with read_tx():
        return answer(q.pipeline(window, _now()))


# ── live sessions ────────────────────────────────────────────


@router.get("/live-sessions")
def live_sessions(status: str | None = None, limit: Limit = None, cursor: Cursor = None) -> JSONResponse:
    def meta() -> dict[str, Any]:
        max_slots = settings.get("live.max_slots", None)
        return {
            "max_slots": max_slots if isinstance(max_slots, int) else None,
            "blocked": q.blocked_live_platforms(_now()),
            # The whole record (since, error, access, blocks): a platform stays refused between back-offs.
            "source_health": q.fresh_settings(["live.source_health"]).get("live.source_health"),
        }

    return page("live-sessions", [], status, limit, cursor, meta)


@router.get("/live-sessions/{session_id}")
def live_session(session_id: str) -> JSONResponse:
    q.require_uuid(session_id, "live session")
    with read_tx():
        row = detail(
            q.fetch_detail(
                q.LIVE_COLUMNS,
                q.LIVE_SOURCE,
                "s.id",
                session_id,
                "s.bot_names, s.clipped_ranges, s.vod_source_id, s.buffer_dir",
            ),
            "live session",
        )
        # `spikes` keeps the S/A/B counts of the list item; the spikes themselves go in `spikes_list`.
        row["spikes_list"] = q.spikes_of("live_session_id", session_id)
        row["events"] = q.entity_events("live_session", session_id)
        row["trace"] = q.build_trace(live_session_id=session_id)
    return answer(row)


# ── videos ───────────────────────────────────────────────────


@router.get("/videos")
def videos(
    status: str | None = None, platform: str | None = None, limit: Limit = None, cursor: Cursor = None
) -> JSONResponse:
    filters: list[q.Filter] = []
    if choice("platform", platform, ("twitch", "youtube")):
        filters.append(("v.platform = %s", (platform,)))

    def meta() -> dict[str, Any]:
        return {"source_health": q.fresh_settings(["vod.source_health"]).get("vod.source_health")}

    return page("videos", filters, status, limit, cursor, meta)


@router.get("/videos/{video_id}")
def video(video_id: str) -> JSONResponse:
    q.require_uuid(video_id, "video")
    with read_tx():
        row = detail(q.fetch_detail(q.VIDEO_COLUMNS, q.VIDEO_SOURCE, "v.id", video_id), "video")
        row["chat_file"] = db.fetch_one(
            """
            select messages_count, duration_s, baseline_rate, bot_names, storage_path, downloaded_at
            from chat_files where video_id = %s order by downloaded_at desc limit 1
            """,
            (video_id,),
        )
        row["spikes"] = q.spikes_of("video_id", video_id)
        row["jobs"] = db.fetch_all(
            f"select {q.JOB_COLUMNS} from jobs j where j.payload->>'video_id' = %s "
            "order by j.id desc limit %s",
            (video_id, q.EVENTS_PER_ENTITY),
        )
        row["events"] = q.entity_events("video", video_id)
        row["trace"] = q.build_trace(video_id=video_id)
    return answer(row)


# ── spikes ───────────────────────────────────────────────────


@router.get("/spikes")
def spikes(
    status: str | None = None,
    tier: str | None = None,
    mode: str | None = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> JSONResponse:
    filters: list[q.Filter] = []
    if choice("tier", tier, ("S", "A", "B", "rejected")):
        filters.append(("sp.tier = %s", (tier,)))
    if choice("mode", mode, ("live", "replay", "transcript")):
        filters.append(("sp.mode = %s", (mode,)))
    return page("spikes", filters, status, limit, cursor)


@router.get("/spikes/{spike_id}")
def spike(spike_id: str) -> JSONResponse:
    q.require_uuid(spike_id, "spike")
    with read_tx():
        row = detail(
            q.fetch_detail(
                q.SPIKE_COLUMNS,
                q.SPIKE_SOURCE,
                "sp.id",
                spike_id,
                "sp.chat_sample, sp.msgs_in_bucket, sp.unique_chatters, sp.reaction_count, sp.clip_requests",
            ),
            "spike",
        )
        row["events"] = q.entity_events("spike", spike_id)
        row["trace"] = q.build_trace(spike_id=spike_id)
    return answer(row)


# ── clips ────────────────────────────────────────────────────

CLIP_DETAIL_COLUMNS = """
c.caption, c.hashtags, c.commentary, c.transcript, c.keywords, c.safety_flags, c.score_parts, c.spoiler,
c.profanity_count, c.gate_result, c.precheck_result, c.render_ms, c.file_size_bytes, c.storage_path,
c.start_s, c.end_s, c.original_language, c.style_id, c.layout_kind, c.style_plan
"""


@router.get("/clips")
def clips(
    status: str | None = None,
    lane: str | None = None,
    creator_id: str | None = None,
    q_: Annotated[str | None, Query(alias="q", max_length=200)] = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> JSONResponse:
    filters: list[q.Filter] = []
    if choice("lane", lane, ("spike", "hourly")):
        filters.append(("c.lane = %s", (lane,)))
    if uuid_filter("creator_id", creator_id):
        filters.append(("c.creator_id = %s", (creator_id,)))
    text = (q_ or "").strip()
    if text:
        pattern = "%" + q.like_escape(text) + "%"
        filters.append(
            ("(c.hook ilike %s or c.title ilike %s or c.topic ilike %s)", (pattern, pattern, pattern))
        )
    return page("clips", filters, status, limit, cursor)


@router.get("/clips/{clip_id}")
def clip(clip_id: str) -> JSONResponse:
    q.require_uuid(clip_id, "clip")
    with read_tx():
        row = detail(
            q.fetch_detail(q.CLIP_COLUMNS, q.CLIP_SOURCE, "c.id", clip_id, CLIP_DETAIL_COLUMNS), "clip"
        )
        row["posts"] = q.posts_where("p.clip_id = %s", (clip_id,))
        row["renders"] = q.clip_renders(clip_id)
        row["family"] = q.clip_family(clip_id)
        row["events"] = q.entity_events("clip", clip_id)
        row["trace"] = q.build_trace(clip_id=clip_id)
    return answer(row)


def media_answer(reason: str, url: str | None = None, expires_at: datetime | None = None) -> JSONResponse:
    return answer({"url": url, "expires_at": expires_at, "reason": reason})


@router.get("/clips/{clip_id}/media")
def clip_media(clip_id: str, render: Annotated[str | None, Query()] = None) -> JSONResponse:
    """A signed URL (1 h) for the rendered clip, or for one of its design-style renders (`render`, a
    `clip_renders.id`), or why there is none."""
    q.require_uuid(clip_id, "clip")
    if render is not None:
        q.require_uuid(render, "render")
    with read_tx():
        row = detail(db.fetch_one("select storage_path from clips where id = %s", (clip_id,)), "clip")
        if render is not None:
            row = detail(q.render_storage_path(clip_id, render), "render")
    key = row["storage_path"]
    if not key:
        return media_answer("no_file")
    log = get_logger("api")
    try:
        url = storage_factory().signed_url(str(key), SIGNED_URL_TTL_S)
    except NotConfiguredError as exc:
        raise ApiError(503, "storage_not_configured", "Supabase Storage is not configured") from exc
    except ClientApiError as exc:
        if exc.status in MISSING_OBJECT_STATUSES:
            return media_answer("deleted")
        log.warning("admin_media_sign_failed", clip_id=clip_id, status=exc.status)
        raise ApiError(503, "storage_unavailable", "Supabase Storage did not sign the URL") from exc
    except ClientError as exc:
        log.warning("admin_media_sign_failed", clip_id=clip_id, error=type(exc).__name__)
        raise ApiError(503, "storage_unavailable", "Supabase Storage did not sign the URL") from exc
    except ValueError:
        return media_answer("no_file")  # not a storage key
    return media_answer("ok", url, _now() + timedelta(seconds=SIGNED_URL_TTL_S))


@router.get("/styles")
def styles() -> JSONResponse:
    """The design styles and how each performs per account (development-plan/M18 Phase D)."""
    with read_tx():
        body = q.style_performance()
    return answer(body)


# ── queue, posts ─────────────────────────────────────────────


@router.get("/queue")
def queue() -> JSONResponse:
    with read_tx():
        view = q.posting_view(_now())
        body = {
            "ranked": q.ranked_queue(view),
            "slots": q.slot_grid(view),
            "caps": [q.cap_row(view, account) for account in view.accounts],
            "decisions": q.events_of_types(q.DECISION_TYPES, q.DECISIONS),
            "next_slot_at": q.next_slot_at(view),
        }
    return answer(body)


@router.get("/posts")
def posts(
    status: str | None = None,
    account_id: Annotated[str | None, Query(max_length=100)] = None,
    lane: str | None = None,
    clip_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> JSONResponse:
    filters: list[q.Filter] = []
    if account_id:
        filters.append(("p.account_id = %s", (account_id,)))
    if clip_id:
        if not q.is_uuid(clip_id):
            raise q.bad_filter("clip_id", clip_id)
        filters.append(("p.clip_id = %s", (clip_id,)))
    if choice("lane", lane, ("spike", "hourly")):
        filters.append(("p.lane = %s", (lane,)))
    return page("posts", filters, status, limit, cursor)


@router.get("/posts/{post_id}")
def post(post_id: str) -> JSONResponse:
    q.require_uuid(post_id, "post")
    with read_tx():
        row = detail(
            q.fetch_detail(
                q.POST_COLUMNS,
                q.POST_SOURCE,
                "p.id",
                post_id,
                "p.upload_request_id, p.idempotency_key, p.stats_updated_at, p.claim_checked_at, "
                "p.removed_at, p.remove_reason",
            ),
            "post",
        )
        row["events"] = q.entity_events("post", post_id)
        row["trace"] = q.build_trace(clip_id=row["clip_id"])
    return answer(row)


# ── creators, accounts, discovery, candidates ────────────────


@router.get("/creators")
def creators(
    tier: str | None = None,
    platform: str | None = None,
    q_: Annotated[str | None, Query(alias="q", max_length=200)] = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> JSONResponse:
    filters: list[q.Filter] = []
    if choice("platform", platform, ("twitch", "youtube")):
        filters.append(("cr.platform = %s", (platform,)))
    text = (q_ or "").strip()
    if text:
        pattern = "%" + q.like_escape(text) + "%"
        filters.append(("(cr.handle ilike %s or cr.display_name ilike %s)", (pattern, pattern)))
    return page("creators", filters, tier, limit, cursor)


@router.get("/creators/{creator_id}")
def creator(creator_id: str) -> JSONResponse:
    q.require_uuid(creator_id, "creator")
    with read_tx():
        row = detail(q.creator_detail(creator_id), "creator")
    return answer(row)


@router.get("/accounts")
def accounts() -> JSONResponse:
    with read_tx():
        return answer({"items": q.account_summaries(q.posting_view(_now()))})


@router.get("/accounts/{account_id}")
def account(account_id: str) -> JSONResponse:
    with read_tx():
        row = detail(q.account_detail(account_id, _now()), "account")
    return answer(row)


@router.get("/discovery")
def discovery() -> JSONResponse:
    with read_tx():
        return answer(q.discovery_view())


@router.get("/candidates")
def candidates(
    source_type: str | None = None, picked: bool | None = None, limit: Limit = None, cursor: Cursor = None
) -> JSONResponse:
    filters: list[q.Filter] = []
    if picked is not None:
        filters.append(("ca.picked = %s", (picked,)))
    return page("candidates", filters, source_type, limit, cursor)


# ── guardian, takedowns ──────────────────────────────────────


@router.get("/guardian")
def guardian() -> JSONResponse:
    with read_tx():
        return answer(q.guardian_view(_now()))


@router.get("/takedowns")
def takedowns(status: str | None = None, limit: Limit = None, cursor: Cursor = None) -> JSONResponse:
    return page("takedowns", [], status, limit, cursor)


@router.get("/takedowns/{takedown_id}")
def takedown(takedown_id: str) -> JSONResponse:
    q.require_uuid(takedown_id, "takedown")
    with read_tx():
        row = detail(q.fetch_detail(q.TAKEDOWN_COLUMNS, q.TAKEDOWN_SOURCE, "t.id", takedown_id), "takedown")
        ids = [str(item) for item in row["matched_post_ids"] or []]
        row["matched_posts"] = q.posts_where("p.id = any(%s::uuid[])", (ids,)) if ids else []
        row["events"] = q.entity_events("takedown", takedown_id)
    return answer(row)


# ── operations ───────────────────────────────────────────────


@router.get("/health")
def health() -> JSONResponse:
    with read_tx():
        return answer(q.health_view())


@router.get("/alerts")
def alerts(
    severity: str | None = None, delivered: bool | None = None, limit: Limit = None, cursor: Cursor = None
) -> JSONResponse:
    filters: list[q.Filter] = []
    if delivered is not None:
        filters.append(("(o.sent_at is not null) = %s", (delivered,)))
    return page("alerts", filters, severity, limit, cursor)


@router.get("/jobs")
def jobs(
    status: str | None = None,
    queue: str | None = None,
    type: Annotated[str | None, Query(max_length=100)] = None,  # noqa: A002
    limit: Limit = None,
    cursor: Cursor = None,
) -> JSONResponse:
    filters: list[q.Filter] = []
    if choice("queue", queue, q.JOB_QUEUES):
        filters.append(("j.queue = %s", (queue,)))
    if type:
        filters.append(("j.type = %s", (type,)))
    return page("jobs", filters, status, limit, cursor, lambda: {"by_queue": q.jobs_by_queue()})


@router.get("/jobs/{job_id}")
def job(job_id: str) -> JSONResponse:
    if not job_id.isdigit() or len(job_id) > 18:
        raise q.not_found("job")
    with read_tx():
        row = detail(
            db.fetch_one(f"select {q.JOB_COLUMNS}, j.payload from jobs j where j.id = %s", (int(job_id),)),
            "job",
        )
    return answer(row)


@router.get("/events")
def events(
    type_prefix: Annotated[str | None, Query(max_length=100)] = None,
    level: str | None = None,
    service: Annotated[str | None, Query(max_length=100)] = None,
    entity_type: Annotated[str | None, Query(max_length=100)] = None,
    entity_id: Annotated[str | None, Query(max_length=200)] = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> JSONResponse:
    filters: list[q.Filter] = []
    if type_prefix:
        filters.append(("e.type like %s", (q.like_escape(type_prefix) + "%",)))
    if service:
        filters.append(("e.service = %s", (service,)))
    if entity_type:
        filters.append(("e.entity_type = %s", (entity_type,)))
    if entity_id:
        filters.append(("e.entity_id = %s", (entity_id,)))
    return page("events", filters, level, limit, cursor)


@router.get("/events/{event_id}")
def event(event_id: str) -> JSONResponse:
    if not event_id.isdigit() or len(event_id) > 18:
        raise q.not_found("event")
    with read_tx():
        row = detail(q.event_detail(int(event_id)), "event")
    return answer(row)


# ── insights, settings, search ───────────────────────────────


@router.get("/usage")
def usage(range: Literal["today", "7d", "month"] = "today") -> JSONResponse:  # noqa: A002
    with read_tx():
        return answer(q.usage_view(range))


@router.get("/settings")
def settings_list() -> JSONResponse:
    with read_tx():
        return answer({"items": q.settings_rows()})


@router.get("/settings/history")
def settings_history(limit: Limit = None, cursor: Cursor = None) -> JSONResponse:
    filters: list[q.Filter] = [("(e.type = 'setting.changed' or e.type like 'control.%%')", ())]
    return page("events", filters, None, limit, cursor)


@router.get("/search")
def search(q_: Annotated[str, Query(alias="q", max_length=200)] = "") -> JSONResponse:
    with read_tx():
        return answer({"results": q.search(q_)})

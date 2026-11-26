"""Long-form episodes in the admin console (M20 L5): `GET /v1/admin/episodes`, one episode with its
moments, and signed URLs (1 h) for the episode video and its thumbnail. Read-only, like the rest of
`/v1/admin/*`; the helpers (read-only transaction, answer, signing) come from `routes_admin`."""

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from sixstars import db
from sixstars.api import admin_queries as q
from sixstars.api import routes_admin as admin
from sixstars.api.errors import ApiError
from sixstars.clients._http import ApiError as ClientApiError
from sixstars.clients._http import ClientError, NotConfiguredError
from sixstars.logging import get_logger
from sixstars.storage import SIGNED_URL_TTL_S

router = APIRouter(prefix="/v1/admin")

LIST_SQL = """
select e.id::text as id, e.episode_date, e.status, e.n_moments, e.duration_s, e.title, e.error, e.privacy,
       e.post_url, e.account_id, e.created_at, e.updated_at, e.format,
       (select coalesce(f.title_name, f.display_name, f.handle) from creators f
         where f.id = e.featured_creator_id) as featured_name,
       (e.storage_path is not null) as has_file,
       (select count(*) from episode_items i where i.episode_id = e.id) as items,
       (select count(*) from episode_items i where i.episode_id = e.id and i.status = 'rendered') as rendered,
       (select count(*) from episode_items i where i.episode_id = e.id and i.status = 'dropped') as dropped
  from episodes e
 order by e.episode_date desc, e.created_at desc
 limit %s
"""

DETAIL_SQL = """
select e.id::text as id, e.episode_date, e.status, e.n_moments, e.duration_s, e.title, e.description,
       e.chapters, e.tags, e.thumb_text, e.error, e.privacy, e.post_url, e.account_id, e.created_at,
       e.updated_at, (e.storage_path is not null) as has_file, (e.thumb_path is not null) as has_thumb,
       e.format, e.hashtags, e.thumb_scene, e.thumb_source, e.published_at,
       e.featured_creator_id::text as featured_creator_id,
       coalesce(f.title_name, f.display_name, f.handle) as featured_name
  from episodes e
  left join creators f on f.id = e.featured_creator_id
 where e.id = %s
"""

ITEMS_SQL = """
select i.id::text as id, i.position, i.status, i.error, i.clip_id::text as clip_id, i.est_s, i.duration_s,
       i.cut_start_s, i.cut_end_s, i.align_offset_s, i.fetch_url,
       cl.series_no, coalesce(cl.title, cl.hook) as title, cl.score, cl.source_type,
       cr.platform as creator_platform, cr.handle as creator_handle,
       coalesce(cr.title_name, cr.display_name, cr.handle) as creator_name
  from episode_items i
  join clips cl on cl.id = i.clip_id
  join creators cr on cr.id = i.creator_id
 where i.episode_id = %s
 order by (i.status = 'dropped'), i.position desc
"""


@router.get("/episodes")
def episodes(limit: Annotated[int, Query(ge=1, le=200)] = 60) -> JSONResponse:
    """The latest episodes, newest first."""
    with admin.read_tx():
        rows = [dict(r) for r in db.fetch_all(LIST_SQL, (limit,))]
    return admin.answer({"items": rows})


@router.get("/episodes/{episode_id}")
def episode(episode_id: str) -> JSONResponse:
    """One episode with its moments in playback order (dropped moments last)."""
    q.require_uuid(episode_id, "episode")
    with admin.read_tx():
        row: dict[str, Any] = dict(admin.detail(db.fetch_one(DETAIL_SQL, (episode_id,)), "episode"))
        row["moments"] = [dict(r) for r in db.fetch_all(ITEMS_SQL, (episode_id,))]
        row["events"] = q.entity_events("episode", episode_id)
    return admin.answer(row)


def _sign(key: str | None, what: str, episode_id: str) -> dict[str, Any]:
    if not key:
        return {"url": None, "expires_at": None, "reason": "no_file"}
    try:
        url = admin.storage_factory().signed_url(key, SIGNED_URL_TTL_S)
    except NotConfiguredError as exc:
        raise ApiError(503, "storage_not_configured", "Supabase Storage is not configured") from exc
    except ClientApiError as exc:
        if exc.status in admin.MISSING_OBJECT_STATUSES:
            return {"url": None, "expires_at": None, "reason": "deleted"}
        get_logger("api").warning(
            "episode_media_sign_failed", episode_id=episode_id, what=what, status=exc.status
        )
        raise ApiError(503, "storage_unavailable", "Supabase Storage did not sign the URL") from exc
    except ClientError as exc:
        get_logger("api").warning(
            "episode_media_sign_failed", episode_id=episode_id, what=what, error=type(exc).__name__
        )
        raise ApiError(503, "storage_unavailable", "Supabase Storage did not sign the URL") from exc
    except ValueError:
        return {"url": None, "expires_at": None, "reason": "no_file"}
    expires = admin._now() + timedelta(seconds=SIGNED_URL_TTL_S)
    return {"url": url, "expires_at": expires, "reason": "ok"}


@router.get("/episodes/{episode_id}/media")
def episode_media(episode_id: str) -> JSONResponse:
    """Signed URLs (1 h) for the episode video and its thumbnail, or why there is none."""
    q.require_uuid(episode_id, "episode")
    with admin.read_tx():
        row = admin.detail(
            db.fetch_one("select storage_path, thumb_path from episodes where id = %s", (episode_id,)),
            "episode",
        )
    return admin.answer(
        {
            "video": _sign(row["storage_path"], "video", episode_id),
            "thumb": _sign(row["thumb_path"], "thumb", episode_id),
        }
    )

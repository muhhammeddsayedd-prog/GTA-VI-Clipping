"""Publish a ready episode to YouTube (M20 L6): Upload-Post fetches the MP4 from a signed Storage URL and sets
the thumbnail from `thumbnail_url` (docs/14, Upload-Post upload-video, checked 2026-11-25).

The scheduler calls `due_tick`; from `longform.publish_hour_utc` on, each `ready` episode of the last two
days gets one `longform.publish` job. `publish` refuses unless `longform.enabled`, `longform.publish_mode` is
not `off`, `control.emergency_stop` is off and `launch.dry_run` is off (the posting gate's mode). Staging is
always private, like every staging post. The upload is idempotent (`episode:<id>:<account>` and a fixed
request id), so a job that is run again after a deploy waits for the same upload instead of posting twice.

Caps: an episode counts against the YouTube account's rolling 24 h total. When the Shorts lanes already used
the platform limit, the episode waits for the next tick.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sixstars import db, events, jobs, notify
from sixstars.clients.upload_post import AccountAuthError, PostingBlocked, RateLimited, UploadPostClient
from sixstars.logging import get_logger
from sixstars.longform.config import LongformConfig, PublishMode
from sixstars.posting.gate import Mode, posting_mode
from sixstars.posting.models import Platform
from sixstars.posting.store import DbStore, app_env
from sixstars.storage import ClipStorage

JOB_PUBLISH = "longform.publish"
PLATFORM = "youtube"
LOOKBACK_DAYS = 2
log = get_logger(__name__)

EPISODE_SQL = """
select id::text as id, episode_date, status, title, description, tags, storage_path, thumb_path, account_id,
       upload_request_id
  from episodes where id = %s
"""

USED_24H_SQL = """
select (select count(*) from posts
         where account_id = %(account)s and not dry_run
           and status not in ('skipped', 'failed', 'removed')
           and coalesce(published_at, created_at) >= %(since)s)
     + (select count(*) from episodes
         where account_id = %(account)s and id <> %(episode)s
           and status in ('publishing', 'posted', 'posted_private')
           and coalesce(published_at, updated_at) >= %(since)s) as used
"""

DUE_SQL = """
select id::text as id from episodes
 where status = 'ready' and episode_date >= %s and episode_date <= %s
 order by episode_date
"""


class PublishPendingError(RuntimeError):
    """The upload is still running at Upload-Post: the job is retried and waits again."""


def privacy_for(mode: PublishMode, env_name: str) -> str:
    """The YouTube privacy of the upload. Staging never publishes. Pure."""
    if env_name != "production":
        return "private"
    return "public" if mode is PublishMode.PUBLIC else "private"


def fields_for(ep: dict[str, Any], privacy: str, thumb_url: str | None, category_id: str) -> dict[str, Any]:
    """The Upload-Post fields of a long-form YouTube upload. No #Shorts. Pure."""
    title = str(ep["title"])
    return {
        "title": title,
        "youtube_title": title,
        "youtube_description": str(ep["description"] or ""),
        "description": str(ep["description"] or ""),
        "privacyStatus": privacy,
        "categoryId": category_id,
        "tags": list(ep["tags"] or []),
        "thumbnail_url": thumb_url,
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": False,
        "embeddable": True,
    }


def request_id_for(episode_id: str) -> str:
    return f"episode-{episode_id}"


def blocked_reason(cfg: LongformConfig, now: datetime) -> str | None:
    """Why nothing may be published now, or None."""
    if not cfg.enabled:
        return "longform.enabled is off"
    if cfg.publish_mode is PublishMode.OFF:
        return "longform.publish_mode is off"
    mode, reason = posting_mode(now, DbStore().load_params())
    if mode is not Mode.LIVE:
        return f"posting is not live ({reason.value})"
    return None


def used_24h(account_id: str, episode_id: str, now: datetime) -> int:
    row = db.fetch_one(
        USED_24H_SQL, {"account": account_id, "episode": episode_id, "since": now - timedelta(hours=24)}
    )
    return int(row["used"]) if row is not None else 0


def publish(
    episode_id: str,
    cfg: LongformConfig | None = None,
    *,
    client: UploadPostClient | None = None,
    storage: ClipStorage | None = None,
    now: datetime | None = None,
) -> str:
    """Upload one episode and wait for YouTube's answer. Returns the episode status."""
    cfg = cfg or LongformConfig.load()
    now = now or datetime.now(UTC)
    row = db.fetch_one(EPISODE_SQL, (episode_id,))
    if row is None:
        return "missing"
    ep = dict(row)
    if ep["status"] not in ("ready", "publishing"):
        return str(ep["status"])
    blocked = blocked_reason(cfg, now)
    if blocked:
        log.info("episode_publish_blocked", episode_id=episode_id, reason=blocked)
        return str(ep["status"])
    store = DbStore()
    account = store.get_account(str(ep["account_id"] or cfg.account_id))
    if account is None or not account.enabled or account.platform is not Platform.YOUTUBE:
        return _fail(episode_id, ep, f"account {ep['account_id']} is missing, disabled or not YouTube")
    upload = client or UploadPostClient()
    request_id = str(ep["upload_request_id"] or request_id_for(episode_id))
    privacy = privacy_for(cfg.publish_mode, app_env())
    if ep["status"] == "ready":
        limit = store.load_params().platform_max_24h[Platform.YOUTUBE]
        used = used_24h(account.id, episode_id, now)
        if used >= limit:
            log.warning("episode_publish_cap", episode_id=episode_id, used=used, limit=limit)
            return "ready"
        files = storage or ClipStorage()
        ttl = cfg.publish.video_url_ttl_s
        video_url = files.signed_url(str(ep["storage_path"]), ttl)
        thumb_url = files.signed_url(str(ep["thumb_path"]), ttl) if ep["thumb_path"] else None
        db.execute(
            "update episodes set status = 'publishing', upload_request_id = %s, privacy = %s,"
            " attempts = attempts + 1 where id = %s",
            (request_id, privacy, episode_id),
        )
        try:
            upload.upload_video_url(
                account.upload_post_user,
                PLATFORM,
                video_url,
                fields_for(ep, privacy, thumb_url, cfg.publish.category_id),
                idempotency_key=f"episode:{episode_id}:{account.id}",
                request_id=request_id,
            )
        except PostingBlocked as exc:
            db.execute("update episodes set status = 'ready' where id = %s", (episode_id,))
            log.info("episode_publish_blocked", episode_id=episode_id, reason=str(exc))
            return "ready"
        except RateLimited as exc:
            db.execute(
                "update episodes set status = 'ready', error = %s where id = %s", (str(exc)[:300], episode_id)
            )
            log.warning("episode_publish_rate_limited", episode_id=episode_id)
            return "ready"
        except AccountAuthError as exc:
            return _fail(episode_id, ep, f"Upload-Post account problem: {exc}")
        events.emit(
            "episode.publishing",
            "info",
            "episode",
            episode_id,
            f"uploading ({privacy})",
            {"request_id": request_id},
        )
    status = upload.wait_for_upload(
        request_id, timeout_s=cfg.publish.wait_timeout_s, poll_s=cfg.publish.poll_s
    )
    if not status.finished:
        raise PublishPendingError(f"upload {request_id} still {status.status}")
    result = status.result_for(PLATFORM)
    if result is None or not result.success:
        error = (result.error if result else None) or status.message or status.status
        return _fail(episode_id, ep, f"YouTube upload failed: {error}")
    final = "posted" if privacy == "public" else "posted_private"
    db.execute(
        """
        update episodes set status = %s, platform_post_id = %s, post_url = %s, published_at = %s, error = null
         where id = %s
        """,
        (final, result.post_id, result.post_url, now, episode_id),
    )
    pending_thumb = bool(result.raw.get("thumbnail_pending"))
    detail = {"post_url": result.post_url, "privacy": privacy, "thumbnail_pending": pending_thumb}
    events.emit(f"episode.{final}", "info", "episode", episode_id, str(ep["title"]), detail)
    notify.send(
        "info",
        f"Episode {ep['episode_date']} is live on YouTube ({privacy})",
        f"{ep['title']}\n{result.post_url or result.post_id}",
    )
    log.info("episode_published", episode_id=episode_id, privacy=privacy, post_id=result.post_id)
    return final


def _fail(episode_id: str, ep: dict[str, Any], reason: str) -> str:
    db.execute("update episodes set status = 'failed', error = %s where id = %s", (reason[:500], episode_id))
    events.emit("episode.failed", "warn", "episode", episode_id, reason[:200])
    notify.send("warn", f"Episode {ep['episode_date']} not published", reason[:800])
    return "failed"


def due_tick(now: datetime | None = None, cfg: LongformConfig | None = None) -> list[int]:
    """Scheduler tick: enqueue one publish job per ready episode once the publish hour has come."""
    cfg = cfg or LongformConfig.load()
    now = now or datetime.now(UTC)
    if not cfg.enabled or cfg.publish_mode is PublishMode.OFF or now.hour < cfg.publish_hour_utc:
        return []
    today = now.date()
    out: list[int] = []
    for row in db.fetch_all(DUE_SQL, (today - timedelta(days=LOOKBACK_DAYS), today)):
        job_id = jobs.enqueue(
            "normal",
            JOB_PUBLISH,
            {"episode_id": row["id"]},
            cfg.priority,
            dedupe_key=f"{JOB_PUBLISH}:{row['id']}",
        )
        if job_id is not None:
            out.append(job_id)
    return out


def handle_publish(job: jobs.Job) -> None:
    """Job `longform.publish`."""
    publish(str(job.payload["episode_id"]))

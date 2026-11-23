"""Stage 6 of the cover engine (docs/29a section 6.6 and 7): hand the cover to the post.

At upload time (`cover_fields`, called by the publisher; never raises):
- Instagram: `cover_url` of the passing cover (covers-public), else `thumb_offset` at the best rendered frame.
- TikTok: `cover_timestamp` at the best rendered frame; with `cover.tiktok_image_enabled` also
  `tiktok_cover_image_url` (a signed URL of the passing cover).
- YouTube: nothing at upload (the probe showed it stays pending); after publishing, `cover.youtube_thumb`
  sets the thumbnail with `cover.youtube_custom_enabled`, backing off 2, 5 and 15 minutes while the video
  processes. A 403 switches `cover.youtube_custom_enabled` off and alerts once; a 429 retries for up to 24 h.
A cover that is not ready when the post goes out is logged as `skipped_deadline` and the post goes out
without it. After publishing (`after_published`) the cover row is marked `applied`.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sixstars import db, events, jobs, notify
from sixstars import storage as clip_storage
from sixstars.covers import miner, store
from sixstars.covers.config import CoverConfig, lane_key
from sixstars.logging import get_logger

JOB_YOUTUBE_THUMB = "cover.youtube_thumb"
YOUTUBE_BACKOFF_MIN = (2, 5, 15)
RATE_LIMIT_RETRY_MIN = 60
RATE_LIMIT_GIVE_UP_H = 24
SIGNED_URL_S = 24 * 3600
USABLE = ("passed", "fallback_plain")

_COVER_SQL = """
select id::text as id, status, storage_path, public_url, rendered_ts_ms, platform
from covers
where clip_id = %s and account_id = %s and status in ('passed', 'fallback_plain', 'applied')
order by created_at desc
limit 1
"""


def _timestamp_ms(clip_id: str) -> int | None:
    import json

    path = miner.cover_dir(clip_id) / miner.MINE_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("cover_timestamp_ms")
    except (OSError, ValueError):
        return None
    return int(value) if isinstance(value, int | float) else None


def fields_for(
    platform: str,
    cover: Mapping[str, Any] | None,
    timestamp_ms: int | None,
    *,
    tiktok_image: bool,
    signed_url: str | None,
) -> dict[str, str]:
    """The Upload-Post fields for one platform. Pure."""
    fields: dict[str, str] = {}
    if platform == "instagram":
        if cover is not None and cover.get("public_url"):
            fields["cover_url"] = str(cover["public_url"])
        elif timestamp_ms is not None:
            fields["thumb_offset"] = str(timestamp_ms)
    elif platform == "tiktok":
        if timestamp_ms is not None:
            fields["cover_timestamp"] = str(timestamp_ms)
        if tiktok_image and cover is not None and signed_url:
            fields["tiktok_cover_image_url"] = signed_url
    return fields


def cover_fields(clip_id: str, account_id: str, platform: str, lane: str, post_id: str) -> dict[str, str]:
    """The cover fields for this post (empty when the engine is off or anything fails)."""
    log = get_logger("covers")
    try:
        config = CoverConfig.load()
        key = lane_key(lane)
        if not config.runs_for(platform, key):
            return {}
        cover = db.fetch_one(_COVER_SQL, (clip_id, account_id))
        timestamp = _timestamp_ms(clip_id)
        if cover is None and platform != "youtube":
            store.insert(
                store.NewCover(
                    clip_id=clip_id,
                    lane=key,
                    platform=platform,
                    arm="none",
                    config_version=config.version,
                    account_id=account_id,
                    line_style="none",
                    rendered_ts_ms=timestamp,
                    status="skipped_deadline",
                )
            )
        signed = None
        if (
            cover is not None
            and platform == "tiktok"
            and config.tiktok_image_enabled
            and cover.get("storage_path")
        ):
            signed = clip_storage.ClipStorage().signed_url(str(cover["storage_path"]), SIGNED_URL_S)
        fields = fields_for(
            platform, cover, timestamp, tiktok_image=config.tiktok_image_enabled, signed_url=signed
        )
        if cover is not None:
            db.execute("update covers set post_id = %s::uuid where id = %s", (post_id, cover["id"]))
        log.info("cover_fields", clip_id=clip_id, account_id=account_id, fields=sorted(fields))
        return fields
    except Exception as exc:  # noqa: BLE001 - a cover never stops a post
        log.warning("cover_fields_failed", clip_id=clip_id, error=f"{type(exc).__name__}: {exc}"[:300])
        alert_once("fields", "Cover fields failed", f"clip {clip_id}: {type(exc).__name__}: {exc}"[:300])
        return {}


def after_published(post_id: str, platform: str, platform_post_id: str | None, user: str) -> None:
    """Mark the post's cover applied; queue the YouTube thumbnail when that is on. Never raises."""
    try:
        cover = db.fetch_one(
            "select id::text as id, status, storage_path from covers where post_id = %s::uuid "
            "order by created_at desc limit 1",
            (post_id,),
        )
        if cover is None or cover["status"] not in USABLE:
            return
        if platform != "youtube":
            store.set_status(str(cover["id"]), "applied", post_id=post_id)
            return
        if not CoverConfig.load().youtube_custom_enabled or not platform_post_id or not cover["storage_path"]:
            return
        queue_youtube_thumb(str(cover["id"]), platform_post_id, user, attempt=0)
    except Exception as exc:  # noqa: BLE001 - the post is already published
        get_logger("covers").warning("cover_after_publish_failed", post_id=post_id, error=str(exc)[:300])


def queue_youtube_thumb(
    cover_id: str, video_id: str, user: str, *, attempt: int, delay_min: float | None = None
) -> None:
    delay = (
        delay_min
        if delay_min is not None
        else YOUTUBE_BACKOFF_MIN[min(attempt, len(YOUTUBE_BACKOFF_MIN) - 1)]
    )
    jobs.enqueue(
        "normal",
        JOB_YOUTUBE_THUMB,
        {
            "cover_id": cover_id,
            "video_id": video_id,
            "user": user,
            "attempt": attempt,
            "first_at": datetime.now(UTC).isoformat(),
        },
        run_after=datetime.now(UTC) + timedelta(minutes=delay),
        dedupe_key=f"{JOB_YOUTUBE_THUMB}:{cover_id}:{attempt}",
    )


def handle_youtube_thumb(job: jobs.Job, *, client: Any = None) -> None:
    """Set the YouTube thumbnail of a published Short; back off while it processes."""
    from sixstars.clients.upload_post import AccountAuthError, RateLimited, UploadPostClient

    payload = job.payload
    cover_id, video_id, user = str(payload["cover_id"]), str(payload["video_id"]), str(payload["user"])
    attempt = int(payload.get("attempt") or 0)
    first_at = datetime.fromisoformat(str(payload.get("first_at") or datetime.now(UTC).isoformat()))
    config = CoverConfig.load()
    if not config.youtube_custom_enabled:
        return
    row = db.fetch_one("select storage_path from covers where id = %s", (cover_id,))
    if row is None or not row["storage_path"]:
        return
    url = clip_storage.ClipStorage().signed_url(str(row["storage_path"]), SIGNED_URL_S)
    upload = client or UploadPostClient()
    try:
        done = upload.set_youtube_thumbnail(user, video_id, url)
    except AccountAuthError as exc:
        disable_youtube(f"{exc}"[:300])
        store.set_status(cover_id, "apply_failed", error="youtube 403")
        return
    except RateLimited:
        if datetime.now(UTC) - first_at < timedelta(hours=RATE_LIMIT_GIVE_UP_H):
            queue_youtube_thumb(cover_id, video_id, user, attempt=attempt + 1, delay_min=RATE_LIMIT_RETRY_MIN)
        else:
            store.set_status(cover_id, "apply_failed", error="youtube 429 for 24 h")
        return
    if done:
        store.set_status(cover_id, "applied")
        return
    if attempt + 1 < len(YOUTUBE_BACKOFF_MIN):
        queue_youtube_thumb(cover_id, video_id, user, attempt=attempt + 1)
    else:
        store.set_status(cover_id, "apply_failed", error="still processing after the last retry")


def disable_youtube(reason: str) -> None:
    """Switch `cover.youtube_custom_enabled` off (versioned like every scoring_config change) and alert."""
    db.execute("update scoring_config set value = 'false'::jsonb where key = 'cover.youtube_custom_enabled'")
    alert_once(
        "youtube_403", "Cover engine: YouTube thumbnails switched off", f"YouTube answered 403: {reason}"
    )


def alert_once(kind: str, title: str, body: str) -> None:
    """One Telegram alert per kind and UTC day (the rest only go to the log and the events table)."""
    try:
        seen = db.fetch_one(
            "select 1 from events where type = 'cover.alert' and data->>'kind' = %s "
            "and created_at >= date_trunc('day', now(), 'UTC') limit 1",
            (kind,),
        )
        events.emit("cover.alert", level="warn", data={"kind": kind, "body": body[:500]})
        if seen is None:
            notify.send("warn", title, body)
    except Exception as exc:  # noqa: BLE001 - alerting must not fail the caller
        get_logger("covers").warning("cover_alert_failed", kind=kind, error=str(exc)[:200])


def register_handlers() -> None:
    """Register `cover.youtube_thumb` with the worker (safe to call more than once)."""
    from sixstars.services import worker

    if worker.HANDLERS.get(JOB_YOUTUBE_THUMB) is not handle_youtube_thumb:
        worker.register(JOB_YOUTUBE_THUMB, handle_youtube_thumb)

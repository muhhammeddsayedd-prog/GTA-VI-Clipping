"""One fixed thumbnail for every YouTube Short (owner, 2026-11-25; migration 0057).

When `cover.youtube_fixed_thumbnail` names an image in the public `covers-public` bucket, every published
YouTube post gets that image through Upload-Post's set-thumbnail endpoint (job `cover.youtube_fixed_thumb`).
This works with the cover engine off and replaces any composed YouTube cover. The job backs off while
YouTube still processes the Short (202), retries a 429 hourly for up to 24 h, and alerts once on a 403.
`backfill` queues the same job for Shorts published before the setting was on.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sixstars import db, events, jobs
from sixstars import storage as clip_storage
from sixstars.covers.config import CoverConfig
from sixstars.logging import get_logger

JOB = "cover.youtube_fixed_thumb"
BACKOFF_MIN = (2, 5, 15, 30, 60)
RATE_LIMIT_RETRY_MIN = 60
RATE_LIMIT_GIVE_UP_H = 24
KEY_PREFIX = f"{clip_storage.COVERS_PUBLIC_BUCKET}/brand/youtube-thumbnail"
SETTING = "cover.youtube_fixed_thumbnail"


def image_key(image: bytes) -> str:
    """The storage key for a thumbnail image, named by its content so a new image gets a new URL. Pure."""
    return f"{KEY_PREFIX}-{hashlib.sha256(image).hexdigest()[:12]}.jpg"


def delay_for(attempt: int) -> int:
    """Minutes before try number `attempt` (0-based) while the Short processes. Pure."""
    return BACKOFF_MIN[min(attempt, len(BACKOFF_MIN) - 1)]


def queue(
    post_id: str, video_id: str, user: str, *, attempt: int = 0, delay_min: float | None = None
) -> None:
    """Queue the set-thumbnail job for one published YouTube post."""
    delay = delay_for(attempt) if delay_min is None else delay_min
    jobs.enqueue(
        "normal",
        JOB,
        {
            "post_id": post_id,
            "video_id": video_id,
            "user": user,
            "attempt": attempt,
            "first_at": datetime.now(UTC).isoformat(),
        },
        run_after=datetime.now(UTC) + timedelta(minutes=delay),
        dedupe_key=f"{JOB}:{post_id}:{attempt}",
    )


def on_published(
    post_id: str, platform: str, platform_post_id: str | None, user: str, config: CoverConfig
) -> bool:
    """Queue the fixed thumbnail for a published YouTube post. True when the fixed thumbnail owns YouTube
    (the composed-cover path then stays out of it), False when the feature is off or this is not YouTube."""
    if platform != "youtube" or config.youtube_fixed_thumbnail is None:
        return False
    if platform_post_id:
        queue(post_id, platform_post_id, user)
    else:
        get_logger("covers").warning("youtube_fixed_thumb_no_video_id", post_id=post_id)
    return True


def handle(job: jobs.Job, *, client: Any = None, storage: Any = None) -> None:
    """Set the fixed thumbnail on one Short; back off while it processes."""
    from sixstars.clients.upload_post import AccountAuthError, PostingBlocked, RateLimited, UploadPostClient
    from sixstars.covers.deliver import alert_once

    log = get_logger("covers")
    payload = job.payload
    post_id, video_id, user = str(payload["post_id"]), str(payload["video_id"]), str(payload["user"])
    attempt = int(payload.get("attempt") or 0)
    first_at = datetime.fromisoformat(str(payload.get("first_at") or datetime.now(UTC).isoformat()))
    key = CoverConfig.load().youtube_fixed_thumbnail
    if key is None:
        return
    url = (storage or clip_storage.ClipStorage()).public_url(key)
    upload = client or UploadPostClient()
    data = {"post_id": post_id, "video_id": video_id, "thumbnail": key, "attempt": attempt}
    try:
        done = upload.set_youtube_thumbnail(user, video_id, url)
    except PostingBlocked as exc:
        log.info("youtube_fixed_thumb_blocked", post_id=post_id, reason=str(exc)[:200])
        return
    except AccountAuthError as exc:
        events.emit("cover.youtube_fixed_failed", "warn", "post", post_id, "YouTube answered 403", data)
        alert_once("youtube_fixed_403", "YouTube thumbnail refused (403)", f"video {video_id}: {exc}"[:300])
        return
    except RateLimited:
        if datetime.now(UTC) - first_at < timedelta(hours=RATE_LIMIT_GIVE_UP_H):
            _requeue(payload, attempt + 1, RATE_LIMIT_RETRY_MIN)
        else:
            events.emit("cover.youtube_fixed_failed", "warn", "post", post_id, "YouTube 429 for 24 h", data)
        return
    if done:
        events.emit("cover.youtube_fixed_applied", "info", "post", post_id, None, data)
        log.info("youtube_fixed_thumb_set", post_id=post_id, video_id=video_id)
        return
    if attempt + 1 < len(BACKOFF_MIN):
        _requeue(payload, attempt + 1, delay_for(attempt + 1))
    else:
        events.emit("cover.youtube_fixed_failed", "warn", "post", post_id, "still processing", data)
        alert_once("youtube_fixed_pending", "YouTube thumbnail not set", f"video {video_id} still processing")


def _requeue(payload: dict[str, Any], attempt: int, delay_min: float) -> None:
    jobs.enqueue(
        "normal",
        JOB,
        {**payload, "attempt": attempt},
        run_after=datetime.now(UTC) + timedelta(minutes=delay_min),
        dedupe_key=f"{JOB}:{payload['post_id']}:{attempt}",
    )


def install(image: Path, *, storage: Any = None) -> str:
    """Upload `image` (JPEG) to covers-public, point `cover.youtube_fixed_thumbnail` at it, return the key."""
    key = image_key(image.read_bytes())
    (storage or clip_storage.ClipStorage()).upload(image, key, "image/jpeg")
    db.execute("update scoring_config set value = to_jsonb(%s::text) where key = %s", (key, SETTING))
    events.emit("cover.youtube_fixed_set", "info", data={"thumbnail": key})
    return key


def disable() -> None:
    """Turn the fixed thumbnail off (YouTube goes back to the cover engine's rules)."""
    db.execute("update scoring_config set value = 'null'::jsonb where key = %s", (SETTING,))
    events.emit("cover.youtube_fixed_set", "info", data={"thumbnail": None})


_PUBLISHED_SQL = """
select p.id::text as post_id, p.platform_post_id, a.upload_post_user
from posts p join platform_accounts a on a.id = p.account_id
where p.platform = 'youtube' and p.status = 'published' and p.platform_post_id is not null
  and p.published_at >= now() - make_interval(days => %s)
order by p.published_at desc
"""


def backfill(days: int) -> int:
    """Queue the fixed thumbnail for YouTube posts published in the last `days` days. Returns how many."""
    if CoverConfig.load().youtube_fixed_thumbnail is None:
        return 0
    rows = db.fetch_all(_PUBLISHED_SQL, (days,))
    for index, row in enumerate(rows):
        # Spread the calls (each costs YouTube quota) one every 20 s.
        queue(
            str(row["post_id"]),
            str(row["platform_post_id"]),
            str(row["upload_post_user"]),
            attempt=0,
            delay_min=index / 3,
        )
    return len(rows)


def register_handlers() -> None:
    """Register `cover.youtube_fixed_thumb` with the worker (safe to call more than once)."""
    from sixstars.services import worker

    if worker.HANDLERS.get(JOB) is not handle:
        worker.register(JOB, handle)

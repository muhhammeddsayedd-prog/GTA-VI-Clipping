"""All SQL of the Twitch Clips source: the `viewer_clips` table (migration 0019) and what it reads from
`clips` and `creators`. Every change of `viewer_clips.status` writes its `events` row in the same
transaction.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from psycopg.types.json import Jsonb

from sixstars import db, events, jobs
from sixstars.config import env
from sixstars.discovery.models import FORBIDDEN_TIERS
from sixstars.viewer_clips import JOB_PROCESS
from sixstars.viewer_clips.select import Range, Ranked

PLATFORM: Final = "twitch"
ENTITY: Final = "viewer_clip"
JOB_PRIORITY: Final = 30  # the priority discovery gives `vod.process`
# Rows that hold a moment: another viewer clip of the same range is not queued while one of these exists.
HOLDING: Final = ("new", "processing", "done")
WAITING: Final = ("new", "processing")
CLOSED: Final = ("done", "skipped", "failed")


@dataclass(frozen=True, slots=True)
class ViewerClip:
    """A `viewer_clips` row with what the job needs from the broadcaster's creator row."""

    id: str
    source_id: str
    creator_id: str
    url: str
    title: str
    language: str | None
    view_count: int | None
    duration_s: float | None
    clip_created_at: datetime | None
    vod_id: str | None
    vod_offset_s: float | None
    status: str
    skip_reason: str | None
    attempts: int
    creator_handle: str
    creator_tier: str

    @property
    def forbidden(self) -> bool:
        """True when the broadcaster's tier forbids clipping."""
        return self.creator_tier in FORBIDDEN_TIERS

    @property
    def closed(self) -> bool:
        return self.status in CLOSED


def job_key(row_id: str) -> str:
    return f"viewer_clip:{row_id}"


def download_dir(row_id: str) -> Path:
    """`$DATA_DIR/viewer_clips/<id>/`: the downloaded clip, deleted when the job ends."""
    return env().DATA_DIR / "viewer_clips" / row_id


# ── discovery side ───────────────────────────────────────────


def known_ids(clip_ids: Collection[str]) -> set[str]:
    """The Twitch clip ids we have stored before, whatever became of them."""
    if not clip_ids:
        return set()
    rows = db.fetch_all(
        "select source_id from viewer_clips where platform = %s and source_id = any(%s)",
        (PLATFORM, list(clip_ids)),
    )
    return {str(row["source_id"]) for row in rows}


def queue_counts() -> tuple[int, int]:
    """`(stored in the last 24 hours, waiting to be rendered)`."""
    row = db.fetch_one(
        """
        select count(*) filter (where created_at >= now() - interval '24 hours') as recent,
               count(*) filter (where status = any(%s)) as waiting
        from viewer_clips
        """,
        (list(WAITING),),
    )
    assert row is not None
    return int(row["recent"]), int(row["waiting"])


def close_orphans(grace_s: float) -> list[str]:
    """Close open rows that have no queued or running job and were not touched for `grace_s` seconds:
    their job ended without closing them (it failed for good while its worker was dead). Each becomes
    `failed` with a `viewer_clip.failed` event. Returns their ids."""
    with db.transaction():
        rows = db.fetch_all(
            """
            update viewer_clips v
            set status = 'failed', skip_reason = 'orphaned: its job ended without closing it'
            where v.status = any(%s) and v.updated_at < now() - make_interval(secs => %s)
              and not exists (
                select 1 from jobs j
                where j.type = %s and j.status in ('queued', 'running')
                  and j.payload->>'viewer_clip_id' = v.id::text)
            returning v.id::text as id
            """,
            (list(WAITING), max(0.0, grace_s), JOB_PROCESS),
        )
        for row in rows:
            events.emit(
                "viewer_clip.failed",
                level="warn",
                entity_type=ENTITY,
                entity_id=str(row["id"]),
                message="orphaned: its job ended without closing it",
            )
    return [str(row["id"]) for row in rows]


def taken_ranges(video_ids: Collection[str]) -> dict[str, list[Range]]:
    """Per broadcast video id, the ranges we already have a clip of: our own clips of that video (any
    lane, not rejected) and earlier viewer clips that were queued or rendered."""
    if not video_ids:
        return {}
    rows = db.fetch_all(
        """
        select source_id as video_id, start_s, end_s from clips
        where source_platform = %(platform)s and source_id = any(%(ids)s) and status <> 'rejected'
        union all
        select vod_id, vod_offset_s, vod_offset_s + duration_s from viewer_clips
        where platform = %(platform)s and vod_id = any(%(ids)s) and status = any(%(holding)s)
          and vod_offset_s is not null and duration_s is not null
        """,
        {"platform": PLATFORM, "ids": list(video_ids), "holding": list(HOLDING)},
    )
    taken: dict[str, list[Range]] = {}
    for row in rows:
        taken.setdefault(str(row["video_id"]), []).append((float(row["start_s"]), float(row["end_s"])))
    return taken


def creators_by_channel(channel_ids: Collection[str]) -> dict[str, Mapping[str, Any]]:
    """Twitch creator rows by channel id (Twitch user id)."""
    if not channel_ids:
        return {}
    rows = db.fetch_all(
        "select * from creators where platform = %s and channel_id = any(%s)", (PLATFORM, list(channel_ids))
    )
    return {str(row["channel_id"]): row for row in rows}


def queue(item: Ranked, creator_id: str) -> str | None:
    """Insert the row (status `new`) and its `viewer_clip.process` job, with the event, in one
    transaction. Returns the row id, or None when the clip was stored before."""
    clip = item.clip
    with db.transaction():
        row = db.fetch_one(
            """
            insert into viewer_clips
              (platform, source_id, creator_id, url, title, clipped_by, language, view_count, duration_s,
               clip_created_at, vod_id, vod_offset_s, rank_score, rank_parts, thumbnail_url)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (platform, source_id) do nothing
            returning id::text as id
            """,
            (PLATFORM, clip.id, creator_id, clip.url, clip.title, clip.creator_name or None)
            + (clip.language or None, clip.view_count, clip.duration_s, clip.created_at)
            + (clip.video_id, clip.vod_offset_s, item.rank, Jsonb(item.parts), clip.thumbnail_url or None),
        )
        if row is None:
            return None
        row_id = str(row["id"])
        jobs.enqueue(
            "normal",
            JOB_PROCESS,
            {"viewer_clip_id": row_id},
            priority=JOB_PRIORITY,
            dedupe_key=job_key(row_id),
        )
        events.emit(
            "viewer_clip.queued",
            entity_type=ENTITY,
            entity_id=row_id,
            message=f"twitch {clip.broadcaster_name}: {clip.title}"[:300],
            data={"source_id": clip.id, "views": clip.view_count, "rank": item.rank, "vod_id": clip.video_id},
        )
        return row_id


# ── job side ─────────────────────────────────────────────────

_ROW_SQL = """
select v.id::text as id, v.source_id, v.creator_id::text as creator_id, v.url, v.title, v.language,
       v.view_count, v.duration_s, v.clip_created_at, v.vod_id, v.vod_offset_s, v.status, v.skip_reason,
       v.attempts, cr.handle as creator_handle, cr.tier as creator_tier
from viewer_clips v join creators cr on cr.id = v.creator_id
"""


def load(row_id: str) -> ViewerClip | None:
    row = db.fetch_one(_ROW_SQL + " where v.id::text = %s", (row_id,))
    if row is None:
        return None
    return ViewerClip(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        creator_id=str(row["creator_id"]),
        url=str(row["url"]),
        title=str(row["title"] or ""),
        language=row["language"],
        view_count=None if row["view_count"] is None else int(row["view_count"]),
        duration_s=None if row["duration_s"] is None else float(row["duration_s"]),
        clip_created_at=row["clip_created_at"],
        vod_id=row["vod_id"],
        vod_offset_s=None if row["vod_offset_s"] is None else float(row["vod_offset_s"]),
        status=str(row["status"]),
        skip_reason=row["skip_reason"],
        attempts=int(row["attempts"]),
        creator_handle=str(row["creator_handle"]),
        creator_tier=str(row["creator_tier"]),
    )


def set_status(
    row_id: str,
    status: str,
    event: str,
    *,
    reason: str | None = None,
    level: events.Level = "info",
    data: Mapping[str, Any] | None = None,
    count_attempt: bool = False,
) -> bool:
    """Move a row that is not closed to `status` and write `event`. Returns False, and writes nothing,
    when the row is already closed (a second worker or a retried job got there first)."""
    with db.transaction():
        changed = db.execute(
            """
            update viewer_clips
            set status = %s, skip_reason = %s, attempts = attempts + %s
            where id::text = %s and status <> all(%s)
            """,
            (status, reason[:500] if reason else None, 1 if count_attempt else 0, row_id, list(CLOSED)),
        )
        if changed:
            events.emit(
                event,
                level=level,
                entity_type=ENTITY,
                entity_id=row_id,
                message=reason[:500] if reason else None,
                data=data,
            )
    return bool(changed)


def clip_status(row_id: str) -> str | None:
    """The status of the rendered clip of this row (`clips.id` is the row's id), None before a render."""
    row = db.fetch_one("select status from clips where id::text = %s", (row_id,))
    return None if row is None else str(row["status"])

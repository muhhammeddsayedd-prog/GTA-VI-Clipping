"""SQL of the VOD lane: the `videos` row and its status changes, chat files, the spikes of one video, the
download-blocked markers and the files the lane keeps under `DATA_DIR`. Implemented in M08.

Every change of `videos.status` goes through `set_status`, which writes the `events` row in the same
transaction.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

import psycopg
from psycopg.types.json import Jsonb

from sixstars import db, events
from sixstars.config import env
from sixstars.logging import get_logger

FORBIDDEN_TIERS: Final = ("restricted", "blocked")
OPEN_STATUSES: Final = ("new", "processing")
EVENT_BLOCKED: Final = "vod.download_blocked"
EVENT_BLOCK_NOTIFIED: Final = "vod.download_blocked_notified"
EVENT_FALLBACK: Final = "video.fallback_picked"
USAGE_FALLBACK_TRANSCRIBE: Final = "vod_fallback_transcribe"
SERVICE: Final = "vod"

log = get_logger("vod")


@dataclass(frozen=True, slots=True)
class Video:
    """A `videos` row with what the lane needs from its creator and its live session."""

    id: str
    creator_id: str
    platform: str
    source_id: str
    url: str
    title: str
    duration_s: float | None
    has_chat: bool | None
    live_session_id: str | None
    status: str
    skip_reason: str | None
    attempts: int
    creator_handle: str
    creator_tier: str
    session_started_at: datetime | None
    clipped_ranges: tuple[Mapping[str, Any], ...]

    @property
    def forbidden(self) -> bool:
        """True when the creator's tier forbids clipping."""
        return self.creator_tier in FORBIDDEN_TIERS


@dataclass(frozen=True, slots=True)
class SpikeRow:
    """A stored spike of one video."""

    id: str
    mode: str
    status: str
    tier: str
    score_z: float
    offset_s: float
    window_start_s: float
    window_end_s: float
    msgs: int
    uniq: int
    parts: Mapping[str, Any]
    chat_sample: tuple[Mapping[str, Any], ...]
    confidence: float | None
    reason: str | None

    @property
    def pad_start_s(self) -> float:
        value = self.parts.get("pad_start")
        return float(value) if isinstance(value, int | float) else self.window_start_s

    @property
    def pad_end_s(self) -> float:
        value = self.parts.get("pad_end")
        return float(value) if isinstance(value, int | float) else self.window_end_s


# ── Paths under DATA_DIR ─────────────────────────────────────


def video_dir(video_id: str) -> Path:
    """`$DATA_DIR/vod/<video_id>/`: everything the lane keeps on disk for one video."""
    return env().DATA_DIR / "vod" / video_id


def sections_dir(video_id: str) -> Path:
    """Downloaded windows, one file per spike; each is deleted once its render has ended."""
    return video_dir(video_id) / "sections"


def work_dir(video_id: str) -> Path:
    """Scratch files of one processing attempt (captions, audio); removed when the attempt ends."""
    return video_dir(video_id) / "work"


def chat_path(platform: str, source_id: str) -> Path:
    """`$DATA_DIR/chat/<platform>/<source_id>.jsonl`: the chat replay, kept `vod.chat_retention_s`."""
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in source_id)
    return env().DATA_DIR / "chat" / platform / f"{safe}.jsonl"


# ── videos ───────────────────────────────────────────────────

_VIDEO_SQL: Final = """
    select v.id::text as id, v.creator_id::text as creator_id, v.platform, v.source_id, v.url, v.title,
           v.duration_s, v.has_chat, v.live_session_id::text as live_session_id, v.status,
           v.skip_reason, v.attempts,
           c.handle as creator_handle, c.tier as creator_tier,
           l.started_at as session_started_at, l.clipped_ranges
    from videos v
    join creators c on c.id = v.creator_id
    left join live_sessions l on l.id = v.live_session_id
    where v.id = %s
"""


def _video(row: Mapping[str, Any]) -> Video:
    ranges = row["clipped_ranges"] if isinstance(row["clipped_ranges"], list) else []
    return Video(
        id=str(row["id"]),
        creator_id=str(row["creator_id"]),
        platform=str(row["platform"]),
        source_id=str(row["source_id"]),
        url=str(row["url"]),
        title=str(row["title"] or ""),
        duration_s=float(row["duration_s"]) if row["duration_s"] else None,
        has_chat=row["has_chat"],
        live_session_id=row["live_session_id"],
        status=str(row["status"]),
        skip_reason=row["skip_reason"],
        attempts=int(row["attempts"]),
        creator_handle=str(row["creator_handle"]),
        creator_tier=str(row["creator_tier"]),
        session_started_at=row["session_started_at"],
        clipped_ranges=tuple(item for item in ranges if isinstance(item, Mapping)),
    )


def load_video(video_id: str) -> Video | None:
    """The video with its creator and live session, or None when the id is unknown or malformed."""
    try:
        row = db.fetch_one(_VIDEO_SQL, (video_id,))
    except psycopg.errors.InvalidTextRepresentation:
        return None
    return _video(row) if row is not None else None


def set_status(
    video_id: str,
    status: str,
    event_type: str,
    *,
    reason: str | None = None,
    level: events.Level = "info",
    data: Mapping[str, Any] | None = None,
    count_attempt: bool = False,
    only_from: Sequence[str] = OPEN_STATUSES,
) -> bool:
    """Change `videos.status` and write the event, in one transaction.

    `skip_reason` is set to `reason` (cleared when None). The change applies only to a row whose status
    is in `only_from`; returns False (and writes nothing) otherwise.
    """
    with db.transaction():
        changed = db.execute(
            """
            update videos
            set status = %s, skip_reason = %s, attempts = attempts + %s
            where id = %s and status = any(%s)
            """,
            (status, reason[:500] if reason else None, 1 if count_attempt else 0, video_id, list(only_from)),
        )
        if not changed:
            return False
        events.emit(
            event_type,
            level=level,
            entity_type="video",
            entity_id=video_id,
            message=reason[:500] if reason else None,
            data={"status": status, **(data or {})},
            service=SERVICE,
        )
    return True


# True when a `vod.process` or `replay.scan` job other than `%(job_id)s` is running for video `v`.
_RUNNING_JOB_SQL: Final = """
    exists (
      select 1 from jobs j
      where j.status = 'running' and j.type in ('vod.process', 'replay.scan')
        and j.id is distinct from %(job_id)s::bigint
        and (j.payload->>'video_id' = v.id::text
             or (v.live_session_id is not null
                 and j.payload->>'live_session_id' = v.live_session_id::text))
    )
"""
SLOT_LOCK: Final = "vod.concurrency_slot"
EVENT_RECOVERED: Final = "video.recovered"
ClaimResult = Literal["claimed", "busy", "full"]


def _slot_lock() -> None:
    """Serialise slot claims and orphan resets across workers until the transaction ends."""
    db.fetch_one("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (SLOT_LOCK,))


def count_in_work(exclude_video_id: str, grace_s: float) -> int:
    """Other videos that are really being processed: status `processing` with a running job for them,
    or (a run started by hand has no job) updated in the last `grace_s` seconds."""
    row = db.fetch_one(
        f"""
        select count(*) as n from videos v
        where v.status = 'processing' and v.id <> %(id)s
          and ({_RUNNING_JOB_SQL} or v.updated_at > now() - make_interval(secs => %(grace)s))
        """,
        {"id": exclude_video_id, "grace": grace_s, "job_id": None},
    )
    return int(row["n"]) if row is not None else 0


def claim(
    video_id: str,
    stale_s: float,
    *,
    limit: int | None = None,
    grace_s: float = 0.0,
    job_id: int | None = None,
) -> ClaimResult:
    """Take a video and a concurrency slot for processing, atomically.

    The video can be taken when it is `new`, or `processing` and either not updated for `stale_s` or
    (the caller is job `job_id`) no other job is running for it: then it was left behind by an attempt
    that ended without closing it, and this job resumes it. `busy`: another worker has the video.
    `full`: `limit` other videos are in work (`count_in_work`; None = no limit); nothing changes.
    `claimed`: status `processing`, attempts + 1, with the event.

    Check and update run under one advisory lock, so two workers cannot both take the last slot.
    """
    with db.transaction():
        _slot_lock()
        row = db.fetch_one(
            f"""
            select v.status, v.updated_at <= now() - make_interval(secs => %(stale)s) as stale,
                   {_RUNNING_JOB_SQL} as other_job
            from videos v where v.id = %(id)s
            for update of v
            """,
            {"id": video_id, "stale": stale_s, "job_id": job_id},
        )
        if row is None:
            return "busy"
        orphan = job_id is not None and not row["other_job"]
        resumed = row["status"] == "processing" and (bool(row["stale"]) or orphan)
        if row["status"] != "new" and not resumed:
            return "busy"
        if limit is not None and count_in_work(video_id, grace_s) >= limit:
            return "full"
        db.execute(
            "update videos set status = 'processing', skip_reason = null, attempts = attempts + 1, "
            "updated_at = now() where id = %s",
            (video_id,),
        )
        events.emit(
            "video.processing",
            entity_type="video",
            entity_id=video_id,
            data={"status": "processing", "resumed": resumed, "job_id": job_id},
            service=SERVICE,
        )
    return "claimed"


def reset_orphans(grace_s: float) -> list[str]:
    """Put back to `new` every video in `processing` that nothing is working on: no running job for it
    and not updated for `grace_s` seconds. One `video.recovered` event per video. Returns the ids."""
    reason = "recovered: left in processing without a running job"
    with db.transaction():
        _slot_lock()
        rows = db.fetch_all(
            f"""
            update videos v set status = 'new', skip_reason = %(reason)s
            where v.status = 'processing'
              and v.updated_at <= now() - make_interval(secs => %(grace)s)
              and not {_RUNNING_JOB_SQL}
            returning v.id::text as id
            """,
            {"reason": reason, "grace": grace_s, "job_id": None},
        )
        ids = sorted(str(row["id"]) for row in rows)
        for video_id in ids:
            events.emit(
                EVENT_RECOVERED,
                level="warn",
                entity_type="video",
                entity_id=video_id,
                message=reason,
                data={"status": "new", "grace_s": grace_s},
                service=SERVICE,
            )
    return ids


def latest_job_status(video_id: str) -> str | None:
    """Status of the newest `vod.process` or `replay.scan` job of a video, or None when it has none."""
    row = db.fetch_one(
        """
        select j.status from jobs j
        join videos v on v.id::text = %s
        where j.type in ('vod.process', 'replay.scan')
          and (j.payload->>'video_id' = v.id::text
               or (v.live_session_id is not null
                   and j.payload->>'live_session_id' = v.live_session_id::text))
        order by j.id desc limit 1
        """,
        (video_id,),
    )
    return str(row["status"]) if row is not None else None


def touch(video_id: str, *, title: str | None = None, duration_s: float | None = None) -> None:
    """Refresh `updated_at` (the lane's sign of life) and fill the title or length when they are missing."""
    db.execute(
        """
        update videos
        set updated_at = now(),
            title = coalesce(nullif(title, ''), %s),
            duration_s = coalesce(duration_s, %s)
        where id = %s
        """,
        (title or None, int(duration_s) if duration_s else None, video_id),
    )


# ── chat files ───────────────────────────────────────────────


def save_chat_file(
    video: Video,
    *,
    path: Path,
    messages_count: int,
    duration_s: float | None,
    baseline_rate: float,
    bot_names: Sequence[str],
) -> None:
    """Create or refresh the `chat_files` row of a video."""
    db.execute(
        """
        insert into chat_files
          (platform, source_id, video_id, storage_path, messages_count, duration_s, baseline_rate, bot_names)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (platform, source_id) do update
          set video_id = excluded.video_id, storage_path = excluded.storage_path,
              messages_count = excluded.messages_count, duration_s = excluded.duration_s,
              baseline_rate = excluded.baseline_rate, bot_names = excluded.bot_names
        """,
        (
            video.platform,
            video.source_id,
            video.id,
            str(path),
            messages_count,
            int(duration_s) if duration_s else None,
            baseline_rate,
            sorted(bot_names),
        ),
    )


def chat_files_older_than(seconds: float) -> list[tuple[str, str]]:
    """`(id, storage_path)` of chat files downloaded more than `seconds` ago that still name a file."""
    rows = db.fetch_all(
        """
        select id::text as id, storage_path from chat_files
        where storage_path is not null and downloaded_at < now() - make_interval(secs => %s)
        """,
        (seconds,),
    )
    return [(str(row["id"]), str(row["storage_path"])) for row in rows]


def clear_chat_path(chat_file_id: str) -> None:
    """Record that the file of a chat replay was deleted (the numbers stay)."""
    db.execute("update chat_files set storage_path = null where id = %s", (chat_file_id,))


# ── spikes ───────────────────────────────────────────────────


def _spike(row: Mapping[str, Any]) -> SpikeRow:
    sample = row["chat_sample"] if isinstance(row["chat_sample"], list) else []
    return SpikeRow(
        id=str(row["id"]),
        mode=str(row["mode"]),
        status=str(row["status"]),
        tier=str(row["tier"] or "B"),
        score_z=float(row["score_z"] or 0.0),
        offset_s=float(row["spike_offset_s"] or 0.0),
        window_start_s=float(row["window_start_s"] or 0.0),
        window_end_s=float(row["window_end_s"] or 0.0),
        msgs=int(row["msgs_in_bucket"] or 0),
        uniq=int(row["unique_chatters"] or 0),
        parts=row["parts"] if isinstance(row["parts"], Mapping) else {},
        chat_sample=tuple(item for item in sample if isinstance(item, Mapping)),
        confidence=float(row["claude_confidence"]) if row["claude_confidence"] is not None else None,
        reason=row["claude_reason"],
    )


def video_spikes(video_id: str) -> list[SpikeRow]:
    """The spikes of a video that were not merged into another one, best first."""
    rows = db.fetch_all(
        """
        select id::text as id, mode, status, tier, score_z, spike_offset_s, window_start_s, window_end_s,
               msgs_in_bucket, unique_chatters, parts, chat_sample, claude_confidence, claude_reason
        from spikes
        where video_id = %s and status <> 'merged' and mode in ('replay', 'transcript')
        order by score_z desc nulls last, msgs_in_bucket desc nulls last, spike_offset_s
        """,
        (video_id,),
    )
    return [_spike(row) for row in rows]


def reject_spike(spike_id: str, reason: str) -> None:
    """Close a spike the lane will not clip (for example one the live lane already clipped)."""
    with db.transaction():
        changed = db.execute(
            """
            update spikes set status = 'rejected', tier = 'rejected', claude_reason = %s
            where id = %s and status = 'detected'
            """,
            (reason[:500], spike_id),
        )
        if changed:
            events.emit(
                "spike.rejected",
                entity_type="spike",
                entity_id=spike_id,
                message=reason[:500],
                data={"stage": "vod"},
                service=SERVICE,
            )


def fail_spike(spike_id: str, reason: str) -> None:
    """A confirmed spike whose window could not be downloaded."""
    with db.transaction():
        changed = db.execute(
            "update spikes set status = 'failed' where id = %s and status = 'confirmed'", (spike_id,)
        )
        if changed:
            events.emit(
                "spike.failed",
                level="warn",
                entity_type="spike",
                entity_id=spike_id,
                message=reason[:500],
                data={"stage": "download"},
                service=SERVICE,
            )


def insert_transcript_spike(
    video: Video,
    *,
    start_s: float,
    end_s: float,
    confidence: float,
    reason: str,
    tier: str,
    status: str,
    parts: Mapping[str, Any],
) -> tuple[str, bool]:
    """Store one moment picked from a transcript (mode `transcript`). Returns `(spike id, created)`.

    Idempotent like `spikes.store.persist_spike`: one row per video and start position.
    """
    with db.transaction():
        db.fetch_one(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"spike:transcript:{video.id}:{start_s}",),
        )
        existing = db.fetch_one(
            """
            select id::text as id from spikes
            where mode = 'transcript' and video_id = %s and spike_offset_s = %s
            limit 1
            """,
            (video.id, start_s),
        )
        if existing is not None:
            return str(existing["id"]), False
        row = db.fetch_one(
            """
            insert into spikes
              (mode, video_id, live_session_id, creator_id, spike_offset_s, score_z, parts, msgs_in_bucket,
               unique_chatters, reaction_count, clip_requests, chat_sample, tier, claude_confidence,
               claude_reason, window_start_s, window_end_s, status)
            values ('transcript', %s, %s, %s, %s, %s, %s, 0, 0, 0, 0, '[]'::jsonb, %s, %s, %s, %s, %s, %s)
            returning id::text as id
            """,
            (
                video.id,
                video.live_session_id,
                video.creator_id,
                start_s,
                confidence,
                Jsonb(dict(parts)),
                tier if status == "confirmed" else "rejected",
                confidence,
                reason[:500],
                start_s,
                end_s,
                status,
            ),
        )
        assert row is not None
        spike_id = str(row["id"])
        events.emit(
            f"spike.{status}",
            entity_type="spike",
            entity_id=spike_id,
            message=reason[:500],
            data={
                "mode": "transcript",
                "tier": tier,
                "confidence": confidence,
                "window_start": start_s,
                "window_end": end_s,
                "video_id": video.id,
            },
            service=SERVICE,
        )
        return spike_id, True


def render_job_exists(spike_id: str) -> bool:
    """True when a `clip.render` job was ever queued for this spike, whatever became of it."""
    row = db.fetch_one(
        "select 1 as hit from jobs where type = 'clip.render' and payload->>'spike_id' = %s limit 1",
        (spike_id,),
    )
    return row is not None


def spike_states(spike_ids: Sequence[str]) -> dict[str, str]:
    """`{spike id: status}` for the given ids (unknown ids are left out)."""
    if not spike_ids:
        return {}
    rows = db.fetch_all(
        "select id::text as id, status from spikes where id::text = any(%s)", (list(spike_ids),)
    )
    return {str(row["id"]): str(row["status"]) for row in rows}


# ── events the lane reads back ───────────────────────────────


def has_event(video_id: str, event_type: str) -> bool:
    """True when an event of this type was written for the video."""
    row = db.fetch_one(
        "select 1 as hit from events where type = %s and entity_type = 'video' and entity_id = %s limit 1",
        (event_type, video_id),
    )
    return row is not None


def note_block(video: Video, detail: str, access: str) -> None:
    """Record that `video.platform` refused a download from this machine."""
    events.emit(
        EVENT_BLOCKED,
        level="warn",
        entity_type="video",
        entity_id=video.id,
        message=detail[:500],
        data={"platform": video.platform, "access": access},
        service=SERVICE,
    )


def block_retry_at(platform: str, backoff_s: float, access: str | None = None) -> datetime | None:
    """When downloads of `platform` may be tried again after a refusal, or None when they may be tried
    now. Measured on the database clock, the one clock every worker shares.

    `access` is `Access.described` of this process. A refusal recorded under a different access (before
    the owner set `PROXY_URL` or mounted the cookies file) says nothing about the present one and is
    ignored, so the lane tries again right after the restart instead of waiting out the back-off."""
    row = db.fetch_one(
        """
        select max(created_at) + make_interval(secs => %s) as retry_at,
               max(created_at) + make_interval(secs => %s) > now() as waiting
        from events
        where type = %s and data->>'platform' = %s
          and (%s::text is null or data->>'access' is null or data->>'access' = %s)
        """,
        (backoff_s, backoff_s, EVENT_BLOCKED, platform, access, access),
    )
    if row is None or not row["waiting"]:
        return None
    retry_at: datetime = row["retry_at"]
    return retry_at


def claim_block_notification(platform: str, interval_s: float) -> bool:
    """True for the one caller per `interval_s` that should send the "downloads are blocked" message.

    The claim is an event row written under an advisory lock, so two workers cannot both win.
    """
    with db.transaction():
        db.fetch_one("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (EVENT_BLOCK_NOTIFIED,))
        recent = db.fetch_one(
            """
            select 1 as hit from events
            where type = %s and created_at > now() - make_interval(secs => %s)
            limit 1
            """,
            (EVENT_BLOCK_NOTIFIED, interval_s),
        )
        if recent is not None:
            return False
        events.emit(EVENT_BLOCK_NOTIFIED, level="warn", data={"platform": platform}, service=SERVICE)
        return True


def waiting_videos(platform: str) -> int:
    """Videos of `platform` still waiting for the lane."""
    row = db.fetch_one(
        "select count(*) as n from videos where platform = %s and status = any(%s)",
        (platform, list(OPEN_STATUSES)),
    )
    return int(row["n"]) if row is not None else 0


def transcribed_today_s() -> float:
    """Seconds of audio the transcript fallback has transcribed since 00:00 UTC."""
    row = db.fetch_one(
        """
        select coalesce(sum(units), 0) as total from api_usage
        where operation = %s and created_at >= date_trunc('day', now(), 'UTC')
        """,
        (USAGE_FALLBACK_TRANSCRIBE,),
    )
    return float(row["total"]) if row is not None else 0.0


# ── rows the CLI and the replay pass create ──────────────────


def ensure_creator(platform: str, channel_id: str, handle: str, display_name: str | None) -> str:
    """The id of the creator with this channel, creating the row (tier `auto`) when it is missing."""
    row = db.fetch_one(
        """
        insert into creators (platform, channel_id, handle, display_name)
        values (%s, %s, %s, %s)
        on conflict (platform, channel_id) do update set handle = creators.handle
        returning id::text as id
        """,
        (platform, channel_id, handle, display_name),
    )
    assert row is not None
    return str(row["id"])


def ensure_video(
    *,
    creator_id: str,
    platform: str,
    source_id: str,
    url: str,
    title: str | None,
    duration_s: float | None,
    has_chat: bool | None,
    live_session_id: str | None = None,
) -> str:
    """The id of the video with this source, creating the row (status `new`) when it is missing."""
    row = db.fetch_one(
        """
        insert into videos
          (creator_id, platform, source_id, url, title, duration_s, has_chat, live_session_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (platform, source_id) do update
          set live_session_id = coalesce(videos.live_session_id, excluded.live_session_id)
        returning id::text as id
        """,
        (
            creator_id,
            platform,
            source_id,
            url,
            title,
            int(duration_s) if duration_s else None,
            has_chat,
            live_session_id,
        ),
    )
    assert row is not None
    return str(row["id"])


def video_for_live_session(live_session_id: str) -> str | None:
    """The video of a finished live session (for `replay.scan`), created from the session when missing."""
    try:
        session = db.fetch_one(
            """
            select id::text as id, creator_id::text as creator_id, platform, source_id, vod_source_id, url
            from live_sessions where id = %s
            """,
            (live_session_id,),
        )
    except psycopg.errors.InvalidTextRepresentation:
        return None
    if session is None:
        return None
    source_id = str(session["vod_source_id"] or session["source_id"])
    platform = str(session["platform"])
    url = (
        f"https://www.twitch.tv/videos/{source_id}"
        if platform == "twitch"
        else f"https://www.youtube.com/watch?v={source_id}"
    )
    return ensure_video(
        creator_id=str(session["creator_id"]),
        platform=platform,
        source_id=source_id,
        url=url,
        title=None,
        duration_s=None,
        has_chat=True,
        live_session_id=str(session["id"]),
    )

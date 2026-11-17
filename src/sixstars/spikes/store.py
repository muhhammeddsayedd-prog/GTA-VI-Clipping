"""Writes detected spikes to the `spikes` table and records a `spike.detected` event. Implemented in M04.

Writing is idempotent: one row per (mode, source, peak position). Scanning the same video again, or a
live detector that reports a spike twice after a restart, returns the id of the existing row and changes
nothing (later stages may already have moved its status on).
"""

from datetime import UTC, datetime
from typing import Any, Final

from psycopg.types.json import Jsonb

from sixstars import db, events
from sixstars.logging import get_logger
from sixstars.spikes.detector import DetectedSpike

log = get_logger("spikes")

_LOCK_SQL: Final = "select pg_advisory_xact_lock(hashtextextended(%s, 0))"
_FIND_SQL: Final = """
    select id::text as id from spikes
    where mode = %(mode)s
      and live_session_id is not distinct from %(live_session_id)s::uuid
      and video_id is not distinct from %(video_id)s::uuid
      and spike_wall is not distinct from %(spike_wall)s
      and spike_offset_s is not distinct from %(spike_offset_s)s
    order by detected_at
    limit 1
"""
_INSERT_SQL: Final = """
    insert into spikes (
        mode, live_session_id, video_id, creator_id, spike_wall, spike_offset_s, score_z, parts,
        msgs_in_bucket, unique_chatters, reaction_count, clip_requests, chat_sample, tier,
        window_start_s, window_end_s, window_start_wall, window_end_wall, status, merged_into
    ) values (
        %(mode)s, %(live_session_id)s, %(video_id)s, %(creator_id)s, %(spike_wall)s, %(spike_offset_s)s,
        %(score_z)s, %(parts)s, %(msgs)s, %(uniq)s, %(reaction_count)s, %(clip_requests)s, %(chat_sample)s,
        %(tier)s, %(window_start_s)s, %(window_end_s)s, %(window_start_wall)s, %(window_end_wall)s,
        %(status)s, %(merged_into)s
    )
    returning id::text as id
"""


def _wall(t: float) -> datetime:
    return datetime.fromtimestamp(t, UTC)


def spike_row(
    detected: DetectedSpike,
    *,
    creator_id: str,
    live_session_id: str | None,
    video_id: str | None,
    status: str = "detected",
    merged_into: str | None = None,
) -> dict[str, Any]:
    """The column values of one `spikes` row. Pure.

    Replay spikes store their position and window as offsets in the video (`spike_offset_s`,
    `window_*_s`); live spikes store wall-clock times (`spike_wall`, `window_*_wall`). `parts` holds the
    z-scores plus what the table has no column for: `first_t`, `spike_floor`, `pad_start`, `pad_end` (on
    the chat clock) and the baseline means.
    """
    live = detected.mode == "live"
    parts: dict[str, Any] = {
        **detected.parts,
        "first_t": detected.first_t,
        "spike_floor": detected.spike_floor,
        "pad_start": detected.pad_start,
        "pad_end": detected.pad_end,
        "baseline_mean": detected.baseline_mean,
    }
    return {
        "mode": detected.mode,
        "live_session_id": live_session_id,
        "video_id": video_id,
        "creator_id": creator_id,
        "spike_wall": _wall(detected.peak_t) if live else None,
        "spike_offset_s": None if live else detected.peak_t,
        "score_z": detected.score_z,
        "parts": Jsonb(parts),
        "msgs": detected.msgs,
        "uniq": detected.uniq,
        "reaction_count": detected.reaction_count,
        "clip_requests": detected.clip_requests,
        "chat_sample": Jsonb(detected.chat_sample),
        "tier": detected.tier,
        "window_start_s": None if live else detected.window_start,
        "window_end_s": None if live else detected.window_end,
        "window_start_wall": _wall(detected.window_start) if live else None,
        "window_end_wall": _wall(detected.window_end) if live else None,
        "status": status,
        "merged_into": merged_into,
    }


def _write(row: dict[str, Any]) -> tuple[str, bool]:
    """Insert the row unless the same spike is already stored. Returns (id, created)."""
    source = row["live_session_id"] or row["video_id"]
    position = row["spike_wall"].isoformat() if row["spike_wall"] is not None else row["spike_offset_s"]
    with db.transaction():
        db.fetch_one(_LOCK_SQL, (f"spike:{row['mode']}:{source}:{position}",))
        existing = db.fetch_one(_FIND_SQL, row)
        if existing is not None:
            return str(existing["id"]), False
        created = db.fetch_one(_INSERT_SQL, row)
        assert created is not None
        return str(created["id"]), True


def persist_spike(
    detected: DetectedSpike,
    *,
    creator_id: str,
    live_session_id: str | None = None,
    video_id: str | None = None,
) -> str:
    """Store a detected spike (status `detected`) and return its id.

    Pass `live_session_id` for a live spike or `video_id` for a replay spike (a replay of a recorded
    live session may pass both). The spikes absorbed by a replay merge are stored with status `merged`
    and `merged_into` pointing at this spike. Calling this again for the same spike returns the same id
    and writes nothing.
    """
    if live_session_id is None and video_id is None:
        raise ValueError("persist_spike needs live_session_id or video_id")
    spike_id, created = _write(
        spike_row(detected, creator_id=creator_id, live_session_id=live_session_id, video_id=video_id)
    )
    if not created:
        log.info("spike_already_stored", spike_id=spike_id, mode=detected.mode)
        return spike_id
    for other in detected.absorbed:
        _write(
            spike_row(
                other,
                creator_id=creator_id,
                live_session_id=live_session_id,
                video_id=video_id,
                status="merged",
                merged_into=spike_id,
            )
        )
    data = {
        "mode": detected.mode,
        "tier": detected.tier,
        "score_z": detected.score_z,
        "peak_t": detected.peak_t,
        "msgs": detected.msgs,
        "uniq": detected.uniq,
        "window_start": detected.window_start,
        "window_end": detected.window_end,
        "live_session_id": live_session_id,
        "video_id": video_id,
        "creator_id": creator_id,
        "merged": len(detected.absorbed),
    }
    events.emit(
        "spike.detected",
        entity_type="spike",
        entity_id=spike_id,
        message=f"{detected.mode} spike, tier {detected.tier}, score {detected.score_z:.2f}",
        data=data,
        service="spikes",
    )
    log.info("spike_detected", spike_id=spike_id, **{k: v for k, v in data.items() if k != "creator_id"})
    return spike_id

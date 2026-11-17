"""Database access of the live lane: `live_sessions` rows and their status changes, the creator's tier,
spike bookkeeping and the clipped ranges. Every status change writes an `events` row.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from psycopg.types.json import Jsonb

from sixstars import db, events
from sixstars.config import env

SERVICE: Final = "live"
FORBIDDEN_TIERS: Final[frozenset[str]] = frozenset({"restricted", "blocked"})
MANAGED_STATUSES: Final[tuple[str, ...]] = ("assigned", "recording", "released")

_SESSION_SQL: Final = """
    select s.id::text as id, s.creator_id::text as creator_id, s.platform, s.source_id, s.channel_login,
           s.url, s.status, s.reconnects, s.assigned_at, s.started_at, s.bot_names, s.vod_source_id,
           s.trend_score, c.handle, c.channel_id, c.tier
    from live_sessions s join creators c on c.id = s.creator_id
"""


@dataclass(frozen=True, slots=True)
class SessionRow:
    """One `live_sessions` row with what the lane needs of its creator."""

    id: str
    creator_id: str
    platform: str
    source_id: str
    channel_login: str
    url: str
    status: str
    reconnects: int
    assigned_at: datetime
    started_at: datetime | None
    bot_names: tuple[str, ...]
    vod_source_id: str | None
    handle: str
    channel_id: str
    tier: str
    trend_score: float | None = None

    @property
    def forbidden(self) -> bool:
        return self.tier in FORBIDDEN_TIERS


def _session(row: Mapping[str, Any]) -> SessionRow:
    return SessionRow(
        id=str(row["id"]),
        creator_id=str(row["creator_id"]),
        platform=str(row["platform"]),
        source_id=str(row["source_id"]),
        channel_login=str(row["channel_login"]),
        url=str(row["url"]),
        status=str(row["status"]),
        reconnects=int(row["reconnects"] or 0),
        assigned_at=row["assigned_at"],
        started_at=row["started_at"],
        bot_names=tuple(row["bot_names"] or ()),
        vod_source_id=row["vod_source_id"],
        handle=str(row["handle"]),
        channel_id=str(row["channel_id"]),
        tier=str(row["tier"]),
        trend_score=float(row["trend_score"]) if row["trend_score"] is not None else None,
    )


def live_root() -> Path:
    """`<DATA_DIR>/live`: one directory per session below it."""
    return env().DATA_DIR / "live"


def buffer_dir(session_id: str) -> Path:
    return live_root() / session_id


def managed_sessions() -> list[SessionRow]:
    """Sessions the manager must look at: `assigned`, `recording` and `released`, best first."""
    rows = db.fetch_all(
        _SESSION_SQL + " where s.status = any(%s) order by s.trend_score desc nulls last, s.assigned_at",
        (list(MANAGED_STATUSES),),
    )
    return [_session(row) for row in rows]


def get_session(session_id: str) -> SessionRow | None:
    row = db.fetch_one(_SESSION_SQL + " where s.id = %s", (session_id,))
    return _session(row) if row is not None else None


def creator_tier(creator_id: str) -> str | None:
    """The creator's tier now (read straight from the table, not from a cache)."""
    row = db.fetch_one("select tier from creators where id = %s", (creator_id,))
    return str(row["tier"]) if row is not None else None


def emit(
    event: str,
    session_id: str,
    message: str,
    data: Mapping[str, Any] | None = None,
    level: events.Level = "info",
) -> None:
    events.emit(
        event,
        level=level,
        entity_type="live_session",
        entity_id=session_id,
        message=message[:500],
        data=data,
        service=SERVICE,
    )


def mark_recording(session: SessionRow, now: datetime, directory: Path, *, resumed: bool) -> bool:
    """`assigned` (or a `recording` row being resumed after a restart) → `recording`. `started_at` is set
    the first time only, so wall time minus `started_at` stays the position in the stream. Returns False
    when the row changed in the meantime (released by discovery)."""
    changed = db.execute(
        """
        update live_sessions
        set status = 'recording', started_at = coalesce(started_at, %s), buffer_dir = %s
        where id = %s and status = any(%s)
        """,
        (now, str(directory), session.id, ["assigned", "recording"]),
    )
    if changed:
        emit(
            "live.resumed" if resumed else "live.recording",
            session.id,
            f"{'resumed' if resumed else 'started'} recording {session.platform} {session.handle}",
            {"platform": session.platform, "source_id": session.source_id, "creator_id": session.creator_id},
        )
    return bool(changed)


def add_reconnect(session_id: str, kind: str, detail: str, attempt: int) -> None:
    db.execute("update live_sessions set reconnects = reconnects + 1 where id = %s", (session_id,))
    emit(
        "live.reconnect",
        session_id,
        f"recorder restart {attempt} after {kind}: {detail}",
        {"kind": kind, "attempt": attempt},
        level="warn",
    )


def save_bots(session_id: str, names: frozenset[str]) -> None:
    db.execute("update live_sessions set bot_names = %s where id = %s", (sorted(names), session_id))


def finish(
    session_id: str, status: str, now: datetime, reason: str, data: Mapping[str, Any] | None = None
) -> bool:
    """Close a session the manager was recording or about to record: `ended` (the stream is over or the
    slot was released after recording), `failed` (recording could not be kept up) or `released` (nothing
    was ever recorded: discovery may assign the stream again or queue its replay itself)."""
    if status not in ("ended", "failed", "released"):
        raise ValueError(f"cannot finish a session as {status!r}")
    changed = db.execute(
        """
        update live_sessions
        set status = %s, ended_at = case when %s = 'released' then ended_at else %s end, buffer_dir = null
        where id = %s and status = any(%s)
        """,
        (status, status, now, session_id, list(MANAGED_STATUSES)),
    )
    if changed:
        emit(
            f"live.{status}",
            session_id,
            reason,
            data,
            level="warn" if status == "failed" else "info",
        )
    return bool(changed)


def set_replay_queued(session_id: str, vod_source_id: str, job_id: int | None, run_after: datetime) -> None:
    db.execute(
        "update live_sessions set status = 'replay_queued', vod_source_id = %s "
        "where id = %s and status = 'ended'",
        (vod_source_id, session_id),
    )
    emit(
        "live.replay_queued",
        session_id,
        f"replay scan queued for {vod_source_id}",
        {"vod_source_id": vod_source_id, "job_id": job_id, "run_after": run_after.isoformat()},
    )


def add_clipped_range(session_id: str, start_wall: float, end_wall: float, clip_id: str) -> bool:
    """Append a clipped window to `live_sessions.clipped_ranges` (once per clip), so the replay pass does
    not clip the same moment again."""
    entry = {
        "start_wall": datetime.fromtimestamp(start_wall, UTC).isoformat(),
        "end_wall": datetime.fromtimestamp(end_wall, UTC).isoformat(),
        "clip_id": clip_id,
    }
    changed = db.execute(
        """
        update live_sessions
        set clipped_ranges = clipped_ranges || %s::jsonb
        where id = %s and not clipped_ranges @> %s::jsonb
        """,
        (Jsonb([entry]), session_id, Jsonb([{"clip_id": clip_id}])),
    )
    return bool(changed)


def close_spike(spike_id: str, status: str, reason: str, *, event: str, level: events.Level = "info") -> None:
    """Close a spike that will not become a clip (`rejected` or `failed`) and say why."""
    db.execute(
        "update spikes set status = %s, claude_reason = coalesce(claude_reason, %s) where id = %s",
        (status, reason[:500], spike_id),
    )
    events.emit(
        event, level=level, entity_type="spike", entity_id=spike_id, message=reason[:500], service=SERVICE
    )


def emergency_stop() -> bool:
    """`control.emergency_stop`, read straight from the table (not from the 30-second cache)."""
    row = db.fetch_one("select value from settings where key = 'control.emergency_stop'")
    return bool(row is not None and row["value"] is True)


def recording_sessions_count() -> int:
    row = db.fetch_one("select count(*) as n from live_sessions where status = 'recording'")
    return int(row["n"]) if row is not None else 0


def finished_spikes(spike_ids: list[str]) -> list[str]:
    """Those of `spike_ids` whose render is over (or that no longer exist): their cut file is not needed
    any more."""
    rows = db.fetch_all(
        """
        select k.id
        from unnest(%s::text[]) as k(id)
        left join spikes s on s.id::text = k.id
        where s.id is null or s.status = any(%s)
        """,
        (spike_ids, ["clipped", "rejected", "failed", "merged"]),
    )
    return [str(row["id"]) for row in rows]

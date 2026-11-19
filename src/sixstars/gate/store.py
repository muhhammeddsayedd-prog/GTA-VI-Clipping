"""Database side of the quality gate: load the inputs of the decision, store its result.

Everything that changes a clip happens inside `decision_lock()`: one transaction that first takes an
advisory lock, so two gates never decide at the same time and the duplicate checks of the second one see
what the first one stored.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db, events
from sixstars.factory.layout import LayoutKind
from sixstars.gate import visual as visuals
from sixstars.gate.models import (
    EXPRESS,
    HOURLY,
    LANE_SPIKE,
    PROFILES,
    ClipFacts,
    CreatorFacts,
    FileFacts,
    GateDecision,
    GateSettings,
    RecentClip,
    Verdict,
    VisualFindings,
)
from sixstars.posting.precedence import demoted_rank

LOCK_NAME = "sixstars.gate.decide"
STATUS_GATING = "gating"
STATUS_PRECHECK = "precheck_pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
# Other clips of the creator that count as "already has a clip" for the first-clip risk rule.
PRIOR_STATUSES = ("approved", "posted", "removed", "expired")
# Other clips that count toward the daily cap of an auto-tier creator besides those posted today.
WAITING_STATUSES = ("approved", "precheck_pending")
RECENT_LIMIT = 1000

_CLIP_SQL = """
select c.id::text as id, c.creator_id::text as creator_id, c.spike_id::text as spike_id, c.status, c.lane,
       c.gate_profile, c.source_platform, c.source_id, c.start_s, c.end_s, c.duration_s, c.score,
       c.score_parts, c.music_ratio, c.spoiler, c.safety_flags, c.hook, c.title, c.caption, c.hashtags,
       c.topic, c.commentary,
       c.transcript, c.local_path, c.storage_path, c.gate_result, c.precheck_video_id, c.created_at,
       c.layout_kind, c.version, c.parent_clip_id::text as parent_clip_id
from clips c
"""


@dataclass(frozen=True, slots=True)
class ClipRow:
    """One `clips` row as the gate reads it."""

    id: str
    creator_id: str
    spike_id: str | None
    status: str
    lane: str
    profile: str
    local_path: str | None
    storage_path: str | None
    duration_s: float | None
    gate_result: Mapping[str, Any] | None
    precheck_video_id: str | None
    raw: Mapping[str, Any]
    layout_kind: LayoutKind | None = None  # migration 0027; None for older clips
    version: str = "short"  # migration 0029: "story" for the longer cut of `parent_clip_id`
    parent_clip_id: str | None = None

    @property
    def family_id(self) -> str:
        """The short clip of this clip's family (itself, or the clip it is the story cut of)."""
        return self.parent_clip_id or self.id


def _layout_kind(value: object) -> LayoutKind | None:
    return value if value in ("A", "B", "C") else None


def _float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def transcript_words(transcript: object) -> list[tuple[str, float, float]]:
    """(word, start, end) of the stored transcript (`{"words": [{"w", "s", "e"}]}`); malformed entries are
    left out."""
    if not isinstance(transcript, Mapping):
        return []
    listed = transcript.get("words")
    words: list[tuple[str, float, float]] = []
    for item in listed if isinstance(listed, list) else []:
        if not isinstance(item, Mapping):
            continue
        start, end = item.get("s"), item.get("e")
        if isinstance(start, int | float) and isinstance(end, int | float):
            words.append((str(item.get("w") or ""), float(start), float(end)))
    return words


def transcript_text(transcript: object) -> str:
    return " ".join(word for word, _, _ in transcript_words(transcript) if word)


def profile_of(row: Mapping[str, Any]) -> str:
    """`clips.gate_profile`, or the profile of the lane when the column is empty."""
    profile = row.get("gate_profile")
    if profile in PROFILES:
        return str(profile)
    return EXPRESS if row.get("lane") == LANE_SPIKE else HOURLY


def get_clip(clip_id: str) -> ClipRow | None:
    row = db.fetch_one(_CLIP_SQL + " where c.id = %s", (clip_id,))
    if row is None:
        return None
    result = row["gate_result"]
    return ClipRow(
        id=str(row["id"]),
        creator_id=str(row["creator_id"]),
        spike_id=row["spike_id"],
        status=str(row["status"]),
        lane=str(row["lane"]),
        profile=profile_of(row),
        local_path=row["local_path"],
        storage_path=row["storage_path"],
        duration_s=_float(row["duration_s"]),
        gate_result=result if isinstance(result, Mapping) else None,
        precheck_video_id=row["precheck_video_id"],
        raw=row,
        layout_kind=_layout_kind(row.get("layout_kind")),
        version=str(row.get("version") or "short"),
        parent_clip_id=row.get("parent_clip_id"),
    )


def music_frames(clip_id: str) -> tuple[bool, ...] | None:
    """The detector's per-second music flags, from the latest `clip.rendered` event of the clip."""
    row = db.fetch_one(
        "select data from events where type = 'clip.rendered' and entity_type = 'clip' and entity_id = %s "
        "order by id desc limit 1",
        (clip_id,),
    )
    data = row["data"] if row else None
    music = data.get("music") if isinstance(data, Mapping) else None
    frames = music.get("frames") if isinstance(music, Mapping) else None
    if not isinstance(frames, list) or not frames or not all(flag in (0, 1, True, False) for flag in frames):
        return None
    return tuple(bool(flag) for flag in frames)


def spike_row(spike_id: str | None) -> Mapping[str, Any] | None:
    """What the visual score needs of the chat spike a clip was cut around; None when there is none."""
    if not spike_id:
        return None
    return db.fetch_one(
        "select mode, spike_offset_s, msgs_in_bucket, unique_chatters, reaction_count, tier, chat_sample "
        "from spikes where id = %s",
        (spike_id,),
    )


def audience_row(clip_id: str, spike_id: str | None) -> Mapping[str, Any] | None:
    """What Twitch knew about the viewer-made clip this clip was rendered from (`viewer_clips`, whose id
    is the clip's id; decision D110): title, views and when each was read. None for every other clip: one
    with a chat spike is not asked about, and a database without the table (before migration 0019) has
    no such clips."""
    if spike_id:
        return None
    present = db.fetch_one("select to_regclass('public.viewer_clips') is not null as present")
    if present is None or not present["present"]:
        return None
    return db.fetch_one(
        "select title, view_count, clip_created_at, created_at from viewer_clips where id::text = %s",
        (clip_id,),
    )


def store_visual_score(clip_id: str, score: int, score_parts: Mapping[str, object]) -> bool:
    """Write the visual score on a clip that is still in `gating`, with its `clip.scored` event.
    `score_parts` is the whole new document (see `gate.rescore.score_document`). Returns False, and
    writes nothing, when the clip left `gating` in the meantime."""
    with db.transaction():
        changed = db.execute(
            "update clips set score = %s, score_parts = %s where id = %s and status = %s",
            (score, Jsonb(dict(score_parts)), clip_id, STATUS_GATING),
        )
        if changed:
            events.emit(
                "clip.scored",
                entity_type="clip",
                entity_id=clip_id,
                message=f"visual score {score}",
                data={"score": score, "score_parts": dict(score_parts)},
                service="gate",
            )
    return bool(changed)


def clip_facts(row: ClipRow, file: FileFacts | None, frames: tuple[bool, ...] | None) -> ClipFacts:
    """The pure decision's view of the row."""
    raw = row.raw
    words = transcript_words(raw["transcript"])
    flags = raw["safety_flags"]
    return ClipFacts(
        id=row.id,
        creator_id=row.creator_id,
        profile=row.profile,
        lane=row.lane,
        source_platform=str(raw["source_platform"]),
        source_id=str(raw["source_id"]),
        start_s=float(raw["start_s"]),
        end_s=float(raw["end_s"]),
        duration_s=row.duration_s,
        score=_float(raw["score"]),
        music_ratio=_float(raw["music_ratio"]),
        spoiler=raw["spoiler"],
        safety_flags=flags if isinstance(flags, Mapping) else None,
        hook=raw["hook"],
        title=raw["title"],
        caption=raw["caption"],
        hashtags=tuple(str(tag) for tag in (raw["hashtags"] or ())),
        topic=raw["topic"],
        transcript_text=" ".join(word for word, _, _ in words if word),
        word_spans=tuple((start, end) for _, start, end in words),
        created_at=_utc(raw["created_at"]),
        file=file,
        music_frames=frames,
        commentary=raw["commentary"],
    )


def creator_facts(creator_id: str, clip_id: str, now: datetime) -> CreatorFacts:
    """Tier and clip counts of the creator, not counting the clip being judged. A creator row that is
    missing gives an empty tier, which the gate refuses."""
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    row = db.fetch_one(
        """
        select cr.tier,
          (select count(*) from clips c where c.creator_id = cr.id and c.id <> %(clip)s
             and ((c.status = 'posted' and c.posted_at >= %(day)s) or c.status = any(%(waiting)s))) as today,
          (select count(*) from clips c where c.creator_id = cr.id and c.id <> %(clip)s
             and c.status = any(%(prior)s)) as prior
        from creators cr where cr.id = %(creator)s
        """,
        {
            "clip": clip_id,
            "creator": creator_id,
            "day": day_start,
            "waiting": list(WAITING_STATUSES),
            "prior": list(PRIOR_STATUSES),
        },
    )
    if row is None:
        return CreatorFacts(id=creator_id, tier="")
    return CreatorFacts(
        id=creator_id, tier=str(row["tier"]), clips_today=int(row["today"]), prior_clips=int(row["prior"])
    )


def recent_clips(row: ClipRow, settings: GateSettings, now: datetime) -> list[RecentClip]:
    """Other clips the duplicate checks must see: every clip of the same video, and every clip decided
    inside the longer of the two topic windows."""
    window_h = max(settings.hourly.topic_dedupe_h, settings.express.topic_dedupe_h)
    rows = db.fetch_all(
        """
        select c.id::text as id, c.creator_id::text as creator_id, c.status, c.source_platform, c.source_id,
               c.start_s, c.end_s, c.topic, c.transcript, c.created_at,
               coalesce(greatest(c.approved_at, c.posted_at), c.created_at) as decided_at
        from clips c
        where c.id <> %(clip)s and c.status not in ('rejected', 'rendering')
          and coalesce(c.parent_clip_id, c.id) <> %(family)s::uuid
          and ((c.source_platform = %(platform)s and c.source_id = %(source)s)
               or coalesce(greatest(c.approved_at, c.posted_at), c.created_at) >= %(since)s)
        order by c.created_at desc
        limit %(limit)s
        """,
        {
            "clip": row.id,
            "family": row.family_id,
            "platform": row.raw["source_platform"],
            "source": row.raw["source_id"],
            "since": now - timedelta(hours=window_h),
            "limit": RECENT_LIMIT,
        },
    )
    return [
        RecentClip(
            id=str(item["id"]),
            creator_id=str(item["creator_id"]),
            status=str(item["status"]),
            source_platform=str(item["source_platform"]),
            source_id=str(item["source_id"]),
            start_s=float(item["start_s"]),
            end_s=float(item["end_s"]),
            topic=item["topic"],
            transcript_text=transcript_text(item["transcript"]),
            decided_at=_utc(item["decided_at"]),
            created_at=_utc(item["created_at"]),
        )
        for item in rows
    ]


@contextmanager
def decision_lock() -> Iterator[None]:
    """One transaction holding the gate's advisory lock until it commits."""
    with db.transaction():
        db.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (LOCK_NAME,))
        yield


def file_as_json(file: FileFacts | None) -> dict[str, object] | None:
    if file is None:
        return None
    return {
        "width": file.width,
        "height": file.height,
        "has_video": file.has_video,
        "has_audio": file.has_audio,
        "duration_s": file.duration_s,
        "size_bytes": file.size_bytes,
        "longest_black_s": file.longest_black_s,
        "longest_freeze_s": file.longest_freeze_s,
    }


def _optional_number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def file_from_json(data: object) -> FileFacts | None:
    """The stored measurements back into `FileFacts`; None when they are missing or incomplete."""
    if not isinstance(data, Mapping):
        return None
    width, height, size = data.get("width"), data.get("height"), data.get("size_bytes")
    if not isinstance(width, int) or not isinstance(height, int):
        return None
    return FileFacts(
        width=width,
        height=height,
        has_video=data.get("has_video") is True,
        has_audio=data.get("has_audio") is True,
        duration_s=_optional_number(data.get("duration_s")),
        size_bytes=size if isinstance(size, int) and not isinstance(size, bool) else None,
        longest_black_s=_optional_number(data.get("longest_black_s")),
        longest_freeze_s=_optional_number(data.get("longest_freeze_s")),
    )


def result_document(
    decision: GateDecision,
    file: FileFacts | None,
    visual: VisualFindings | None,
    now: datetime,
    *,
    p5_skipped: bool = False,
) -> dict[str, object]:
    """`clips.gate_result`: the decision plus the measurements it was made from, so that the pre-check
    result can be judged later without measuring again."""
    document = decision.as_json()
    document["decided_at"] = now.isoformat()
    document["inputs"] = {
        "file": file_as_json(file),
        "visual": None if visual is None or p5_skipped else visuals.findings_as_json(visual),
        "p5": "skipped: the clip was rejected without it" if p5_skipped else "run",
    }
    return document


def event_data(decision: GateDecision) -> dict[str, object]:
    return {
        "verdict": decision.verdict.value,
        "profile": decision.profile,
        "lane": decision.lane,
        "codes": [code.value for code in decision.codes],
        "reasons": list(decision.reasons),
        "demoted": decision.demoted,
        "express_codes": [code.value for code in decision.express_codes],
        "risk": list(decision.risk_reasons),
        "precheck": decision.precheck.value,
    }


def store_decision(
    row: ClipRow,
    decision: GateDecision,
    document: Mapping[str, object],
    now: datetime,
    *,
    boosted: bool = False,
    rank_bonus: float = 0.0,
) -> bool:
    """Write the verdict on the clip and its event. Call inside `decision_lock()`.

    `rank_bonus` (the first-time boost's rank points) is added to `rank_score`.

    The ranking stub of M07: `rank_score` is the clip's score, or the posting engine's demotion rank when
    the clip came from the spike lane (`boosted`), so that it goes to the top of the hourly queue.
    Returns False (and writes nothing) when the clip left the status it was loaded in.
    """
    score = _float(row.raw["score"])
    if decision.verdict is Verdict.APPROVE:
        rank = None if score is None else (demoted_rank(score) if boosted else score) + rank_bonus
        changed = db.execute(
            """
            update clips set status = 'approved', approved_at = %s, lane = %s, gate_profile = %s,
                   gate_result = %s, reject_reason = null, rank_score = %s
            where id = %s and status = %s
            """,
            (now, decision.lane, decision.profile, Jsonb(document), rank, row.id, row.status),
        )
        event, level, message = "clip.approved", "info", f"approved for the {decision.lane} lane"
    elif decision.verdict is Verdict.HOLD:
        changed = db.execute(
            """
            update clips set status = 'precheck_pending', needs_precheck = true, lane = %s,
                   gate_profile = %s, gate_result = %s, reject_reason = null
            where id = %s and status = %s
            """,
            (decision.lane, decision.profile, Jsonb(document), row.id, row.status),
        )
        event, level, message = "clip.precheck_pending", "info", "; ".join(decision.reasons)
    else:
        changed = db.execute(
            "update clips set status = 'rejected', gate_result = %s, reject_reason = %s "
            "where id = %s and status = %s",
            (Jsonb(document), decision.reject_reason, row.id, row.status),
        )
        event, level, message = "clip.rejected", "info", "; ".join(decision.reasons)
    if not changed:
        return False
    events.emit(
        event,
        level=level,  # type: ignore[arg-type]
        entity_type="clip",
        entity_id=row.id,
        message=message[:500],
        data=event_data(decision),
        service="gate",
    )
    return True


def reject_for_error(clip_id: str, error: str) -> bool:
    """Close a clip the gate could not judge (after the last attempt): it is rejected, never approved."""
    document = {"passed": False, "verdict": "reject", "codes": ["gate_error"], "error": error[:500]}
    with decision_lock():
        changed = db.execute(
            "update clips set status = 'rejected', reject_reason = 'gate_error', gate_result = %s "
            "where id = %s and status in ('gating', 'precheck_pending')",
            (Jsonb(document), clip_id),
        )
        if changed:
            events.emit(
                "clip.rejected",
                level="error",
                entity_type="clip",
                entity_id=clip_id,
                message=f"the gate failed: {error[:300]}",
                data={"verdict": "reject", "codes": ["gate_error"], "reasons": [error[:300]]},
                service="gate",
            )
    return bool(changed)


def gating_clip_ids(older_than_s: float = 0.0) -> list[str]:
    """Clips still waiting for the gate whose row has not changed for `older_than_s` seconds (database
    clock on both sides, so a clock difference between machines does not matter)."""
    rows = db.fetch_all(
        "select id::text as id from clips where status = 'gating' "
        "and updated_at <= now() - make_interval(secs => %s) order by created_at",
        (max(0.0, older_than_s),),
    )
    return [str(row["id"]) for row in rows]

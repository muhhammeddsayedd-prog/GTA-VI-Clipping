"""Worker-side pieces of the live lane (registered through `services.worker.HANDLER_MODULES`).

The render of a live spike is the factory's own job (`express.render` for S tier on the `express` queue,
`clip.render` for A and B on the `normal` queue) and the step after it is the gate's hook, which also
hands an approved spike clip to `posting.spike.try_post_spike`. This module wraps that hook for clips that
came from a live session (the job payload carries a `live` object, written by `live/session.py`):

- before the gate: the clipped window is appended to `live_sessions.clipped_ranges`, so the replay pass
  does not clip the moment again;
- after the gate: one `live.express_done` event with the wall time of every stage from the chat peak to
  the posting decision. `spikes.spike_to_post_s` itself is written by the posting engine when a post is
  really published.

It also owns `replay.find`: after a Twitch stream ends, look up the archive whose `stream_id` is the
session's stream, then queue the `replay.scan` job the VOD lane handles.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sixstars import db, events, jobs
from sixstars.clients.twitch import TwitchClient, TwitchVideo
from sixstars.factory import pipeline
from sixstars.live import store
from sixstars.live.config import LiveConfig
from sixstars.live.manager import JOB_REPLAY_FIND, REPLAY_PRIORITY, queue_replay_scan
from sixstars.logging import get_logger

ARCHIVES_TO_CHECK: Final = 5

_twitch_factory: Callable[[], TwitchClient] = TwitchClient


def set_twitch_factory(factory: Callable[[], TwitchClient] | None) -> None:
    """Replace the Twitch client used by `replay.find` (tests pass a fake); None restores the real one."""
    global _twitch_factory
    _twitch_factory = factory or TwitchClient


# ── after a live clip rendered ───────────────────────────────


def _number(info: Mapping[str, Any], key: str) -> float | None:
    value = info.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def stage_timings(
    info: Mapping[str, Any], *, claimed_at: float | None, rendered_at: float, decided_at: float
) -> dict[str, Any]:
    """Wall time of every stage of a live spike, from the chat peak to the posting decision. Pure.

    `info` is the `live` object of the job payload. Times are unix seconds; results are milliseconds,
    plus `spike_to_decision_s`.
    """
    before = info.get("timings_ms")
    stages: dict[str, Any] = dict(before) if isinstance(before, Mapping) else {}
    queued_at = _number(info, "queued_at")
    if queued_at is not None and claimed_at is not None:
        stages["queue_wait_ms"] = int(max(claimed_at - queued_at, 0.0) * 1000)
    render_from = claimed_at if claimed_at is not None else queued_at
    if render_from is not None:
        stages["render_ms"] = int(max(rendered_at - render_from, 0.0) * 1000)
    stages["gate_post_ms"] = int(max(decided_at - rendered_at, 0.0) * 1000)
    spike_wall = _number(info, "spike_wall")
    if spike_wall is not None:
        stages["spike_to_decision_s"] = round(max(decided_at - spike_wall, 0.0), 1)
    return stages


def _note_rendered(clip_id: str, info: Mapping[str, Any]) -> None:
    session_id, start, end = info.get("session_id"), _number(info, "start_wall"), _number(info, "end_wall")
    if isinstance(session_id, str) and start is not None and end is not None and end > start:
        store.add_clipped_range(session_id, start, end, clip_id)


def _decision(clip_id: str) -> dict[str, Any]:
    clip = db.fetch_one("select status, lane, reject_reason from clips where id = %s", (clip_id,))
    posts = db.fetch_all(
        "select account_id, status, dry_run from posts where clip_id = %s order by created_at", (clip_id,)
    )
    return {
        "clip_status": clip["status"] if clip else None,
        "lane": clip["lane"] if clip else None,
        "reject_reason": clip["reject_reason"] if clip else None,
        "posts": [
            {"account": row["account_id"], "status": row["status"], "dry_run": bool(row["dry_run"])}
            for row in posts
        ],
    }


def after_render(clip_id: str, job: jobs.Job) -> None:
    """The step after a successful render: note the clipped range of a live clip, run the gate's hook
    (which posts an approved spike clip), then record the latency. Never raises."""
    from sixstars.gate import gate

    log = get_logger("live")
    info = job.payload.get("live")
    live = info if isinstance(info, Mapping) else None
    rendered_at = time.time()
    if live is not None:
        try:
            _note_rendered(clip_id, live)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop the gate
            log.error("live_clipped_range_failed", clip_id=clip_id, error=type(exc).__name__)
    gate.after_render(clip_id, job)
    if live is None:
        return
    try:
        claimed_at = job.locked_at.timestamp() if job.locked_at is not None else None
        stages = stage_timings(live, claimed_at=claimed_at, rendered_at=rendered_at, decided_at=time.time())
        decision = _decision(clip_id)
        events.emit(
            "live.express_done" if job.type == pipeline.JOB_EXPRESS_RENDER else "live.clip_done",
            entity_type="clip",
            entity_id=clip_id,
            message=(
                f"spike to posting decision in {stages.get('spike_to_decision_s')} s; clip "
                f"{decision['clip_status']} in lane {decision['lane']}"
            ),
            data={"spike_id": job.payload.get("spike_id"), "stages": stages, **decision},
            service="live",
        )
        log.info(
            "live_clip_done",
            clip_id=clip_id,
            spike_id=job.payload.get("spike_id"),
            job_id=job.id,
            clip_status=decision["clip_status"],
            lane=decision["lane"],
            posts=len(decision["posts"]),
            **stages,
        )
    except Exception as exc:  # noqa: BLE001 - measuring must not fail the job
        log.error("live_latency_failed", clip_id=clip_id, error=type(exc).__name__)


# ── replay.find ──────────────────────────────────────────────


def match_archive(videos: Sequence[TwitchVideo], stream_id: str) -> TwitchVideo | None:
    """The archive of the stream `stream_id` among a broadcaster's newest videos, or None. Pure."""
    for video in videos:
        if video.stream_id is not None and str(video.stream_id) == str(stream_id):
            return video
    return None


def _deadline(payload: Mapping[str, Any]) -> datetime | None:
    value = payload.get("deadline")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def handle_replay_find(job: jobs.Job) -> None:
    """Job `replay.find` (queue `normal`): `{"live_session_id", "deadline", "attempt"}`. Looks for the
    Twitch archive of an ended session every `live.replay_find_interval_s` until the deadline."""
    session_id = str(job.payload.get("live_session_id") or "")
    row = store.get_session(session_id) if session_id else None
    if row is None or row.status != "ended":
        return
    now = datetime.now(UTC)
    client = _twitch_factory()
    if not client.is_configured():
        store.emit(
            "live.replay_missing",
            session_id,
            "Twitch keys are not set: the archive cannot be looked up",
            level="warn",
        )
        return
    found = match_archive(
        client.get_videos_by_user(row.channel_id, "archive", ARCHIVES_TO_CHECK), row.source_id
    )
    if found is not None:
        queue_replay_scan(session_id, found.id, now)
        return
    cfg = LiveConfig.load()
    deadline = _deadline(job.payload)
    again = now + timedelta(seconds=cfg.replay_find_interval_s)
    if deadline is None or again > deadline:
        store.emit(
            "live.replay_missing",
            session_id,
            "no Twitch archive appeared for the stream (archives may be off)",
            level="warn",
        )
        return
    attempt = int(job.payload.get("attempt") or 0) + 1
    jobs.enqueue(
        "normal",
        JOB_REPLAY_FIND,
        {**job.payload, "attempt": attempt},
        priority=REPLAY_PRIORITY,
        run_after=again,
        dedupe_key=f"replay.find:{session_id}:{attempt}",
    )


def register() -> None:
    """Attach the live lane to the worker (safe to call more than once): the wrapped after-render hook
    and the `replay.find` handler."""
    from sixstars.gate import gate
    from sixstars.services import worker

    gate.install()
    for job_type in (pipeline.JOB_CLIP_RENDER, pipeline.JOB_EXPRESS_RENDER):
        pipeline.set_after_render(job_type, after_render)
    if worker.HANDLERS.get(JOB_REPLAY_FIND) is not handle_replay_find:
        worker.register(JOB_REPLAY_FIND, handle_replay_find)

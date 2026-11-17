"""Runs the quality gate for one clip: load the inputs, call the pure decision, store the verdict, hand over.

    run_gate(clip_id) -> GateOutcome

- A clip in `gating` becomes `approved`, `rejected` or `precheck_pending`, with the verdict in
  `clips.gate_result`, the first failed rule in `clips.reject_reason` and one event (`clip.approved`,
  `clip.rejected`, `clip.precheck_pending`). A clip in any other status is left alone.
- An approved clip gets `rank_score` (its score; the posting engine's demotion boost when an express clip
  was moved to the hourly lane), is uploaded to Storage and, in the spike lane, is handed to
  `posting.spike.try_post_spike`. Approved hourly clips wait in the queue for the hourly slot.
- A held clip gets a `clip.precheck` job.
- Rejections are never sent to Telegram: they are `clip.rejected` events with the reason codes, for the
  daily report. Only a gate that keeps failing (not a verdict) raises a warning.

`install()` attaches the gate to the factory (`pipeline.set_after_render`) and registers the job handlers;
the worker process must call it once at start.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from sixstars import events, jobs, notify
from sixstars import storage as clip_storage
from sixstars.ai.calls import BudgetExceededError
from sixstars.clients.anthropic_client import AnthropicClient, BudgetKind
from sixstars.clients.upload_post import UploadPostClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.factory import pipeline, tools
from sixstars.gate import profiles, store
from sixstars.gate import visual as visuals
from sixstars.gate.decision import decide
from sixstars.gate.models import (
    EXPRESS,
    LANE_SPIKE,
    FileFacts,
    GateDecision,
    GateSettings,
    PrecheckState,
    Verdict,
    VisualFindings,
)
from sixstars.logging import get_logger
from sixstars.posting.spike import SpikeRun, try_post_spike
from sixstars.precheck import state as precheck_state

JOB_GATE = "clip.gate"
JOB_PRECHECK = "clip.precheck"
JOB_POST_RETRY = "post.retry"
RETRY_DELAY_S = 30
HANDOFF_RETRY_PRIORITY = 90

# A verdict that stands whatever P5 says is reached with this "nothing wrong" stand-in, to save the call.
_NOTHING_SEEN = VisualFindings(
    other_watermark=False,
    platform_logos=False,
    menu_or_loading=False,
    pause_or_brb_screen=False,
    empty_facecam=False,
    readable_subtitles=True,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class GateDeps:
    """Replaceable collaborators (tests pass fakes). None means the real one, built on first use."""

    ai_client: AnthropicClient | None = None
    youtube: YouTubeClient | None = None
    storage: clip_storage.ClipStorage | None = None
    upload_post: Callable[[], UploadPostClient] = UploadPostClient
    clock: Callable[[], datetime] = utcnow
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    upload_to_storage: bool = True
    hand_off: bool = True


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """What `run_gate` did. `status` is the clip status afterwards; `decision` is None when the clip was
    not in a status the gate handles (`skipped` says why)."""

    clip_id: str
    status: str
    decision: GateDecision | None = None
    skipped: str | None = None
    storage_path: str | None = None
    spike_run: SpikeRun | None = None
    handoff_error: str | None = None
    precheck_job_id: int | None = None


# The collaborators used by the factory hook and the job handlers. Tests replace it with `configure`.
_deps: GateDeps = GateDeps()


def configure(deps: GateDeps | None) -> None:
    """Set (or reset, with None) the collaborators of the hook and the job handlers."""
    global _deps  # noqa: PLW0603
    _deps = deps or GateDeps()


def current_deps() -> GateDeps:
    return _deps


def _measure(row: store.ClipRow, settings: GateSettings, deps: GateDeps) -> FileFacts | None:
    if not row.local_path:
        return None
    try:
        return visuals.measure_file(
            Path(row.local_path), row.duration_s, settings, ffmpeg=deps.ffmpeg, ffprobe=deps.ffprobe
        )
    except tools.FfmpegError as exc:
        get_logger("gate").warning("gate_file_unreadable", error=str(exc)[:300])
        return None


def _decide(
    row: store.ClipRow,
    file: FileFacts | None,
    visual: VisualFindings | None,
    state: PrecheckState,
    settings: GateSettings,
    now: datetime,
) -> GateDecision:
    """Load the database inputs and call the pure decision."""
    clip = store.clip_facts(row, file, store.music_frames(row.id))
    creator = store.creator_facts(row.creator_id, row.id, now)
    recent = store.recent_clips(row, settings, now)
    return decide(clip, creator, recent, settings, visual, state, now)


def _youtube(deps: GateDeps) -> YouTubeClient:
    if deps.youtube is None:
        deps.youtube = YouTubeClient()
    return deps.youtube


def _look(row: store.ClipRow, deps: GateDeps) -> VisualFindings:
    lane: BudgetKind = "express" if row.profile == EXPRESS else "vod"
    assert row.local_path is not None and row.duration_s is not None
    return visuals.look_at_file(
        Path(row.local_path),
        row.duration_s,
        lane=lane,
        clip_id=row.id,
        client=deps.ai_client,
        ffmpeg=deps.ffmpeg,
    )


def _finish(
    row: store.ClipRow,
    file: FileFacts | None,
    visual: VisualFindings | None,
    state: PrecheckState | None,
    settings: GateSettings,
    deps: GateDeps,
    *,
    p5_skipped: bool = False,
) -> GateOutcome:
    """Decide under the lock, store, then do what follows the verdict. `state` None means "ask now"."""
    log = get_logger("gate")
    precheck_job: int | None = None
    with store.decision_lock():
        current = store.get_clip(row.id)
        if current is None or current.status != row.status:
            return GateOutcome(row.id, current.status if current else "missing", skipped="status changed")
        now = deps.clock()
        decided_state = state or precheck_state.current_state(_youtube(deps), now)
        decision = _decide(row, file, visual, decided_state, settings, now)
        document = store.result_document(decision, file, visual, now, p5_skipped=p5_skipped)
        # A clip that was moved to the hourly lane before its pre-check keeps that mark and its boost.
        earlier = row.gate_result or {}
        boosted = decision.demoted or earlier.get("demoted") is True
        if boosted and not decision.demoted:
            document["demoted"] = True
            document["express"] = earlier.get("express")
        if not store.store_decision(row, decision, document, now, boosted=boosted):
            return GateOutcome(row.id, row.status, skipped="status changed")
        if decision.verdict is Verdict.HOLD:
            precheck_job = jobs.enqueue(
                "normal", JOB_PRECHECK, {"clip_id": row.id}, dedupe_key=f"{JOB_PRECHECK}:{row.id}"
            )
    log.info(
        "gate_decided",
        verdict=decision.verdict.value,
        profile=decision.profile,
        lane=decision.lane,
        codes=[code.value for code in decision.codes],
        demoted=decision.demoted,
        precheck=decision.precheck.value,
    )
    if decision.verdict is Verdict.REJECT:
        return GateOutcome(row.id, store.STATUS_REJECTED, decision)
    if decision.verdict is Verdict.HOLD:
        return GateOutcome(row.id, store.STATUS_PRECHECK, decision, precheck_job_id=precheck_job)
    storage_path = _upload(row.id, deps)
    spike_run: SpikeRun | None = None
    handoff_error: str | None = None
    if decision.lane == LANE_SPIKE and deps.hand_off:
        spike_run, handoff_error = _hand_off(row.id, deps)
    return GateOutcome(
        row.id,
        store.STATUS_APPROVED,
        decision,
        storage_path=storage_path,
        spike_run=spike_run,
        handoff_error=handoff_error,
    )


def _upload(clip_id: str, deps: GateDeps) -> str | None:
    """Upload the approved clip to Storage. A failure does not undo the approval: posting reads the local
    file. It is logged and written as a `clip.upload_failed` event."""
    if not deps.upload_to_storage:
        return None
    try:
        return pipeline.upload_clip(clip_id, storage=deps.storage)
    except Exception as exc:  # noqa: BLE001 - never lose an approval over the archive copy
        get_logger("gate").warning("gate_upload_failed", error=f"{type(exc).__name__}: {exc}"[:300])
        events.emit(
            "clip.upload_failed",
            level="warn",
            entity_type="clip",
            entity_id=clip_id,
            message=f"{type(exc).__name__}: {exc}"[:300],
            service="gate",
        )
        return None


def _hand_off(clip_id: str, deps: GateDeps) -> tuple[SpikeRun | None, str | None]:
    """Give an approved spike clip to the posting engine. If that fails the clip stays approved and a
    `post.retry` job tries again; the posting engine itself decides about dry run and the emergency stop."""
    try:
        return try_post_spike(clip_id, deps.upload_post()), None
    except Exception as exc:  # noqa: BLE001 - the clip is approved; posting is retried by a job
        error = f"{type(exc).__name__}: {exc}"[:300]
        get_logger("gate").error("gate_handoff_failed", error=error)
        events.emit(
            "gate.handoff_failed",
            level="warn",
            entity_type="clip",
            entity_id=clip_id,
            message=error,
            service="gate",
        )
        jobs.enqueue(
            "express",
            JOB_POST_RETRY,
            {"kind": "spike", "clip_id": clip_id},
            priority=HANDOFF_RETRY_PRIORITY,
            run_after=deps.clock() + timedelta(seconds=RETRY_DELAY_S),
        )
        return None, error


def run_gate(clip_id: str, *, deps: GateDeps | None = None) -> GateOutcome:
    """Judge the clip `clip_id` if it is in `gating`. Safe to call again: a judged clip is left alone.

    Raises when the clip does not exist, when a gate setting is malformed, or when P5 cannot be asked
    (API error, daily budget): the clip then stays in `gating` and the caller retries.
    """
    deps = deps or _deps
    row = store.get_clip(clip_id)
    if row is None:
        raise ValueError(f"clip {clip_id} does not exist")
    with structlog.contextvars.bound_contextvars(clip_id=clip_id, spike_id=row.spike_id):
        if row.status != store.STATUS_GATING:
            get_logger("gate").info("gate_skipped", status=row.status)
            return GateOutcome(clip_id, row.status, skipped=f"status is {row.status}")
        settings = profiles.load()
        file = _measure(row, settings, deps)
        # Would the clip be rejected even if the pictures were fine? Then P5 is not asked.
        now = deps.clock()
        state = precheck_state.current_state(_youtube(deps), now)
        optimistic = _decide(row, file, _NOTHING_SEEN, state, settings, now)
        if optimistic.verdict is Verdict.REJECT:
            return _finish(row, file, _NOTHING_SEEN, None, settings, deps, p5_skipped=True)
        return _finish(row, file, _look(row, deps), None, settings, deps)


def resolve_precheck(clip_id: str, state: PrecheckState, *, deps: GateDeps | None = None) -> GateOutcome:
    """Judge a clip in `precheck_pending` again with the outcome of its pre-check (`passed`, `claimed`,
    `inconclusive`), or with `unavailable` / `disabled` / `quota_used` when the pre-check could not run
    after all. Every rule is applied again at the current time, from the stored measurements."""
    deps = deps or _deps
    row = store.get_clip(clip_id)
    if row is None:
        raise ValueError(f"clip {clip_id} does not exist")
    with structlog.contextvars.bound_contextvars(clip_id=clip_id, spike_id=row.spike_id):
        if row.status != store.STATUS_PRECHECK:
            return GateOutcome(clip_id, row.status, skipped=f"status is {row.status}")
        if state is PrecheckState.AVAILABLE:
            raise ValueError("resolve_precheck needs the outcome of the pre-check, not 'available'")
        settings = profiles.load()
        inputs = (row.gate_result or {}).get("inputs")
        stored = inputs if isinstance(inputs, dict) else {}
        file = store.file_from_json(stored.get("file")) or _measure(row, settings, deps)
        visual = visuals.findings_from_json(stored.get("visual"))
        return _finish(row, file, visual, state, settings, deps)


# ── Factory hook and job handlers ────────────────────────────


def _queue_gate(clip_id: str, queue: jobs.Queue, run_after: datetime) -> None:
    jobs.enqueue(
        queue, JOB_GATE, {"clip_id": clip_id}, run_after=run_after, dedupe_key=f"{JOB_GATE}:{clip_id}"
    )


def after_render(clip_id: str, job: jobs.Job) -> None:
    """The step after a successful render (`pipeline.set_after_render`): gate the clip now.

    The render job must not be run again because the gate failed, so nothing is raised: a `clip.gate` job
    takes over (after the budget reset when the daily AI budget stopped P5).
    """
    queue: jobs.Queue = "express" if job.queue == "express" else "normal"
    log = get_logger("gate")
    try:
        run_gate(clip_id)
    except BudgetExceededError:
        log.warning("gate_deferred", clip_id=clip_id, reason="ai.daily_budget_usd is used up")
        _queue_gate(clip_id, queue, pipeline.next_budget_reset(_deps.clock()))
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.error("gate_failed", clip_id=clip_id, error=f"{type(exc).__name__}: {exc}"[:300])
        _queue_gate(clip_id, queue, _deps.clock() + timedelta(seconds=RETRY_DELAY_S))


def handle_gate(job: jobs.Job) -> None:
    """Job `clip.gate` (`{"clip_id": ...}`): run the gate. After the last failed attempt the clip is
    rejected with `gate_error` and a warning is sent: a clip nobody could judge is never posted."""
    clip_id = str(job.payload["clip_id"])
    try:
        run_gate(clip_id)
    except BudgetExceededError:
        jobs.enqueue(
            "express" if job.queue == "express" else "normal",
            JOB_GATE,
            {"clip_id": clip_id},
            run_after=pipeline.next_budget_reset(_deps.clock()),
        )
    except Exception as exc:
        if job.attempts < job.max_attempts:
            raise
        error = f"{type(exc).__name__}: {exc}"
        if store.reject_for_error(clip_id, error):
            notify.send("warn", "Quality gate failed", f"clip {clip_id}: {error[:300]}")


def run_pending(older_than_s: float = 0.0, *, deps: GateDeps | None = None) -> list[GateOutcome]:
    """Run the gate for every clip still in `gating` (recovery after a crash or a lost job)."""
    deps = deps or _deps
    return [run_gate(clip_id, deps=deps) for clip_id in store.gating_clip_ids(older_than_s)]


def install(job_types: tuple[str, ...] = (pipeline.JOB_CLIP_RENDER, pipeline.JOB_EXPRESS_RENDER)) -> None:
    """Attach the gate to the factory and register the gate and pre-check job handlers (safe to call more
    than once). The worker process calls this at start, next to `pipeline.register_handlers()`."""
    from sixstars.precheck import youtube_precheck
    from sixstars.services import worker

    for job_type in job_types:
        pipeline.set_after_render(job_type, after_render)
    handlers: dict[str, jobs.Handler] = {
        JOB_GATE: handle_gate,
        youtube_precheck.JOB_PRECHECK: youtube_precheck.handle_precheck,
        youtube_precheck.JOB_PRECHECK_RESULT: youtube_precheck.handle_precheck_result,
    }
    for job_type, handler in handlers.items():
        if worker.HANDLERS.get(job_type) is not handler:
            worker.register(job_type, handler)


__all__ = [
    "GateDeps",
    "GateOutcome",
    "after_render",
    "configure",
    "current_deps",
    "handle_gate",
    "install",
    "resolve_precheck",
    "run_gate",
    "run_pending",
]

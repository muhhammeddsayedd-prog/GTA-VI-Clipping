"""YouTube claim pre-check (docs/10 section 3): a risky hourly clip is uploaded as a private video to the
owner's channel, its status is read after `precheck.wait_s`, the private copy is deleted and the clip is
approved or rejected.

Two job handlers:

- `clip.precheck` (`{"clip_id"}`): upload the file privately, remember the video id, queue the result job.
- `clip.precheck_result` (`{"clip_id", "check"}`): read the status, delete the copy, then let the gate
  judge the clip again with the outcome (`gate.resolve_precheck`).

The pre-check needs the owner's OAuth refresh token. Without one (decision D103) no clip ever reaches
these handlers: the gate sees the state `unavailable` and applies the stricter music rule instead. If the
pre-check stops being possible between the gate and the upload (token removed, switched off, daily quota
used, upload keeps failing), the clip is judged again with that state, which applies the same stricter
rule. Nothing waits forever and nothing is approved because a check could not run.

Limits (docs/10): the API shows rejections, blocks and removals, not every Content ID claim.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

import structlog
from psycopg.types.json import Jsonb

from sixstars import db, events, jobs
from sixstars.clients.youtube import YouTubeClient, YouTubeVideoStatus
from sixstars.gate import gate, store
from sixstars.gate.models import PrecheckState
from sixstars.logging import get_logger
from sixstars.precheck import state as precheck_state

JOB_PRECHECK = "clip.precheck"
JOB_PRECHECK_RESULT = "clip.precheck_result"
UPLOAD_STATUS_REJECTED = "rejected"
UPLOAD_STATUS_PROCESSED = "processed"
UPLOAD_STATUS_UPLOADED = "uploaded"  # received, still processing
# `status.rejectionReason` values that mean a rights holder objected.
CLAIM_REJECTION_REASONS = ("claim", "copyright", "trademark", "legal")


class Outcome(StrEnum):
    PASSED = "passed"
    CLAIMED = "claimed"
    PENDING = "pending"  # YouTube is still processing: read again later
    INCONCLUSIVE = "inconclusive"


_STATE = {
    Outcome.PASSED: PrecheckState.PASSED,
    Outcome.CLAIMED: PrecheckState.CLAIMED,
    Outcome.INCONCLUSIVE: PrecheckState.INCONCLUSIVE,
}


def evaluate_status(status: YouTubeVideoStatus | None) -> tuple[Outcome, str]:
    """Pure: what the status of the private copy means.

    Claimed: the video is gone, was rejected for a rights reason (or without a reason), or is blocked or
    limited in any country. Passed: processed with no restriction. Pending: still processing. Anything
    else (a failed upload, a rejection for length or as a duplicate) is inconclusive.
    """
    if status is None:
        return Outcome.CLAIMED, "the private copy was removed"
    if status.region_blocked:
        return Outcome.CLAIMED, "blocked in " + ", ".join(status.region_blocked[:10])
    if status.region_allowed is not None:
        return Outcome.CLAIMED, "viewable only in " + (", ".join(status.region_allowed[:10]) or "no country")
    if status.upload_status == UPLOAD_STATUS_REJECTED:
        reason = status.rejection_reason
        if reason is None or reason in CLAIM_REJECTION_REASONS:
            return Outcome.CLAIMED, f"upload rejected: {reason or 'no reason given'}"
        return Outcome.INCONCLUSIVE, f"upload rejected: {reason}"
    if status.upload_status == UPLOAD_STATUS_PROCESSED:
        return Outcome.PASSED, "processed without a restriction"
    if status.upload_status == UPLOAD_STATUS_UPLOADED:
        return Outcome.PENDING, "still processing"
    return Outcome.INCONCLUSIVE, f"upload status {status.upload_status or 'unknown'}"


def _client() -> YouTubeClient:
    deps = gate.current_deps()
    if deps.youtube is None:
        deps.youtube = YouTubeClient()
    return deps.youtube


def _now() -> datetime:
    return gate.current_deps().clock()


def _number(key: str) -> int:
    value = precheck_state.setting(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"setting {key} must be a non-negative integer")
    return value


def _queue_result(clip_id: str, check: int, run_after: datetime) -> None:
    jobs.enqueue(
        "normal",
        JOB_PRECHECK_RESULT,
        {"clip_id": clip_id, "check": check},
        run_after=run_after,
        dedupe_key=f"{JOB_PRECHECK_RESULT}:{clip_id}:{check}",
    )


def _save_result(clip_id: str, result: dict[str, object]) -> None:
    db.execute(
        "update clips set precheck_result = coalesce(precheck_result, '{}'::jsonb) || %s where id = %s",
        (Jsonb(result), clip_id),
    )


def handle_precheck(job: jobs.Job) -> None:
    """Job `clip.precheck`: upload the clip as a private video and queue the status check."""
    clip_id = str(job.payload["clip_id"])
    log = get_logger("precheck")
    with structlog.contextvars.bound_contextvars(clip_id=clip_id, job_id=job.id):
        row = store.get_clip(clip_id)
        if row is None or row.status != store.STATUS_PRECHECK:
            log.info("precheck_skipped", status=row.status if row else "missing")
            return
        now = _now()
        wait = timedelta(seconds=_number("precheck.wait_s"))
        if row.precheck_video_id:
            _queue_result(clip_id, 1, now + wait)
            return
        client = _client()
        state = precheck_state.current_state(client, now)
        if state is not PrecheckState.AVAILABLE:
            log.warning("precheck_not_possible", state=state.value)
            gate.resolve_precheck(clip_id, state)
            return
        path = Path(row.local_path) if row.local_path else None
        if path is None or not path.is_file():
            gate.resolve_precheck(clip_id, PrecheckState.INCONCLUSIVE)
            return
        try:
            video_id = client.upload_private(
                path, f"precheck {clip_id}", "6 Stars Clips pre-check copy. Deleted automatically."
            )
        except Exception as exc:
            if job.attempts < job.max_attempts:
                raise
            log.error("precheck_upload_failed", error=f"{type(exc).__name__}: {exc}"[:300])
            gate.resolve_precheck(clip_id, PrecheckState.UNAVAILABLE)
            return
        with db.transaction():
            db.execute("update clips set precheck_video_id = %s where id = %s", (video_id, clip_id))
            _save_result(clip_id, {"video_id": video_id, "uploaded_at": now.isoformat()})
            events.emit(
                precheck_state.EVENT_UPLOADED,
                entity_type="clip",
                entity_id=clip_id,
                data={"video_id": video_id},
                service="precheck",
            )
            _queue_result(clip_id, 1, now + wait)
        log.info("precheck_uploaded", video_id=video_id)


def _delete(client: YouTubeClient, clip_id: str, video_id: str) -> bool:
    try:
        client.delete_video(video_id)
    except Exception as exc:  # noqa: BLE001 - the verdict must not depend on the clean-up
        events.emit(
            "clip.precheck_delete_failed",
            level="warn",
            entity_type="clip",
            entity_id=clip_id,
            message=f"private video {video_id} was not deleted: {type(exc).__name__}",
            data={"video_id": video_id},
            service="precheck",
        )
        return False
    return True


def handle_precheck_result(job: jobs.Job) -> None:
    """Job `clip.precheck_result`: read the status of the private copy, delete it and settle the clip."""
    clip_id = str(job.payload["clip_id"])
    check = int(job.payload.get("check", 1))
    log = get_logger("precheck")
    with structlog.contextvars.bound_contextvars(clip_id=clip_id, job_id=job.id):
        row = store.get_clip(clip_id)
        if row is None:
            return
        client = _client()
        video_id = row.precheck_video_id
        if row.status != store.STATUS_PRECHECK:
            if video_id:
                _delete(client, clip_id, video_id)  # settled some other way: do not leave the copy behind
            return
        if not video_id:
            gate.resolve_precheck(clip_id, PrecheckState.UNAVAILABLE)
            return
        try:
            status = client.get_status(video_id)
        except Exception as exc:
            if job.attempts < job.max_attempts:
                raise
            log.error("precheck_status_failed", error=f"{type(exc).__name__}: {exc}"[:300])
            outcome, detail = Outcome.INCONCLUSIVE, f"status could not be read: {type(exc).__name__}"
            status = None
        else:
            outcome, detail = evaluate_status(status)
        now = _now()
        if outcome is Outcome.PENDING:
            if check < _number("precheck.max_status_checks"):
                _queue_result(clip_id, check + 1, now + timedelta(seconds=_number("precheck.wait_s")))
                return
            outcome, detail = Outcome.INCONCLUSIVE, f"still processing after {check} checks"
        deleted = _delete(client, clip_id, video_id) if status is not None else False
        _save_result(
            clip_id,
            {
                "outcome": outcome.value,
                "detail": detail,
                "upload_status": status.upload_status if status else None,
                "rejection_reason": status.rejection_reason if status else None,
                "region_blocked": list(status.region_blocked) if status else [],
                "checked_at": now.isoformat(),
                "deleted": deleted,
            },
        )
        if outcome is Outcome.CLAIMED:
            db.execute("update creators set claims_count = claims_count + 1 where id = %s", (row.creator_id,))
        events.emit(
            "clip.precheck_result",
            level="warn" if outcome is Outcome.CLAIMED else "info",
            entity_type="clip",
            entity_id=clip_id,
            message=detail,
            data={"outcome": outcome.value, "video_id": video_id, "deleted": deleted},
            service="precheck",
        )
        log.info("precheck_result", outcome=outcome.value, detail=detail)
        gate.resolve_precheck(clip_id, _STATE[outcome])

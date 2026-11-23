"""Hourly lane (docs/11 §6): one clip per hour per account.

`schedule_hourly_slot()` is what the hourly trigger calls (n8n at minute 0, or the scheduler failover). It
records the heartbeat `trigger:hourly` and queues one `post.hourly` job that becomes runnable at the slot's
jittered time. The job calls `run_hourly_slot`, which decides the slot under the planning lock, inserts the
`posts` rows in the same transaction and then publishes the real ones.

Under `control.emergency_stop` nothing is queued, decided or recorded. In dry mode (`launch.dry_run`, or
before `launch.launch_at`) the decisions are stored as `posts` rows with `dry_run = true, status = skipped`
and nothing is sent.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sixstars import db, jobs
from sixstars.clients.upload_post import UploadPostClient
from sixstars.logging import get_logger
from sixstars.posting.captions import build_upload_request, idempotency_key
from sixstars.posting.gate import Mode, posting_mode
from sixstars.posting.models import Account, Action, Clip, Decision, Reason
from sixstars.posting.params import PostingParams
from sixstars.posting.publisher import RECOVERY_DELAY_S, DeliverHooks, Publisher, PublishResult
from sixstars.posting.selection import SlotPlan, empty_slots_alert, plan_hourly_slot
from sixstars.posting.slots import SLOT_LENGTH, check_slot, fire_at, slot_start
from sixstars.posting.store import DbStore, clips_only, dry_run_key, utcnow

HOURLY_JOB_TYPE = "post.hourly"
HOURLY_QUEUE: jobs.Queue = (
    "express"  # posting jobs are short; the normal queue can be busy with a long render
)
HOURLY_PRIORITY = 50  # below the spike lane's 90
HEARTBEAT_NAME = "trigger:hourly"
# Accounts that are simply not part of a lane are not reported every hour.
QUIET_SKIPS = frozenset({Reason.ACCOUNT_DISABLED, Reason.LANE_NOT_ALLOWED})


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    """Answer of `schedule_hourly_slot`. `status` is `scheduled`, `already_scheduled` or `stopped`."""

    status: str
    slot_at: datetime
    fire_at: datetime | None = None
    job_id: int | None = None


@dataclass(frozen=True, slots=True)
class SlotRun:
    """What one run of a slot did. `plan` is None when the emergency stop was on."""

    slot_at: datetime
    mode: Mode
    plan: SlotPlan | None = None
    post_ids: tuple[str, ...] = ()  # real `pending` rows created by this run
    dry_run_ids: tuple[str, ...] = ()  # dry-run rows created by this run
    results: tuple[PublishResult, ...] = ()


def schedule_hourly_slot(now: datetime | None = None, store: DbStore | None = None) -> ScheduleResult:
    """Called by the hourly trigger. Returns at once; the post happens in a `post.hourly` job at the
    slot's jittered time. Calling it twice for one slot queues one job."""
    store = store or DbStore()
    now = now or utcnow()
    slot = slot_start(now)
    db.heartbeat(HEARTBEAT_NAME, {"slot_at": slot.isoformat()})
    log = get_logger("posting")
    params = store.load_params()
    if params.emergency_stop:
        log.warning("hourly_slot_not_scheduled", reason="emergency_stop", slot_at=slot.isoformat())
        return ScheduleResult("stopped", slot)
    at = fire_at(slot, params.jitter_max_s)
    job_id = jobs.enqueue(
        HOURLY_QUEUE,
        HOURLY_JOB_TYPE,
        {"slot_at": slot.isoformat()},
        priority=HOURLY_PRIORITY,
        run_after=at,
        dedupe_key=f"{HOURLY_JOB_TYPE}:{slot.isoformat()}",
    )
    log.info("hourly_slot_scheduled", slot_at=slot.isoformat(), fire_at=at.isoformat(), job_id=job_id)
    return ScheduleResult("scheduled" if job_id is not None else "already_scheduled", slot, at, job_id)


def plan_slot(
    store: DbStore, slot_at: datetime, now: datetime, params: PostingParams
) -> tuple[SlotPlan, dict[str, Clip], dict[str, Account]]:
    """Reads the accounts, the queue and the history and decides the slot. Writes nothing."""
    accounts = store.load_accounts()
    posted_since = now - timedelta(hours=params.max_clip_age_h) if params.requeues_posted else None
    queue = clips_only(store.load_hourly_queue(posted_since))
    spikes = clips_only(store.load_spike_clips())
    history = store.load_history(now, [clip.id for clip in (*queue, *spikes)])
    plan = plan_hourly_slot(accounts, history, queue, slot_at, now, params, spike_clips=spikes)
    return plan, {clip.id: clip for clip in queue}, {account.id: account for account in accounts}


def preview_slot(
    slot_at: datetime | None = None, now: datetime | None = None, store: DbStore | None = None
) -> tuple[SlotPlan, PostingParams]:
    """What the slot (default: the current one) would do right now. Read-only: no lock, no rows, no jobs."""
    store = store or DbStore()
    now = now or utcnow()
    params = store.load_params()
    plan, _, _ = plan_slot(store, check_slot(slot_at) if slot_at else slot_start(now), now, params)
    return plan, params


def would_send(clip: Clip, account: Account, params: PostingParams) -> Mapping[str, object]:
    """The upload fields of a dry run, for the event log."""
    try:
        return dict(build_upload_request(clip, account, params, preview=True).fields)
    except ValueError as exc:
        return {"error": str(exc)}


def _decision_data(decision: Decision) -> dict[str, object]:
    return {
        "account_id": decision.account_id,
        "lane": decision.lane.value,
        "code": decision.code.value,
        "action": decision.action.value,
        "clip_id": decision.clip_id,
        "slot_at": decision.slot_at.isoformat() if decision.slot_at else None,
    }


def record_decision(
    store: DbStore,
    decision: Decision,
    clip: Clip,
    account: Account,
    now: datetime,
    params: PostingParams,
) -> tuple[str | None, bool]:
    """Insert the `posts` row of a passing decision (inside the planning lock). Returns the new row's id
    and whether it is a real post; `(None, ...)` when the row already existed."""
    real = decision.may_send
    key = idempotency_key(clip.id, account.id) if real else dry_run_key(clip.id, account.id, decision.slot_at)
    post_id = store.insert_post(
        clip.id, account, decision.lane, decision.slot_at, now, dry_run=not real, idempotency_key=key
    )
    if post_id is None:
        return None, real
    data = _decision_data(decision)
    if real:
        store.schedule_publish_retry(post_id, clip.id, decision.lane, _recovery_time(now))
        store.emit("post.planned", entity_type="post", entity_id=post_id, message=decision.reason, data=data)
    else:
        data["would_send"] = would_send(clip, account, params)
        store.emit("post.dry_run", entity_type="post", entity_id=post_id, message=decision.reason, data=data)
    return post_id, real


def _recovery_time(now: datetime) -> datetime:
    return now + timedelta(seconds=RECOVERY_DELAY_S)


def run_hourly_slot(
    slot_at: datetime,
    client: UploadPostClient,
    *,
    store: DbStore | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> SlotRun:
    """Decide the slot `slot_at`, record the decisions and publish the allowed posts.

    Safe to call twice for one slot: the second call finds the rows of the first (`slot_already_filled`).
    """
    store = store or DbStore()
    slot = check_slot(slot_at)
    log = get_logger("posting")
    post_ids: list[str] = []
    dry_ids: list[str] = []
    alert = False
    with store.planning_lock():
        now = clock()
        params = store.load_params()
        mode, _ = posting_mode(now, params)
        if mode is Mode.STOPPED:
            log.warning("hourly_slot_stopped", reason="emergency_stop", slot_at=slot.isoformat())
            return SlotRun(slot, mode)
        plan, clips, accounts = plan_slot(store, slot, now, params)
        for decision in plan.decisions:
            if decision.would_post and decision.clip_id is not None:
                post_id, real = record_decision(
                    store, decision, clips[decision.clip_id], accounts[decision.account_id], now, params
                )
                if post_id is not None:
                    (post_ids if real else dry_ids).append(post_id)
            elif decision.code not in QUIET_SKIPS:
                store.emit(
                    "posting.slot_skipped",
                    entity_type="account",
                    entity_id=decision.account_id,
                    message=decision.reason,
                    data=_decision_data(decision),
                )
        if plan.slot_empty:
            alert = empty_slots_alert([store.slot_was_empty(slot - SLOT_LENGTH), True])
            store.emit(
                "posting.slot_empty",
                "warn",
                entity_type="slot",
                entity_id=slot.isoformat(),
                message="no clip could be posted to any account in this hourly slot",
            )
    log.info(
        "hourly_slot_planned",
        slot_at=slot.isoformat(),
        mode=mode.value,
        posts=len(post_ids),
        dry_runs=len(dry_ids),
        codes={d.account_id: d.code.value for d in plan.decisions if d.action is not Action.ALLOW},
    )
    if alert:
        store.notify(
            "warn",
            "Hourly queue empty",
            f"No clip could be posted in the last two hourly slots (latest {slot.isoformat()}).",
        )
    results = Publisher(client, store, clock, covers=DeliverHooks()).publish_all(post_ids)
    return SlotRun(slot, mode, plan, tuple(post_ids), tuple(dry_ids), results)

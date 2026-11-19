"""Spike lane (docs/11 §7): S-tier live spike clips post at once, within the caps.

`try_post_spike(clip_id, client)` is what the express worker calls when a spike clip passed the gate. It
decides every approved spike-lane clip under the planning lock, best rank first (R5), so two spikes that
are ready together are posted in rank order whichever call comes first. Per account the answer is: publish
now, wait for the minimum gap (a `post.retry` job at the end of the gap, R4) or give up on that account. A
clip that cannot post anywhere moves to the hourly lane with a boosted rank (R7).

Under `control.emergency_stop` nothing is decided, recorded or demoted. In dry mode the decisions are stored
as dry-run `posts` rows and nothing is sent.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sixstars.clients.upload_post import UploadPostClient
from sixstars.logging import get_logger
from sixstars.posting.gate import Mode, posting_mode
from sixstars.posting.hourly import QUIET_SKIPS, record_decision
from sixstars.posting.models import Action, Decision
from sixstars.posting.precedence import plan_spikes
from sixstars.posting.publisher import Publisher, PublishResult
from sixstars.posting.store import DbStore, utcnow


@dataclass(frozen=True, slots=True)
class SpikeRun:
    """What one call of `try_post_spike` did for the clip it was called for."""

    clip_id: str
    mode: Mode
    known: bool = True  # False when the clip does not exist
    decisions: tuple[Decision, ...] = ()
    post_ids: tuple[str, ...] = ()  # real `pending` rows created by this call (all spike clips)
    dry_run_ids: tuple[str, ...] = ()
    results: tuple[PublishResult, ...] = ()
    deferred_until: datetime | None = None
    demoted: bool = False
    posted: bool = False  # the clip was marked `posted`


def _data(decision: Decision) -> dict[str, object]:
    return {
        "account_id": decision.account_id,
        "code": decision.code.value,
        "action": decision.action.value,
        "clip_id": decision.clip_id,
    }


def try_post_spike(
    clip_id: str,
    client: UploadPostClient,
    *,
    store: DbStore | None = None,
    clock: Callable[[], datetime] = utcnow,
    job_id: int | None = None,
) -> SpikeRun:
    """Try to post the spike clip `clip_id` now on every spike-lane account. Safe to call again.

    `job_id` is the `post.retry` job this call runs in, if any (so it does not wait for itself).
    """
    store = store or DbStore()
    log = get_logger("posting")
    post_ids: list[str] = []
    dry_ids: list[str] = []
    planned_clips: list[str] = []
    decisions: tuple[Decision, ...] = ()
    deferred_until: datetime | None = None
    demoted = False
    with store.planning_lock():
        now = clock()
        params = store.load_params()
        mode, _ = posting_mode(now, params)
        if mode is Mode.STOPPED:
            log.warning("spike_stopped", reason="emergency_stop", clip_id=clip_id)
            return SpikeRun(clip_id, mode)
        loaded = {item.clip.id: item.clip for item in store.load_spike_clips()}
        if clip_id not in loaded:
            asked = store.get_clip(clip_id)
            if asked is None:
                return SpikeRun(clip_id, mode, known=False)
            loaded[clip_id] = asked.clip
        accounts = {account.id: account for account in store.load_accounts()}
        history = store.load_history(now, list(loaded))
        for plan in plan_spikes(loaded.values(), list(accounts.values()), history, now, params):
            clip = loaded[plan.clip_id]
            mine = plan.clip_id == clip_id
            waits: list[datetime] = []
            for decision in plan.decisions:
                if decision.would_post:
                    post_id, real = record_decision(
                        store, decision, clip, accounts[decision.account_id], now, params
                    )
                    if post_id is not None:
                        (post_ids if real else dry_ids).append(post_id)
                elif decision.action is Action.DEFER and decision.defer_until is not None:
                    waits.append(decision.defer_until)
                elif mine and decision.code not in QUIET_SKIPS:
                    store.emit(
                        "posting.spike_skipped",
                        entity_type="clip",
                        entity_id=clip.id,
                        message=decision.reason,
                        data=_data(decision),
                    )
            if waits:
                store.schedule_spike_retry(clip.id, min(waits), after_job=job_id)
                store.emit(
                    "posting.spike_deferred",
                    entity_type="clip",
                    entity_id=clip.id,
                    message=f"waiting until {min(waits).isoformat()} (holding window or minimum gap)",
                )
            was_demoted = plan.demote_to_hourly and store.demote_clip(
                clip.id, plan.demoted_rank_score, dry=mode is Mode.DRY
            )
            if was_demoted:
                store.emit(
                    "clip.demoted",
                    entity_type="clip",
                    entity_id=clip.id,
                    message="spike clip moved to the hourly lane (R7)",
                    data={"rank_score": plan.demoted_rank_score},
                )
            planned_clips.append(clip.id)
            if mine:
                decisions = plan.decisions
                deferred_until = min(waits) if waits else None
                demoted = was_demoted
    log.info(
        "spike_planned",
        clip_id=clip_id,
        mode=mode.value,
        posts=len(post_ids),
        dry_runs=len(dry_ids),
        codes={d.account_id: d.code.value for d in decisions},
        demoted=demoted,
    )
    publisher = Publisher(client, store, clock)
    results = publisher.publish_all(post_ids)
    posted = False
    for planned in planned_clips:
        settled = store.settle_clip(planned, clock(), exclude_job_id=job_id)
        posted = posted or (settled and planned == clip_id)
    if not posted:
        after = store.get_clip(clip_id)
        posted = after is not None and after.clip.status == "posted"
    return SpikeRun(
        clip_id,
        mode,
        decisions=decisions,
        post_ids=tuple(post_ids),
        dry_run_ids=tuple(dry_ids),
        results=results,
        deferred_until=deferred_until,
        demoted=demoted,
        posted=posted,
    )

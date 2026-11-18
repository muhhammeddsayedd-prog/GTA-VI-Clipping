"""Pure precedence rules between the spike lane (priority 1) and the hourly lane (priority 2): docs/11 §4
R1-R11 and decision D41. Every function returns `Decision` objects; nothing is sent, stored or timed here.

`history` is always the recent `posts` rows of every account (at least the last 24 h plus the last hourly
post of each account); the functions pick out what they need.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sixstars.posting.caps import account_posts, cap_state, counts
from sixstars.posting.gate import check_gate, duplicate_channels
from sixstars.posting.models import (
    CLIP_APPROVED,
    Account,
    Action,
    Clip,
    Decision,
    Lane,
    PostRecord,
    Reason,
    ensure_utc,
)
from sixstars.posting.params import PostingParams
from sixstars.posting.slots import SLOT_LENGTH, check_slot

SPIKE_DEMOTION_BOOST = (
    1.5  # R7: rank boost of a spike clip that moves to the hourly lane (docs/11, no setting)
)


def demoted_rank(rank_score: float) -> float:
    """The rank of a spike clip after R7 moved it to the hourly lane."""
    return rank_score * SPIKE_DEMOTION_BOOST


def clip_block(
    clip: Clip, account: Account, history: Iterable[PostRecord], dry: bool
) -> tuple[Reason, str] | None:
    """Why `clip` may not go to `account` at all, or None.

    A clip is never posted twice to one account, and never to two accounts of the same platform (D42:
    different clips on each TikTok account).
    """
    same_clip = [r for r in history if r.clip_id == clip.id and counts(r, dry)]
    if any(r.account_id == account.id for r in same_clip):
        return Reason.ALREADY_POSTED, f"clip {clip.id} already has a post on {account.id}"
    other = next((r for r in same_clip if r.platform is account.platform), None)
    if other is not None:
        return (
            Reason.POSTED_ON_SAME_PLATFORM,
            f"clip {clip.id} already has a post on {other.account_id}, "
            f"another {account.platform.value} account",
        )
    return None


# R12: the batch is released this long before its freshest clip would turn too old, at the latest, so a
# held clip always keeps time to post.
HOLD_AGE_MARGIN_S = 120


def hold_release_at(clips: Iterable[Clip], params: PostingParams) -> datetime | None:
    """R12 holding window: when the waiting spike clips may post.

    Approved spike-lane clips wait until `posting.spike_hold_s` after the first of them was approved, then
    they are all decided together, best rank first, so a better clip that arrives during the wait takes
    the spike post. The release never comes later than `HOLD_AGE_MARGIN_S` before any of them reaches
    `posting.spike_max_age_s`. None when holding is off or no clip is waiting.
    """
    if params.spike_hold_s <= 0:
        return None
    waiting = [c for c in clips if c.status == CLIP_APPROVED and c.lane is Lane.SPIKE and c.approved_at]
    if not waiting:
        return None
    release = min(ensure_utc(c.approved_at, "approved_at") for c in waiting if c.approved_at is not None)
    release += timedelta(seconds=params.spike_hold_s)
    latest = [
        ensure_utc(c.spike_at, "spike_at") + timedelta(seconds=params.spike_max_age_s - HOLD_AGE_MARGIN_S)
        for c in waiting
        if c.spike_at is not None
    ]
    return min([release, *latest])


def decide_spike(
    clip: Clip,
    account: Account,
    history: Sequence[PostRecord],
    now: datetime,
    params: PostingParams,
    *,
    hold_until: datetime | None = None,
) -> Decision:
    """May the spike lane post `clip` to `account` now? (R1, R4, R6, R7, R8, R11, R12.)

    `SKIP` means the spike lane gives up on this account (the clip is "demoted" there); `DEFER` means the
    clip waits until `defer_until`: for the holding window (`hold_until`, R12) or because the minimum gap
    is not met yet and waiting is within the allowed wait (R4).
    """
    now = ensure_utc(now, "now")
    gate = check_gate(account, Lane.SPIKE, now, params)
    gate = replace(gate, clip_id=clip.id)
    if not gate.would_post:
        return gate
    dry = gate.dry_run

    def skip(code: Reason, reason: str) -> Decision:
        return Decision(Action.SKIP, code, reason, account.id, Lane.SPIKE, dry_run=dry, clip_id=clip.id)

    if clip.status != CLIP_APPROVED:
        return skip(Reason.CLIP_NOT_APPROVED, f"clip {clip.id} has status {clip.status!r}, not approved")
    if clip.lane is not Lane.SPIKE:
        return skip(Reason.CLIP_WRONG_LANE, f"clip {clip.id} is in the {clip.lane.value} lane")
    blocked = clip_block(clip, account, history, dry)
    if blocked is not None:
        return skip(*blocked)
    if clip.spike_at is None:
        return skip(Reason.SPIKE_TIME_MISSING, f"clip {clip.id} has no spike time, so its age is unknown")
    too_old_at = clip.spike_at + timedelta(seconds=params.spike_max_age_s)
    if now > too_old_at:
        age_s = int((now - clip.spike_at).total_seconds())
        return skip(
            Reason.SPIKE_TOO_OLD,
            f"spike is {age_s} s old, over posting.spike_max_age_s {params.spike_max_age_s}",
        )
    state = cap_state(account, history, now, params, dry=dry)
    if state.at_cap:
        return skip(Reason.AT_CAP, f"{account.id} has used {state.used_24h} of {state.cap} posts in 24 h")
    if not state.spike_rate_ok:
        return skip(
            Reason.SPIKE_HOURLY_LIMIT,
            f"{account.id} had {state.spikes_1h} spike posts in the last hour, "
            f"posting.spike_max_per_hour is {params.spike_max_per_hour}",
        )
    if hold_until is not None and now < hold_until:
        return Decision(
            Action.DEFER,
            Reason.SPIKE_HOLD,
            f"holding spike clips until {hold_until.isoformat()} so the best of them posts first",
            account.id,
            Lane.SPIKE,
            dry_run=dry,
            clip_id=clip.id,
            defer_until=hold_until,
        )
    if not state.gap_ok and state.gap_ends_at is not None:
        gap_end = state.gap_ends_at
        waiting_since = clip.approved_at if clip.approved_at is not None else clip.spike_at
        deadline = waiting_since + timedelta(seconds=params.spike_max_wait_s)
        if gap_end > deadline:
            return skip(
                Reason.SPIKE_WAIT_EXCEEDED,
                f"the {state.gap_s} s gap on {account.id} ends at {gap_end.isoformat()}, after the "
                f"posting.spike_max_wait_s deadline {deadline.isoformat()}",
            )
        if gap_end > too_old_at:
            return skip(
                Reason.SPIKE_TOO_OLD,
                f"the {state.gap_s} s gap on {account.id} ends at {gap_end.isoformat()}, when the spike "
                f"is over posting.spike_max_age_s {params.spike_max_age_s}",
            )
        return Decision(
            Action.DEFER,
            Reason.MIN_GAP_WAIT,
            f"last post on {account.id} was less than {state.gap_s} s ago; retry at {gap_end.isoformat()}",
            account.id,
            Lane.SPIKE,
            dry_run=dry,
            clip_id=clip.id,
            defer_until=gap_end,
        )
    return gate


def has_pending_spike(
    account: Account,
    spike_clips: Iterable[Clip],
    history: Sequence[PostRecord],
    now: datetime,
    params: PostingParams,
) -> bool:
    """True when a spike clip can still post to `account` now or after a short wait (R2: spike first)."""
    return any(decide_spike(c, account, history, now, params).action is not Action.SKIP for c in spike_clips)


def decide_hourly_account(
    account: Account,
    history: Sequence[PostRecord],
    slot_at: datetime,
    now: datetime,
    params: PostingParams,
    *,
    spike_pending: bool = False,
) -> Decision:
    """May `account` take a post in the hourly slot `slot_at`? (R2, R3, R8, R11, reserve, gap, hours.)

    This is the account half of the hourly decision; which clip it gets is decided by
    `sixstars.posting.selection`. `spike_pending` is the answer of `has_pending_spike` for this account.
    The hourly lane never waits for the gap: a slot that cannot post is skipped.
    """
    now = ensure_utc(now, "now")
    slot = check_slot(slot_at)
    gate = replace(check_gate(account, Lane.HOURLY, now, params), slot_at=slot)
    if not gate.would_post:
        return gate
    dry = gate.dry_run

    def skip(code: Reason, reason: str) -> Decision:
        return Decision(Action.SKIP, code, reason, account.id, Lane.HOURLY, dry_run=dry, slot_at=slot)

    if now < slot:
        return Decision(
            Action.DEFER,
            Reason.SLOT_NOT_DUE,
            f"slot {slot.isoformat()} has not started",
            account.id,
            Lane.HOURLY,
            dry_run=dry,
            defer_until=slot,
            slot_at=slot,
        )
    if now >= slot + SLOT_LENGTH:
        return skip(Reason.SLOT_EXPIRED, f"slot {slot.isoformat()} is over; the next slot has started")
    if account.hourly_hours_utc is not None and slot.hour not in account.hourly_hours_utc:
        return skip(
            Reason.OUTSIDE_HOURLY_HOURS,
            f"{account.id} does not post hourly at {slot.hour:02d}:00 UTC "
            f"(hours {sorted(account.hourly_hours_utc)})",
        )
    posts = account_posts(history, account.id, dry)
    if any(p.lane is Lane.HOURLY and p.slot_at == slot for p in posts):
        return skip(
            Reason.SLOT_ALREADY_FILLED, f"{account.id} already has a post for slot {slot.isoformat()}"
        )
    if spike_pending:
        return skip(Reason.SPIKE_PENDING, f"a spike clip is due on {account.id}; spike posts first")
    window_start = slot - timedelta(seconds=params.skip_hourly_after_spike_s)
    recent_spike = max((p.at for p in posts if p.lane is Lane.SPIKE and p.at > window_start), default=None)
    if recent_spike is not None:
        return skip(
            Reason.SPIKE_RECENT,
            f"a spike posted on {account.id} at {recent_spike.isoformat()}, less than "
            f"posting.skip_hourly_after_spike_s {params.skip_hourly_after_spike_s} before the slot",
        )
    state = cap_state(account, history, now, params, dry=dry)
    if state.at_cap:
        return skip(Reason.AT_CAP, f"{account.id} has used {state.used_24h} of {state.cap} posts in 24 h")
    if not state.hourly_allowed:
        return skip(
            Reason.RESERVED_FOR_SPIKE,
            f"{account.id} has {state.free} free posts and {state.reserve_left} are reserved for spikes",
        )
    if not state.gap_ok and state.gap_ends_at is not None:
        return skip(
            Reason.MIN_GAP,
            f"last post on {account.id} was less than {state.gap_s} s ago "
            f"(gap ends {state.gap_ends_at.isoformat()})",
        )
    return gate


@dataclass(frozen=True, slots=True)
class SpikePlan:
    """The spike lane's answer for one clip across all accounts."""

    clip_id: str
    decisions: tuple[Decision, ...]
    demote_to_hourly: bool  # R7 / docs/11 §7.3: nothing can post now or after a wait
    demoted_rank_score: float  # the rank to store when `demote_to_hourly` is acted on


def planned_post(clip: Clip, account: Account, decision: Decision, now: datetime) -> PostRecord:
    """The `posts` row a passing decision will create, used so later decisions in one plan see it."""
    return PostRecord(
        account_id=account.id,
        platform=account.platform,
        clip_id=clip.id,
        lane=decision.lane,
        status="skipped" if decision.dry_run else "pending",
        created_at=now,
        dry_run=decision.dry_run,
        creator_id=clip.creator_id,
        slot_at=decision.slot_at,
    )


def spike_order(clips: Iterable[Clip]) -> list[Clip]:
    """R5: higher rank first; ties go to the earlier spike, then the lower id."""
    last = datetime.max.replace(tzinfo=UTC)
    return sorted(clips, key=lambda c: (-c.rank_score, c.spike_at or c.approved_at or last, c.id))


def plan_spikes(
    clips: Iterable[Clip],
    accounts: Sequence[Account],
    history: Sequence[PostRecord],
    now: datetime,
    params: PostingParams,
) -> tuple[SpikePlan, ...]:
    """Decides every ready spike clip on every account, best clip first (R1, R5).

    A post planned for one clip counts for the next clip, so the second spike waits for the gap. Accounts
    that share a channel with another enabled account are refused (`DUPLICATE_CHANNEL`).
    """
    now = ensure_utc(now, "now")
    duplicates = duplicate_channels(accounts)
    working = list(history)
    plans: list[SpikePlan] = []
    ordered = spike_order(clips)
    hold_until = hold_release_at(ordered, params)
    for clip in ordered:
        decisions: list[Decision] = []
        for account in sorted(accounts, key=lambda a: a.id):
            decision = decide_spike(clip, account, working, now, params, hold_until=hold_until)
            if account.id in duplicates and decision.code is not Reason.EMERGENCY_STOP:
                decision = Decision(
                    Action.SKIP,
                    Reason.DUPLICATE_CHANNEL,
                    f"{account.id} shares {account.platform.value} profile {account.upload_post_user!r} "
                    "with another enabled account",
                    account.id,
                    Lane.SPIKE,
                    dry_run=decision.dry_run,
                    clip_id=clip.id,
                )
            if decision.would_post:
                working.append(planned_post(clip, account, decision, now))
            decisions.append(decision)
        can_still_post = any(d.action is not Action.SKIP for d in decisions)
        is_spike = clip.status == CLIP_APPROVED and clip.lane is Lane.SPIKE
        plans.append(
            SpikePlan(
                clip_id=clip.id,
                decisions=tuple(decisions),
                demote_to_hourly=is_spike and not can_still_post and not params.emergency_stop,
                demoted_rank_score=demoted_rank(clip.rank_score),
            )
        )
    return tuple(plans)

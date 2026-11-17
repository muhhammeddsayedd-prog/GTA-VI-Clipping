"""Hourly lane, pure part: which clip goes to which account in one slot (docs/11 §6, R9, R10, D18, D42).

Every account that passes the account rules gets the best-ranked clip it can take. Because all accounts read
the same ranked queue, they normally get the same clip (D18); an account differs only when it already has
that clip, when its previous hourly post was by the same creator (R9), or when another account of the same
platform has the clip (D42).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sixstars.posting.caps import account_posts
from sixstars.posting.gate import duplicate_channels
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
from sixstars.posting.precedence import clip_block, decide_hourly_account, has_pending_spike, planned_post
from sixstars.posting.slots import check_slot

EMPTY_SLOTS_BEFORE_ALERT = 2  # R10: alert when this many slots in a row were empty (docs/11, no setting)


def hourly_candidates(clips: Iterable[Clip], now: datetime, params: PostingParams) -> list[Clip]:
    """The hourly queue in posting order: approved hourly clips that are not older than
    `posting.max_clip_age_h`, best rank first (ties: approved earlier, then lower id).

    A clip without an approval time is left out: its age cannot be checked.
    """
    now = ensure_utc(now, "now")
    max_age = timedelta(hours=params.max_clip_age_h)
    usable = [
        c
        for c in clips
        if c.status == CLIP_APPROVED
        and c.lane is Lane.HOURLY
        and c.approved_at is not None
        and now - c.approved_at <= max_age
    ]
    return sorted(usable, key=lambda c: (-c.rank_score, c.approved_at, c.id))


def last_hourly_creator(account: Account, history: Iterable[PostRecord], dry: bool) -> str | None:
    """The creator of the account's most recent hourly post (R9), or None."""
    hourly = [p for p in account_posts(history, account.id, dry) if p.lane is Lane.HOURLY]
    if not hourly:
        return None
    return max(hourly, key=lambda p: p.at).creator_id


def pick_clip(
    account: Account, candidates: Sequence[Clip], history: Sequence[PostRecord], dry: bool
) -> Clip | None:
    """The first clip in `candidates` (already ranked) that `account` may take, or None."""
    previous_creator = last_hourly_creator(account, history, dry)
    for clip in candidates:
        if previous_creator is not None and clip.creator_id == previous_creator:
            continue
        if clip_block(clip, account, history, dry) is None:
            return clip
    return None


@dataclass(frozen=True, slots=True)
class SlotPlan:
    """The hourly lane's answer for one slot: one decision per account."""

    slot_at: datetime
    decisions: tuple[Decision, ...]
    slot_empty: bool  # accounts were free to post but no clip could be given to any of them (R10)
    fallback_used: bool  # at least one account got a clip from the fallback list (compilations)

    @property
    def posts(self) -> tuple[Decision, ...]:
        """The decisions that passed every rule (`ALLOW` or `DRY_RUN`), each with its `clip_id`."""
        return tuple(d for d in self.decisions if d.would_post)


def plan_hourly_slot(
    accounts: Sequence[Account],
    history: Sequence[PostRecord],
    queue: Iterable[Clip],
    slot_at: datetime,
    now: datetime,
    params: PostingParams,
    *,
    spike_clips: Iterable[Clip] = (),
    fallback: Iterable[Clip] = (),
) -> SlotPlan:
    """Decides the hourly slot `slot_at` for every account.

    `queue` is the approved hourly clips, `fallback` the unposted compilations used when the queue has
    nothing for an account (R10), `spike_clips` the approved spike-lane clips (R2: an account with a spike
    due skips the slot). Accounts sharing a channel with another enabled account are refused.
    """
    now = ensure_utc(now, "now")
    slot = check_slot(slot_at)
    spikes = tuple(spike_clips)
    candidates = hourly_candidates(queue, now, params)
    spare = hourly_candidates(fallback, now, params)
    duplicates = duplicate_channels(accounts)
    working = list(history)
    decisions: list[Decision] = []
    starved = False
    fallback_used = False
    for account in sorted(accounts, key=lambda a: a.id):
        pending = has_pending_spike(account, spikes, working, now, params)
        decision = decide_hourly_account(account, working, slot, now, params, spike_pending=pending)
        if account.id in duplicates and decision.code is not Reason.EMERGENCY_STOP:
            decision = replace(
                decision,
                action=Action.SKIP,
                code=Reason.DUPLICATE_CHANNEL,
                reason=f"{account.id} shares {account.platform.value} profile {account.upload_post_user!r} "
                "with another enabled account",
                defer_until=None,
            )
        if not decision.would_post:
            decisions.append(decision)
            continue
        clip = pick_clip(account, candidates, working, decision.dry_run)
        from_fallback = False
        if clip is None:
            clip = pick_clip(account, spare, working, decision.dry_run)
            from_fallback = clip is not None
        if clip is None:
            starved = True
            code = Reason.NO_ELIGIBLE_CLIP if candidates or spare else Reason.QUEUE_EMPTY
            reason = (
                f"none of the {len(candidates) + len(spare)} queued clips can go to {account.id} "
                "(already posted there, same creator as its last hourly post, or on another account "
                "of the platform)"
                if candidates or spare
                else "the hourly queue is empty"
            )
            decisions.append(replace(decision, action=Action.SKIP, code=code, reason=reason))
            continue
        fallback_used = fallback_used or from_fallback
        source = "fallback (queue had nothing for this account)" if from_fallback else "hourly queue"
        chosen = replace(
            decision,
            clip_id=clip.id,
            reason=f"{decision.reason}; clip {clip.id} from the {source}, rank {clip.rank_score:g}",
        )
        working.append(planned_post(clip, account, chosen, now))
        decisions.append(chosen)
    posted = any(d.would_post for d in decisions)
    return SlotPlan(
        slot_at=slot,
        decisions=tuple(decisions),
        slot_empty=starved and not posted,
        fallback_used=fallback_used,
    )


def empty_slots_alert(recent_slots_empty: Sequence[bool]) -> bool:
    """R10: True when the latest `EMPTY_SLOTS_BEFORE_ALERT` slots (oldest first) were all empty."""
    tail = recent_slots_empty[-EMPTY_SLOTS_BEFORE_ALERT:]
    return len(tail) == EMPTY_SLOTS_BEFORE_ALERT and all(tail)

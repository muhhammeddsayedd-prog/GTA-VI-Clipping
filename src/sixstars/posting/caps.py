"""Pure cap maths: the rolling 24 h cap per account, the spike reserve, the minimum gap between posts, the
spike posts per hour and what to do when a platform reports a rate limit. No database, no network, no clock.

Which posts count (see `counts`): every real post that was or may have been sent, that is every row that is
not a dry run and not `skipped` (`pending`, `published`, `failed`, `removed`, `remove_failed`). A failed or
removed post may still have used the platform's quota, so it counts. While the system itself is in dry mode
the dry-run rows count as well, so a dry run behaves like the real thing; they never count against real posts.

Window edges: a post exactly 24 h (or exactly 1 h) old is still inside the window, so a slot frees up only
after the full window has passed. The minimum gap is met when exactly `gap_s` seconds have passed.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sixstars.posting.models import Account, Lane, Platform, PostRecord, ensure_utc
from sixstars.posting.params import PostingParams

DAY = timedelta(hours=24)
HOUR = timedelta(hours=1)

# docs/11 §3, "limit reached" handling. These numbers have no settings key in docs/16.
RATE_LIMIT_PAUSE = timedelta(hours=1)
LEARNED_CAP_STEP = {Platform.YOUTUBE: 2}  # platforms whose cap is lowered when they report a limit
LEARNED_CAP_FLOOR = 10


def counts(record: PostRecord, dry: bool) -> bool:
    """True when `record` uses cap, gap and "already posted" budget. `dry` is the mode of the decision."""
    if record.dry_run:
        return dry
    return record.status != "skipped"


def account_posts(history: Iterable[PostRecord], account_id: str, dry: bool) -> tuple[PostRecord, ...]:
    """The counting posts of one account."""
    return tuple(r for r in history if r.account_id == account_id and counts(r, dry))


def in_window(posts: Iterable[PostRecord], now: datetime, window: timedelta) -> tuple[PostRecord, ...]:
    """Posts at or after `now - window`. Posts stamped in the future (clock skew) stay inside the window."""
    start = now - window
    return tuple(p for p in posts if p.at >= start)


def effective_cap(account: Account, params: PostingParams) -> int:
    """The account's cap, never above the limit the platform enforces (`posting.platform_max_24h`)."""
    return max(0, min(account.cap_24h, params.platform_max_24h[account.platform]))


def effective_gap_s(account: Account, params: PostingParams) -> int:
    """The larger of the account's own `min_gap_s` and `posting.min_gap_s`."""
    return max(account.min_gap_s, params.min_gap_s)


def reserve_left(account: Account, spikes_24h: int, now: datetime, params: PostingParams) -> int:
    """Posts still kept free for spikes: `spike_reserve - spike posts in 24 h`, floor 0.

    When `posting.reserve_release_hour_utc` is set, from that UTC hour until midnight the unused reserve
    beyond `posting.reserve_release_keep` is released to the hourly lane.
    """
    left = max(0, account.spike_reserve - spikes_24h)
    release_hour, keep = params.reserve_release_hour_utc, params.reserve_release_keep
    if release_hour is not None and keep is not None and ensure_utc(now).hour >= release_hour:
        left = min(left, keep)
    return left


@dataclass(frozen=True, slots=True)
class CapState:
    """Everything the lanes need to know about one account's limits at one moment."""

    account_id: str
    cap: int
    used_24h: int
    free: int  # cap - used_24h; negative when the cap was lowered below what is already posted
    spikes_24h: int
    spikes_1h: int
    reserve_left: int
    spike_rate_ok: bool  # spikes_1h < posting.spike_max_per_hour
    last_post_at: datetime | None
    gap_s: int
    gap_ends_at: datetime | None  # last_post_at + gap_s
    gap_ok: bool

    @property
    def at_cap(self) -> bool:
        return self.free <= 0

    @property
    def hourly_allowed(self) -> bool:
        """The hourly lane may not eat the spike reserve."""
        return self.free > 0 and self.free > self.reserve_left

    @property
    def spike_allowed(self) -> bool:
        return self.free > 0 and self.spike_rate_ok


def cap_state(
    account: Account,
    history: Iterable[PostRecord],
    now: datetime,
    params: PostingParams,
    *,
    dry: bool = False,
) -> CapState:
    """Cap, reserve and gap figures for `account` at `now`. `history` may hold posts of other accounts."""
    now = ensure_utc(now, "now")
    posts = account_posts(history, account.id, dry)
    day = in_window(posts, now, DAY)
    spikes_day = [p for p in day if p.lane is Lane.SPIKE]
    spikes_hour = in_window(spikes_day, now, HOUR)
    cap = effective_cap(account, params)
    gap_s = effective_gap_s(account, params)
    last = max((p.at for p in posts), default=None)
    gap_ends = None if last is None else last + timedelta(seconds=gap_s)
    return CapState(
        account_id=account.id,
        cap=cap,
        used_24h=len(day),
        free=cap - len(day),
        spikes_24h=len(spikes_day),
        spikes_1h=len(spikes_hour),
        reserve_left=reserve_left(account, len(spikes_day), now, params),
        spike_rate_ok=len(spikes_hour) < params.spike_max_per_hour,
        last_post_at=last,
        gap_s=gap_s,
        gap_ends_at=gap_ends,
        gap_ok=gap_ends is None or now >= gap_ends,
    )


@dataclass(frozen=True, slots=True)
class RateLimitOutcome:
    """What to write to `platform_accounts` after a platform said "limit reached" (or HTTP 429)."""

    account_id: str
    paused_until: datetime
    new_cap_24h: int
    cap_changed: bool
    reason: str


def on_rate_limit(
    account: Account, history: Iterable[PostRecord], now: datetime, params: PostingParams
) -> RateLimitOutcome:
    """Pause the account and, for platforms with a learned limit, lower its cap.

    Every platform is paused for at least `RATE_LIMIT_PAUSE`. A platform in `LEARNED_CAP_STEP` (YouTube) is
    paused until its oldest post in the window is 24 h old if that is later, and its cap drops by the step,
    but not below `LEARNED_CAP_FLOOR` and never upwards. Only real posts are considered.
    """
    now = ensure_utc(now, "now")
    paused_until = now + RATE_LIMIT_PAUSE
    cap = effective_cap(account, params)
    new_cap = cap
    step = LEARNED_CAP_STEP.get(account.platform)
    if step is not None:
        day = in_window(account_posts(history, account.id, dry=False), now, DAY)
        oldest = min((p.at for p in day), default=None)
        if oldest is not None:
            paused_until = max(paused_until, oldest + DAY)
        new_cap = min(cap, max(LEARNED_CAP_FLOOR, cap - step))
    changed = new_cap != account.cap_24h
    cap_text = f"cap_24h {account.cap_24h} -> {new_cap}" if changed else f"cap_24h stays {new_cap}"
    return RateLimitOutcome(
        account_id=account.id,
        paused_until=paused_until,
        new_cap_24h=new_cap,
        cap_changed=changed,
        reason=f"{account.platform.value} reported a rate limit on {account.id}: paused until "
        f"{paused_until.isoformat()}, {cap_text}",
    )

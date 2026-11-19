"""The zero-views delivery guard (docs/28 "Fix first", L0).

After each stats pass, every enabled account is checked: when the median views of its last `last_posts`
published posts that are at least `min_age_h` old is 0, the account's hourly lane is paused
(`platform_accounts.hourly_paused_until`), those posts are marked `delivery_suspect` and Telegram is told.
Only the owner resumes the lane (`resume_lane`).

What counts as 0 views (owner amendment, 19 Nov 2026):
- a post that has been read (`stats_updated_at` set) and has 0 or no views;
- a TikTok post that still has no platform post id `tiktok_missing_id_h` hours after posting (TikTok
  gives a post its id only once it is public and through moderation).
A post that was never read is unknown, not 0, and is left out of the median.

`evaluate` is pure; `run` reads and writes the database. Behind the `scoring_config` flag
`learning.delivery_guard`.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sixstars import db, events, notify
from sixstars.logging import get_logger
from sixstars.scoring import config as scoring_config

SERVICE = "learning"
FLAG = "learning.delivery_guard"
PAUSED_FOREVER = datetime.fromisoformat("2100-01-01T00:00:00+00:00")  # until the owner resumes


@dataclass(frozen=True, slots=True)
class GuardRules:
    last_posts: int = 5
    min_age_h: float = 6.0
    tiktok_missing_id_h: float = 6.0

    @classmethod
    def from_flag(cls, value: Mapping[str, Any]) -> GuardRules:
        def num(key: str, default: float, low: float) -> float:
            raw = value.get(key, default)
            if isinstance(raw, bool) or not isinstance(raw, int | float) or raw < low:
                return default
            return float(raw)

        base = cls()
        return cls(
            last_posts=int(num("last_posts", base.last_posts, 1)),
            min_age_h=num("min_age_h", base.min_age_h, 0),
            tiktok_missing_id_h=num("tiktok_missing_id_h", base.tiktok_missing_id_h, 0),
        )


@dataclass(frozen=True, slots=True)
class PostView:
    """What the guard needs from one published post."""

    id: str
    platform: str
    published_at: datetime
    views: int | None
    read: bool  # stats_updated_at is set
    platform_post_id: str | None


@dataclass(frozen=True, slots=True)
class Verdict:
    account_id: str
    tripped: bool
    median: float | None
    counted: tuple[str, ...] = ()  # post ids that went into the median
    reasons: tuple[str, ...] = field(default_factory=tuple)


def counted_views(post: PostView, now: datetime, rules: GuardRules) -> int | None:
    """The views a post counts with, or None when it is unknown and left out."""
    age_h = (now - post.published_at).total_seconds() / 3600
    if post.platform == "tiktok" and not post.platform_post_id and age_h >= rules.tiktok_missing_id_h:
        return 0
    if not post.read:
        return None
    return post.views or 0


def evaluate(account_id: str, posts: Sequence[PostView], now: datetime, rules: GuardRules) -> Verdict:
    """Does `account_id` trip the guard? `posts` are its published, non-dry-run posts, any order."""
    old_enough = [p for p in posts if (now - p.published_at).total_seconds() >= rules.min_age_h * 3600]
    old_enough.sort(key=lambda p: p.published_at, reverse=True)
    known = [(p, v) for p in old_enough if (v := counted_views(p, now, rules)) is not None]
    window = known[: rules.last_posts]
    if len(window) < rules.last_posts:
        return Verdict(account_id, False, None, reasons=(f"only {len(window)} readable posts",))
    median = statistics.median(v for _, v in window)
    reasons = tuple(f"{p.id}: {_why(p, v)}" for p, v in window)
    return Verdict(account_id, median == 0, float(median), tuple(p.id for p, _ in window), reasons)


def _why(post: PostView, views: int) -> str:
    return (
        "no TikTok post id" if post.platform == "tiktok" and not post.platform_post_id else f"{views} views"
    )


_POSTS_SQL = """
select p.id::text as id, p.platform, p.published_at, p.views, p.stats_updated_at is not null as read,
       p.platform_post_id
from posts p
where p.account_id = %(account)s and p.status = 'published' and not p.dry_run
  and p.published_at <= %(cutoff)s
order by p.published_at desc
limit %(limit)s
"""


def run(now: datetime) -> list[Verdict]:
    """Check every enabled account whose hourly lane is not already paused. Never raises."""
    value = scoring_config.flag(FLAG)
    if value is None:
        return []
    rules = GuardRules.from_flag(value)
    log = get_logger(SERVICE)
    verdicts: list[Verdict] = []
    try:
        accounts = db.fetch_all(
            """select id from platform_accounts
               where enabled and 'hourly' = any(lanes)
                 and (hourly_paused_until is null or hourly_paused_until < %s)""",
            (now,),
        )
        cutoff = now - timedelta(hours=rules.min_age_h)
        for row in accounts:
            rows = db.fetch_all(
                _POSTS_SQL, {"account": row["id"], "cutoff": cutoff, "limit": rules.last_posts * 4}
            )
            posts = [PostView(**r) for r in rows]
            verdict = evaluate(row["id"], posts, now, rules)
            verdicts.append(verdict)
            if verdict.tripped:
                trip(verdict)
    except Exception as exc:  # the guard must never break the stats pass
        log.error("delivery_guard_failed", error=str(exc))
    return verdicts


def trip(verdict: Verdict) -> None:
    reason = (
        f"delivery guard: median views of the last {len(verdict.counted)} posts is 0 "
        f"({'; '.join(verdict.reasons)})"
    )[:1000]
    with db.transaction():
        changed = db.execute(
            """update platform_accounts
               set hourly_paused_until = %s, hourly_pause_reason = %s, updated_at = now()
               where id = %s and (hourly_paused_until is null or hourly_paused_until < now())""",
            (PAUSED_FOREVER, reason, verdict.account_id),
        )
        db.execute(
            "update posts set delivery_suspect = true where id = any(%s::uuid[])", (list(verdict.counted),)
        )
        events.emit(
            "delivery.guard_tripped",
            level="critical",
            entity_type="platform_account",
            entity_id=verdict.account_id,
            message=reason,
            data={"median": verdict.median, "posts": list(verdict.counted)},
            service=SERVICE,
        )
    if changed:
        notify.send(
            "critical",
            f"Hourly lane paused: {verdict.account_id}",
            f"The last {len(verdict.counted)} posts have 0 views 6 h after posting, so posts are probably "
            "not being delivered. They are marked delivery_suspect. "
            f"Resume with /resume {verdict.account_id}",
        )


def resume_lane(account_id: str, who: str = "owner") -> dict[str, Any]:
    """Clear the hourly-lane pause of one account. Returns what changed."""
    with db.transaction():
        before = db.fetch_one(
            "select hourly_paused_until, hourly_pause_reason from platform_accounts where id = %s for update",
            (account_id,),
        )
        if before is None:
            return {"account_id": account_id, "found": False, "changed": False}
        db.execute(
            """update platform_accounts set hourly_paused_until = null, hourly_pause_reason = null,
               updated_at = now() where id = %s""",
            (account_id,),
        )
        changed = before["hourly_paused_until"] is not None
        events.emit(
            "delivery.lane_resumed",
            level="warn",
            entity_type="platform_account",
            entity_id=account_id,
            message=f"hourly lane of {account_id} resumed by {who}",
            data={"old_reason": before["hourly_pause_reason"], "changed": changed},
            service=SERVICE,
        )
    if changed:
        notify.send("warn", f"Hourly lane resumed: {account_id}", f"Resumed by {who}.")
    return {"account_id": account_id, "found": True, "changed": changed}

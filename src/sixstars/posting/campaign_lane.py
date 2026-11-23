"""Campaign lanes in the hourly slot (D118, PR 4). Pure: no database.

Each account's daily limit is shared by the Normal lane and the campaign lanes (`lane_quotas`). For one
account and one slot, `order_candidates` turns the ranked hourly queue into the order the account tries:

1. Campaign clips whose campaign is furthest below its daily minimum on this account (best rank first),
   while it is campaign hours for the platform (`lanes.campaign_hours_utc`).
2. Normal clips, unless the Normal lane has used up what it may take before `lanes.return_unused_at_utc`
   (the account's daily cap minus the campaign minimums still open). From that hour on, unused campaign
   quota goes back to the Normal lane.
3. Campaign clips of campaigns already at their minimum but below their maximum.

A campaign clip is never posted as a Normal clip (it carries its campaign's look). It is left out while its
campaign is not open on this account (no quota row: paused, ended, budget gone, another platform), outside
campaign hours, at the campaign's daily maximum, or within `lanes.campaign_gap_s` of the campaign's last
post on this account. Spike posts are not ordered here; they count toward their campaign's day.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sixstars.posting.models import Account, Clip, PostRecord


@dataclass(frozen=True)
class CampaignQuota:
    """An open campaign lane on one account (`lane_quotas` row of an active campaign)."""

    campaign_id: str
    slug: str
    min_day: int
    max_day: int


@dataclass(frozen=True)
class LaneRules:
    hours: frozenset[int]  # UTC hours for campaign posts on this account's platform
    return_unused_hour: int  # from this UTC hour, unused campaign quota goes back to the Normal lane
    gap_s: int  # seconds between two posts of one campaign on one account


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def posts_today(
    account: Account, history: Sequence[PostRecord], now: datetime, dry: bool
) -> list[PostRecord]:
    """The account's posts made today (UTC) that count toward its lanes (dry-run rows in a dry run)."""
    start = _day_start(now)
    return [
        post
        for post in history
        if post.account_id == account.id
        and post.at >= start
        and post.status not in ("failed", "removed")
        and (post.dry_run if dry else (not post.dry_run and post.status != "skipped"))
    ]


def order_candidates(
    account: Account,
    candidates: Sequence[Clip],
    history: Sequence[PostRecord],
    quotas: Mapping[str, CampaignQuota],
    rules: LaneRules,
    now: datetime,
    dry: bool,
) -> list[Clip]:
    """The clips `account` should try in this slot, in order (see the module docstring)."""
    today = posts_today(account, history, now, dry)
    used: dict[str, int] = {}
    last: dict[str, datetime] = {}
    normal_used = 0
    for post in today:
        if post.campaign_id is None:
            normal_used += 1
            continue
        used[post.campaign_id] = used.get(post.campaign_id, 0) + 1
        last[post.campaign_id] = max(last.get(post.campaign_id, post.at), post.at)
    open_minimum = sum(max(0, q.min_day - used.get(q.campaign_id, 0)) for q in quotas.values())
    in_hours = now.hour in rules.hours
    # Before the return hour the Normal lane leaves room for the campaign minimums still open today.
    normal_room = account.cap_24h if now.hour >= rules.return_unused_hour else account.cap_24h - open_minimum

    def campaign_ok(clip: Clip) -> bool:
        quota = quotas.get(clip.campaign_id or "")
        if quota is None or not in_hours:
            return False
        if used.get(quota.campaign_id, 0) >= quota.max_day:
            return False
        previous = last.get(quota.campaign_id)
        return previous is None or now - previous >= timedelta(seconds=rules.gap_s)

    below_min: list[tuple[int, int, Clip]] = []
    above_min: list[Clip] = []
    normal: list[Clip] = []
    for index, clip in enumerate(candidates):
        if clip.campaign_id is None:
            normal.append(clip)
            continue
        if not campaign_ok(clip):
            continue
        quota = quotas[clip.campaign_id]
        deficit = quota.min_day - used.get(quota.campaign_id, 0)
        if deficit > 0:
            below_min.append((-deficit, index, clip))
        else:
            above_min.append(clip)
    ordered = [clip for _, _, clip in sorted(below_min, key=lambda item: (item[0], item[1]))]
    if normal_used < normal_room:
        ordered += normal
    return ordered + above_min

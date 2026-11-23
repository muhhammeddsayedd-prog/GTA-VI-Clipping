"""Pure rules of the campaign registry (D117): status transitions, the checks every save passes, quota
parsing, the lane-quota rows a campaign holds, and the tier a streamer gets. No database, no network.

Every save, from any door (CLI, console, Telegram), goes through `sixstars.campaigns.service.apply_change`,
which calls `validate` and refuses the save when it returns any problem.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from sixstars.campaigns.models import (
    COLUMN_FIELDS,
    PLATFORMS,
    AccountInfo,
    Campaign,
    Context,
    PlatformQuota,
    Status,
)

# Which status may follow which. An ended campaign can be reopened (after its budget is edited).
TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "draft": frozenset({"active", "ended"}),
    "active": frozenset({"paused", "ended"}),
    "paused": frozenset({"active", "ended"}),
    "ended": frozenset({"active"}),
}
FORBIDDEN_TIERS: Final = frozenset({"restricted", "blocked"})
LIFTABLE_TIERS: Final = frozenset({"preferred", "auto"})  # tiers that become `campaign` while active
CAMPAIGN_TIER: Final = "campaign"


def transition_error(old: Status, new: Status) -> str | None:
    """None when `old -> new` is allowed (or no change), else why not."""
    if old == new or new in TRANSITIONS[old]:
        return None
    return f"status {old} cannot change to {new}"


def parse_quota(raw: Mapping[str, Any]) -> dict[str, PlatformQuota]:
    """`{"youtube": {"min": 2, "max": 3}}` (or `"2-3"`, or a bare max) -> quotas. Raises ValueError."""
    quotas: dict[str, PlatformQuota] = {}
    for platform, value in raw.items():
        if isinstance(value, Mapping):
            low, high = int(value.get("min", 0)), int(value["max"])
        elif isinstance(value, str) and "-" in value:
            first, _, second = value.partition("-")
            low, high = int(first), int(second)
        else:
            low, high = 0, int(value)
        quotas[str(platform)] = PlatformQuota(min=low, max=high)
    return quotas


def quota_json(quota: Mapping[str, PlatformQuota]) -> dict[str, dict[str, int]]:
    return {platform: {"min": q.min, "max": q.max} for platform, q in sorted(quota.items())}


def accounts_for(platform: str, accounts: Iterable[AccountInfo]) -> list[AccountInfo]:
    return [account for account in accounts if account.platform == platform]


def lane_rows(campaign: Campaign, accounts: Iterable[AccountInfo]) -> list[tuple[str, str, int, int]]:
    """The `lane_quotas` rows `(account_id, lane_key, min_day, max_day)` an open campaign holds: its quota
    on every enabled account of each of its platforms. An ended campaign holds none."""
    if not campaign.is_open:
        return []
    rows: list[tuple[str, str, int, int]] = []
    pool = list(accounts)
    for platform in campaign.platforms:
        quota = campaign.quota.get(platform)
        if quota is None:
            continue
        for account in accounts_for(platform, pool):
            rows.append((account.id, campaign.lane_key, quota.min, quota.max))
    return rows


SLUG_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")


def _number_problems(c: Campaign) -> list[str]:
    problems: list[str] = []
    if not SLUG_PATTERN.fullmatch(c.slug):
        problems.append(f"slug {c.slug!r}: use 2-63 lower-case letters, digits and dashes")
    if c.rate_per_1k <= 0:
        problems.append("rate_per_1k must be above 0")
    if c.min_payout < 0:
        problems.append("min_payout must not be negative")
    if c.max_payout is not None and c.max_payout < c.min_payout:
        problems.append("max_payout must be at least min_payout")
    if c.budget_total is not None and c.budget_total <= 0:
        problems.append("budget_total must be above 0")
    if c.budget_left is not None and c.budget_left < 0:
        problems.append("budget_left must not be negative")
    if c.budget_total is not None and c.budget_left is not None and c.budget_left > c.budget_total:
        problems.append("budget_left is more than budget_total")
    if c.priority < 0:
        problems.append("priority must not be negative")
    return problems


def _creator_problems(c: Campaign, ctx: Context) -> list[str]:
    if not c.creator_ids:
        return ["a campaign needs at least one streamer"]
    problems: list[str] = []
    for creator_id in c.creator_ids:
        creator = ctx.creators.get(creator_id)
        if creator is None:
            problems.append(f"streamer {creator_id} is unknown")
        elif creator.tier in FORBIDDEN_TIERS:
            problems.append(f"streamer {creator.platform}:{creator.handle} is {creator.tier}: never clipped")
    return problems


def _platform_problems(c: Campaign, ctx: Context) -> list[str]:
    problems: list[str] = []
    if not c.platforms:
        problems.append("a campaign needs at least one platform")
    for platform in c.platforms:
        if platform not in PLATFORMS:
            problems.append(f"platform {platform} is not supported (Kick and X have no source/account yet)")
        elif not accounts_for(platform, ctx.accounts):
            problems.append(f"no enabled {platform} account")
    for platform in c.quota:
        if platform not in c.platforms:
            problems.append(f"quota for {platform}, which is not one of the campaign's platforms")
    for platform in c.platforms:
        quota = c.quota.get(platform)
        if quota is None:
            problems.append(f"no quota for {platform}")
            continue
        if quota.min < 0 or quota.max < 1 or quota.min > quota.max:
            problems.append(f"{platform} quota {quota.min}-{quota.max} is not a valid min-max")
        ceiling = ctx.max_per_day.get(platform)
        if ceiling is not None and quota.max > ceiling:
            problems.append(f"{platform} quota max {quota.max} is above the per-campaign limit {ceiling}")
    return problems


def _profile_problems(c: Campaign, ctx: Context) -> list[str]:
    profile = ctx.profiles.get(c.render_profile)
    if profile is None:
        return [f"render profile {c.render_profile} does not exist"]
    if profile.logo_pos is not None and not profile.logo_path:
        return [f"render profile {c.render_profile} places a logo but has none uploaded"]
    return []


def _share_problems(c: Campaign, ctx: Context) -> list[str]:
    """Campaign quotas fit inside each account's campaign share; the share fits inside the account cap.
    Counted over this campaign plus every other open (active or paused) campaign."""
    problems: list[str] = []
    load: dict[str, int] = {}
    for campaign in (c, *[o for o in ctx.others if o.slug != c.slug and o.status in ("active", "paused")]):
        for account_id, _lane, _low, high in lane_rows(campaign, ctx.accounts):
            load[account_id] = load.get(account_id, 0) + high
    for account in ctx.accounts:
        share = ctx.campaign_share.get(account.id, 0)
        if share > account.cap_24h:
            problems.append(
                f"{account.id}: campaign share {share} is above the account cap {account.cap_24h}"
            )
        used = load.get(account.id, 0)
        if used > share:
            problems.append(
                f"{account.id}: campaign quotas add up to {used} a day, above the campaign share {share} "
                "(lower a quota or raise lanes.campaign_share)"
            )
    return problems


def _conflict_problems(c: Campaign, ctx: Context) -> list[str]:
    """No other active campaign has the same streamer on the same platform, unless priorities say who wins."""
    problems: list[str] = []
    mine = set(c.creator_ids)
    for other in ctx.others:
        if other.slug == c.slug or other.status != "active":
            continue
        shared_creators = mine & set(other.creator_ids)
        shared_platforms = set(c.platforms) & set(other.platforms)
        if shared_creators and shared_platforms and other.priority == c.priority:
            problems.append(
                f"campaign {other.slug} has the same streamer on {', '.join(sorted(shared_platforms))} "
                "with the same priority: set a different priority so one wins"
            )
    return problems


def validate(c: Campaign, ctx: Context) -> list[str]:
    """Every problem that blocks saving `c` (empty = fine). An ended campaign is history: only its numbers
    are checked. Dates are checked when the campaign is active (it must not have ended already)."""
    problems = _number_problems(c)
    if c.status == "ended":
        return problems
    problems += _creator_problems(c, ctx)
    problems += _platform_problems(c, ctx)
    problems += _profile_problems(c, ctx)
    problems += _share_problems(c, ctx)
    problems += _conflict_problems(c, ctx)
    if c.status == "active" and c.ends_at is not None and c.ends_at <= ctx.now:
        problems.append("ends_at is in the past")
    if c.status == "active" and c.budget_left is not None and c.budget_left <= 0:
        problems.append("budget_left is 0: update it before activating")
    return problems


def tier_for(current: str, before: str | None, has_active_campaign: bool) -> tuple[str, str | None]:
    """The `(tier, tier_before_campaign)` a streamer should have. `campaign` is set over `preferred`/`auto`
    while any of their campaigns is active and dropped back to the remembered tier when none is. Forbidden
    tiers are never touched."""
    if current in FORBIDDEN_TIERS:
        return current, before
    if has_active_campaign:
        if current == CAMPAIGN_TIER:
            return current, before
        return CAMPAIGN_TIER, current if current in LIFTABLE_TIERS else "auto"
    if current == CAMPAIGN_TIER:
        return (before if before in LIFTABLE_TIERS else "auto"), None
    return current, before


def diff(old: Campaign | None, new: Campaign) -> dict[str, list[Any]]:
    """Fields that changed, as `{field: [old, new]}` (for the `campaign.changed` audit event)."""
    names = [*(f for f in COLUMN_FIELDS if f != "budget_checked_at"), "creator_ids"]
    changes: dict[str, list[Any]] = {}
    for name in names:
        before = None if old is None else getattr(old, name)
        after = getattr(new, name)
        if name == "quota":
            before = None if old is None else quota_json(old.quota)
            after = quota_json(new.quota)
        if before != after:
            changes[name] = [_plain(before), _plain(after)]
    return changes


def _plain(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value

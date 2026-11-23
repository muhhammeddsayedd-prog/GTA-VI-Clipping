"""SQL for the campaign registry (D117): read and write `clip_campaigns`, its streamers, its `lane_quotas`
rows, and the context a save is validated against. Writes are meant to run inside `db.transaction()`
opened by `sixstars.campaigns.service`.
"""

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db, settings
from sixstars.campaigns import rules
from sixstars.campaigns.models import (
    COLUMN_FIELDS,
    AccountInfo,
    Campaign,
    Context,
    CreatorInfo,
    ProfileInfo,
)

SHARE_KEY = "lanes.campaign_share"
MAX_PER_DAY_KEY = "lanes.campaign_max_per_day"
DEFAULT_SHARE: dict[str, int] = {"youtube_main": 6, "instagram_main": 18, "tiktok_main": 6}
DEFAULT_MAX_PER_DAY: dict[str, int] = {"youtube": 3, "instagram": 10, "tiktok": 3}

_COLUMNS = ("slug", *COLUMN_FIELDS)
_JSON_COLUMNS = frozenset({"caption_rules", "brief_rules", "quota"})


def _num(value: Any) -> float | None:
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal | int | float) else float(str(value))


def campaign_from_row(row: Mapping[str, Any], creator_ids: Sequence[str]) -> Campaign:
    return Campaign(
        id=str(row["id"]),
        slug=row["slug"],
        name=row["name"],
        status=row["status"],
        platforms=tuple(row["platforms"] or ()),
        creator_ids=tuple(creator_ids),
        rate_per_1k=float(row["rate_per_1k"]),
        min_payout=float(row["min_payout"]),
        max_payout=_num(row["max_payout"]),
        currency=row["currency"],
        budget_total=_num(row["budget_total"]),
        budget_left=_num(row["budget_left"]),
        budget_checked_at=row["budget_checked_at"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        render_profile=row["render_profile"],
        caption_rules=dict(row["caption_rules"] or {}),
        brief_rules=dict(row["brief_rules"] or {}),
        quota=rules.parse_quota(row["quota"] or {}),
        priority=int(row["priority"]),
        required_game=row["required_game"],
        submit_url=row["submit_url"],
        notes=row["notes"],
        version=int(row["version"]),
        end_reason=row["end_reason"],
    )


def _creators_of(campaign_ids: Iterable[str]) -> dict[str, list[str]]:
    ids = list(campaign_ids)
    if not ids:
        return {}
    rows = db.fetch_all(
        "select campaign_id::text as campaign_id, creator_id::text as creator_id from clip_campaign_creators "
        "where campaign_id = any(%s::uuid[]) order by created_at, creator_id",
        (ids,),
    )
    found: dict[str, list[str]] = {}
    for row in rows:
        found.setdefault(row["campaign_id"], []).append(row["creator_id"])
    return found


def load(slug: str, lock: bool = False) -> Campaign | None:
    """One campaign by slug; `lock` takes a row lock for the rest of the transaction."""
    row = db.fetch_one(
        "select * from clip_campaigns where slug = %s" + (" for update" if lock else ""), (slug,)
    )
    if row is None:
        return None
    return campaign_from_row(row, _creators_of([str(row["id"])]).get(str(row["id"]), []))


def load_all(statuses: Sequence[str] | None = None) -> list[Campaign]:
    """Campaigns (optionally only these statuses), highest priority first, then by slug."""
    if statuses:
        rows = db.fetch_all(
            "select * from clip_campaigns where status = any(%s) order by priority desc, slug",
            (list(statuses),),
        )
    else:
        rows = db.fetch_all("select * from clip_campaigns order by priority desc, slug")
    creators = _creators_of(str(row["id"]) for row in rows)
    return [campaign_from_row(row, creators.get(str(row["id"]), [])) for row in rows]


def context(campaign: Campaign, now: Any) -> Context:
    """The facts a save of `campaign` is validated against."""
    others = tuple(c for c in load_all(("active", "paused")) if c.slug != campaign.slug)
    creator_ids = sorted({*campaign.creator_ids, *(i for c in others for i in c.creator_ids)})
    creator_rows = (
        db.fetch_all(
            "select id::text as id, platform, handle, tier from creators where id = any(%s::uuid[])",
            (creator_ids,),
        )
        if creator_ids
        else []
    )
    profile_rows = db.fetch_all("select id, logo_pos, logo_path from render_profiles")
    account_rows = db.fetch_all(
        "select id, platform, cap_24h from platform_accounts where enabled order by id"
    )
    share = settings.get_dict(SHARE_KEY, DEFAULT_SHARE)
    ceiling = settings.get_dict(MAX_PER_DAY_KEY, DEFAULT_MAX_PER_DAY)
    return Context(
        now=now,
        creators={r["id"]: CreatorInfo(r["id"], r["platform"], r["handle"], r["tier"]) for r in creator_rows},
        profiles={r["id"]: ProfileInfo(r["id"], r["logo_pos"], r["logo_path"]) for r in profile_rows},
        accounts=tuple(AccountInfo(r["id"], r["platform"], int(r["cap_24h"])) for r in account_rows),
        campaign_share={str(k): int(v) for k, v in share.items()},
        max_per_day={str(k): int(v) for k, v in ceiling.items()},
        others=others,
    )


def _values(campaign: Campaign) -> list[Any]:
    values: list[Any] = []
    for column in _COLUMNS:
        value: Any = getattr(campaign, column)
        if column == "quota":
            value = Jsonb(rules.quota_json(campaign.quota))
        elif column in _JSON_COLUMNS:
            value = Jsonb(dict(value))
        elif column == "platforms":
            value = list(value)
        values.append(value)
    return values


def save(campaign: Campaign, old: Campaign | None) -> Campaign:
    """Insert or update the row (version + 1 on update), stamp activation/end times, and replace its
    streamer links. Returns the stored campaign."""
    values = _values(campaign)
    if old is None:
        placeholders = ", ".join(["%s"] * len(_COLUMNS))
        row = db.fetch_one(
            f"insert into clip_campaigns ({', '.join(_COLUMNS)}) values ({placeholders}) "
            "returning id, version",
            values,
        )
    else:
        assignments = ", ".join(f"{column} = %s" for column in _COLUMNS)
        row = db.fetch_one(
            f"update clip_campaigns set {assignments}, version = version + 1, "
            "activated_at = case when %s = 'active' and status <> 'active' then now() else activated_at end, "
            "ended_at = case when %s = 'ended' and status <> 'ended' then now() "
            "                when %s <> 'ended' then null else ended_at end "
            "where id = %s returning id, version",
            [*values, campaign.status, campaign.status, campaign.status, old.id],
        )
    assert row is not None
    campaign_id = str(row["id"])
    if old is None and campaign.status == "active":
        db.execute("update clip_campaigns set activated_at = now() where id = %s", (campaign_id,))
    db.execute("delete from clip_campaign_creators where campaign_id = %s", (campaign_id,))
    for creator_id in dict.fromkeys(campaign.creator_ids):
        db.execute(
            "insert into clip_campaign_creators (campaign_id, creator_id) values (%s, %s)",
            (campaign_id, creator_id),
        )
    stored = load(campaign.slug)
    assert stored is not None
    return stored


def write_lane_rows(campaign: Campaign, accounts: Iterable[AccountInfo]) -> int:
    """Replace the campaign's `lane_quotas` rows (none once it has ended). Returns the rows written."""
    db.execute("delete from lane_quotas where lane_key = %s", (campaign.lane_key,))
    rows = rules.lane_rows(campaign, accounts)
    for account_id, lane_key, low, high in rows:
        db.execute(
            "insert into lane_quotas (account_id, lane_key, min_day, max_day) values (%s, %s, %s, %s)",
            (account_id, lane_key, low, high),
        )
    return len(rows)


def refresh_base_rows() -> None:
    """Normal lane = account cap minus the campaign share (the spike reserve sits inside it); Spike lane =
    the spike reserve. Kept in step with the accounts and `lanes.campaign_share`."""
    share = settings.get_dict(SHARE_KEY, DEFAULT_SHARE)
    for row in db.fetch_all("select id, cap_24h, spike_reserve from platform_accounts"):
        normal = max(int(row["cap_24h"]) - int(share.get(row["id"], 0)), 0)
        for lane_key, high in (("normal", normal), ("spike", int(row["spike_reserve"]))):
            db.execute(
                "insert into lane_quotas (account_id, lane_key, min_day, max_day) values (%s, %s, 0, %s) "
                "on conflict (account_id, lane_key) do update set max_day = excluded.max_day "
                "where lane_quotas.max_day is distinct from excluded.max_day",
                (row["id"], lane_key, high),
            )


def lane_quotas(account_id: str | None = None) -> list[dict[str, Any]]:
    if account_id:
        return db.fetch_all(
            "select * from lane_quotas where account_id = %s order by account_id, lane_key", (account_id,)
        )
    return db.fetch_all("select * from lane_quotas order by account_id, lane_key")


def creator_tiers(creator_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Tier facts and whether each creator has any active campaign."""
    ids = sorted(set(creator_ids))
    if not ids:
        return []
    return db.fetch_all(
        """
        select c.id::text as id, c.platform, c.handle, c.tier, c.tier_before_campaign,
               exists (select 1 from clip_campaign_creators l join clip_campaigns k on k.id = l.campaign_id
                       where l.creator_id = c.id and k.status = 'active') as has_active
        from creators c where c.id = any(%s::uuid[])
        """,
        (ids,),
    )


def set_tier(creator_id: str, tier: str, before: str | None, campaign_slug: str) -> None:
    db.execute(
        "update creators set tier = %s, tier_before_campaign = %s, tier_reason = %s, updated_at = now() "
        "where id = %s",
        (
            tier,
            before,
            f"campaign: {campaign_slug}" if tier == "campaign" else f"campaign ended: {campaign_slug}",
            creator_id,
        ),
    )


def active_campaigns_of_creator(creator_id: str) -> list[str]:
    rows = db.fetch_all(
        "select k.slug from clip_campaigns k join clip_campaign_creators l on l.campaign_id = k.id "
        "where l.creator_id = %s and k.status = 'active' order by k.slug",
        (creator_id,),
    )
    return [str(row["slug"]) for row in rows]


def upsert_creator(
    platform: str, channel_id: str, handle: str, display_name: str | None, followers: int | None
) -> dict[str, Any]:
    """Register (or refresh) a streamer found by lookup; new rows start at tier `auto`."""
    row = db.fetch_one(
        """
        insert into creators (platform, channel_id, handle, display_name, follower_count)
        values (%s, %s, %s, %s, %s)
        on conflict (platform, channel_id) do update
          set handle = excluded.handle,
              display_name = coalesce(excluded.display_name, creators.display_name),
              follower_count = coalesce(excluded.follower_count, creators.follower_count)
        returning id::text as id, platform, channel_id, handle, display_name, tier, follower_count
        """,
        (platform, channel_id, handle, display_name, followers),
    )
    assert row is not None
    return row


def find_creator(platform: str, ref: str) -> dict[str, Any] | None:
    """A known creator by channel id or handle (case-insensitive, with or without @)."""
    return db.fetch_one(
        "select id::text as id, platform, channel_id, handle, display_name, tier, follower_count "
        "from creators where platform = %s and (channel_id = %s or lower(handle) = lower(%s))",
        (platform, ref, ref.removeprefix("@")),
    )

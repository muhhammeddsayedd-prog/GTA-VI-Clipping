"""Clipping-campaign registry against the real schema (D118, PR 1): migration 0050, `apply_change` with its
checks and versions, lane quotas, tier `campaign` in and out, the audit event, and the pause when a
streamer forbids clipping."""

from datetime import UTC, datetime, timedelta

import pytest
from posting_support import seed

from sixstars import db
from sixstars.ai.schemas import PermissionVerdict
from sixstars.campaigns import service, store
from sixstars.discovery import tiers
from sixstars.discovery.store import creator_from_row

LATER = datetime.now(UTC) + timedelta(days=5)


@pytest.fixture(autouse=True)
def clean() -> None:
    db.execute(
        "truncate clip_campaigns, clip_campaign_creators, lane_quotas, creator_live_status, posts, clips, "
        "spikes, creators, platform_accounts restart identity cascade"
    )
    seed("production")
    store.refresh_base_rows()


def add_creator(handle: str, tier: str = "auto") -> str:
    row = db.fetch_one(
        "insert into creators (platform, channel_id, handle, tier) values ('twitch', %s, %s, %s) "
        "returning id::text as id",
        (f"id-{handle}", handle, tier),
    )
    assert row is not None
    return str(row["id"])


def tier_of(creator_id: str) -> tuple[str, str | None]:
    row = db.fetch_one("select tier, tier_before_campaign from creators where id = %s", (creator_id,))
    assert row is not None
    return row["tier"], row["tier_before_campaign"]


def new_campaign(slug: str, creator_id: str, **extra: object) -> service.Campaign:
    changes: dict[str, object] = {
        "name": f"Campaign {slug}",
        "rate_per_1k": 1.2,
        "min_payout": 4,
        "max_payout": 250,
        "budget_total": 5000,
        "budget_left": 5000,
        "ends_at": LATER,
        "platforms": ["youtube", "instagram"],
        "creator_ids": [creator_id],
        "quota": {"youtube": "2-3", "instagram": "6-10"},
        "caption_rules": {"required_tags": ["#DylanPage"]},
    }
    changes.update(extra)
    return service.apply_change(slug, changes, "test", create=True)


def lane_rows(slug: str) -> dict[str, tuple[int, int]]:
    rows = db.fetch_all("select * from lane_quotas where lane_key = %s", (f"campaign:{slug}",))
    return {row["account_id"]: (row["min_day"], row["max_day"]) for row in rows}


def test_normal_and_spike_rows_exist_for_every_account() -> None:
    rows = {(r["account_id"], r["lane_key"]): r["max_day"] for r in store.lane_quotas()}
    assert rows[("youtube_main", "normal")] == 10  # cap 16 - campaign share 6
    assert rows[("instagram_main", "normal")] == 27  # cap 45 - 18
    assert rows[("youtube_main", "spike")] == 10  # migration 0056


def test_add_activate_pause_end_moves_the_tier_and_the_lane() -> None:
    creator = add_creator("dylanpage", "preferred")
    draft = new_campaign("dylan", creator)
    assert (draft.status, draft.version) == ("draft", 1)
    assert lane_rows("dylan") == {"youtube_main": (2, 3), "instagram_main": (6, 10)}
    assert tier_of(creator) == ("preferred", None)  # a draft does not change the tier

    active = service.set_status("dylan", "active", "test")
    assert (active.status, active.version) == ("active", 2)
    assert tier_of(creator) == ("campaign", "preferred")
    stamped = db.fetch_one("select activated_at from clip_campaigns where slug = 'dylan'")
    assert stamped is not None and stamped["activated_at"] is not None

    service.set_status("dylan", "paused", "test", "testing")
    assert tier_of(creator) == ("preferred", None)
    assert lane_rows("dylan") == {"youtube_main": (2, 3), "instagram_main": (6, 10)}

    ended = service.set_status("dylan", "ended", "test", "marathon over")
    assert ended.end_reason == "marathon over"
    assert lane_rows("dylan") == {}

    events = db.fetch_all("select data from events where type = 'campaign.changed' order by id")
    assert len(events) == 4
    assert events[1]["data"]["changes"]["status"] == ["draft", "active"]
    moved = {"creator": "twitch:dylanpage", "old": "preferred", "new": "campaign"}
    assert events[1]["data"]["tiers"] == [moved]


def test_a_refused_change_saves_nothing() -> None:
    creator = add_creator("lucky")
    new_campaign("lucky", creator)
    with pytest.raises(service.CampaignError) as refused:
        service.apply_change("lucky", {"quota": {"youtube": "2-9", "instagram": "6-10"}}, "test")
    assert "per-campaign limit" in str(refused.value)
    stored = store.load("lucky")
    assert stored is not None and stored.version == 1
    with pytest.raises(service.CampaignError):
        service.set_status("lucky", "paused", "test")  # draft -> paused is not a transition
    with pytest.raises(service.CampaignError):
        new_campaign("lucky", creator)  # already exists


def test_quotas_of_open_campaigns_must_fit_the_share() -> None:
    first = add_creator("first")
    second = add_creator("second")
    new_campaign("first", first)
    service.set_status("first", "active", "test")
    with pytest.raises(service.CampaignError) as refused:
        new_campaign("second", second)  # Instagram 10 + 10 > share 18
    assert "instagram_main" in str(refused.value)
    new_campaign("second", second, quota={"youtube": "1-3", "instagram": "4-8"})
    assert lane_rows("second") == {"youtube_main": (1, 3), "instagram_main": (4, 8)}


def test_the_same_streamer_in_two_campaigns_needs_priorities() -> None:
    creator = add_creator("shared")
    new_campaign("camp-a", creator, quota={"youtube": "1-2", "instagram": "1-5"})
    service.set_status("camp-a", "active", "test")
    with pytest.raises(service.CampaignError) as refused:
        new_campaign("camp-b", creator, quota={"youtube": "1-1", "instagram": "1-5"})
    assert "same priority" in str(refused.value)
    new_campaign("camp-b", creator, quota={"youtube": "1-1", "instagram": "1-5"}, priority=1)
    service.set_status("camp-b", "active", "test")
    service.set_status("camp-a", "ended", "test")
    assert tier_of(creator)[0] == "campaign"  # still in an active campaign
    service.set_status("camp-b", "ended", "test")
    assert tier_of(creator) == ("auto", None)


def test_the_permission_read_keeps_campaign_but_a_ban_pauses_the_campaign() -> None:
    creator_id = add_creator("ban-me")
    new_campaign("banme", creator_id)
    service.set_status("banme", "active", "test")
    row = db.fetch_one("select * from creators where id = %s", (creator_id,))
    assert row is not None
    creator = creator_from_row(row)
    now = datetime.now(UTC)

    tiers.apply_verdict(creator, PermissionVerdict(tier="auto", evidence=None), now)
    assert tier_of(creator_id)[0] == "campaign"

    tiers.apply_verdict(creator, PermissionVerdict(tier="restricted", evidence="no reuploads"), now)
    assert tier_of(creator_id)[0] == "restricted"
    campaign = store.load("banme")
    assert campaign is not None and campaign.status == "paused"


def test_ended_campaign_can_be_reopened_after_a_budget_update() -> None:
    creator = add_creator("reopen")
    new_campaign("reopen", creator)
    service.set_status("reopen", "active", "test")
    with pytest.raises(service.CampaignError):
        service.apply_change("reopen", {"budget_left": 0}, "test")  # an active campaign needs budget
    service.set_status("reopen", "ended", "test", "budget out")
    service.apply_change("reopen", {"budget_left": 0}, "test")  # history: only the numbers are checked
    with pytest.raises(service.CampaignError):
        service.set_status("reopen", "active", "test")
    service.apply_change("reopen", {"budget_left": 3000}, "test")
    reopened = service.set_status("reopen", "active", "test")
    assert reopened.status == "active" and reopened.end_reason == "budget out"
    assert lane_rows("reopen") == {"youtube_main": (2, 3), "instagram_main": (6, 10)}

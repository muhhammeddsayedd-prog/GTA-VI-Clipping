"""Pure rules of the clipping-campaign registry (D117): transitions, save checks, quota fit, lane rows,
tiers and the audit diff. No database, no network."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sixstars.campaigns import rules
from sixstars.campaigns.models import (
    AccountInfo,
    Campaign,
    Context,
    CreatorInfo,
    PlatformQuota,
    ProfileInfo,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
ACCOUNTS = (
    AccountInfo("youtube_main", "youtube", 16),
    AccountInfo("instagram_main", "instagram", 45),
    AccountInfo("tiktok_main", "tiktok", 14),
)
SHARE = {"youtube_main": 6, "instagram_main": 18, "tiktok_main": 6}
MAX_PER_DAY = {"youtube": 3, "instagram": 10, "tiktok": 3}


def campaign(**overrides: object) -> Campaign:
    base = Campaign(
        slug="dylan",
        name="Dylan Page marathon",
        rate_per_1k=1.2,
        status="active",
        platforms=("youtube", "instagram"),
        creator_ids=("c1",),
        min_payout=4,
        max_payout=250,
        budget_total=5000,
        budget_left=4920,
        ends_at=NOW + timedelta(days=5),
        quota={"youtube": PlatformQuota(2, 3), "instagram": PlatformQuota(6, 10)},
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def context(*others: Campaign, **overrides: object) -> Context:
    base = Context(
        now=NOW,
        creators={
            "c1": CreatorInfo("c1", "youtube", "dylanpage", "auto"),
            "c2": CreatorInfo("c2", "twitch", "lucky", "preferred"),
            "bad": CreatorInfo("bad", "twitch", "nope", "restricted"),
        },
        profiles={
            "brand-disclose": ProfileInfo("brand-disclose", None, None),
            "partner-logo": ProfileInfo("partner-logo", "bottom_right", None),
            "partner-logo-gtaclipper": ProfileInfo("partner-logo-gtaclipper", "bottom_right", "logos/gc.png"),
        },
        accounts=ACCOUNTS,
        campaign_share=SHARE,
        max_per_day=MAX_PER_DAY,
        others=others,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# ── transitions ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("old", "new", "ok"),
    [
        ("draft", "active", True),
        ("draft", "paused", False),
        ("active", "paused", True),
        ("paused", "active", True),
        ("active", "draft", False),
        ("ended", "active", True),  # reopened after the budget is edited
        ("ended", "paused", False),
        ("active", "active", True),
    ],
)
def test_transitions(old: str, new: str, ok: bool) -> None:
    assert (rules.transition_error(old, new) is None) is ok  # type: ignore[arg-type]


# ── quotas ───────────────────────────────────────────────────


def test_parse_quota_accepts_ranges_objects_and_bare_max() -> None:
    assert rules.parse_quota({"youtube": "2-3", "instagram": {"min": 6, "max": 10}, "tiktok": 3}) == {
        "youtube": PlatformQuota(2, 3),
        "instagram": PlatformQuota(6, 10),
        "tiktok": PlatformQuota(0, 3),
    }
    with pytest.raises(ValueError):
        rules.parse_quota({"youtube": "two-three"})


def test_lane_rows_cover_every_account_of_each_platform_and_none_once_ended() -> None:
    rows = rules.lane_rows(campaign(), ACCOUNTS)
    assert rows == [
        ("youtube_main", "campaign:dylan", 2, 3),
        ("instagram_main", "campaign:dylan", 6, 10),
    ]
    assert rules.lane_rows(campaign(status="ended"), ACCOUNTS) == []
    assert rules.lane_rows(campaign(status="paused"), ACCOUNTS) == rows


# ── validation ───────────────────────────────────────────────


def test_a_good_campaign_passes() -> None:
    assert rules.validate(campaign(), context()) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"rate_per_1k": 0}, "rate_per_1k"),
        ({"min_payout": 10, "max_payout": 5}, "max_payout"),
        ({"budget_left": 6000}, "more than budget_total"),
        ({"creator_ids": ()}, "at least one streamer"),
        ({"creator_ids": ("bad",)}, "restricted"),
        ({"creator_ids": ("ghost",)}, "unknown"),
        ({"platforms": ()}, "at least one platform"),
        ({"platforms": ("youtube", "instagram", "kick")}, "kick"),
        ({"quota": {"youtube": PlatformQuota(2, 3)}}, "no quota for instagram"),
        ({"quota": {"youtube": PlatformQuota(4, 3), "instagram": PlatformQuota(6, 10)}}, "not a valid"),
        (
            {"quota": {"youtube": PlatformQuota(2, 5), "instagram": PlatformQuota(6, 10)}},
            "per-campaign limit",
        ),
        ({"render_profile": "missing"}, "does not exist"),
        ({"render_profile": "partner-logo"}, "has none uploaded"),
        ({"ends_at": NOW - timedelta(hours=1)}, "in the past"),
        ({"budget_left": 0}, "budget_left is 0"),
    ],
)
def test_save_checks(overrides: dict[str, object], fragment: str) -> None:
    problems = rules.validate(campaign(**overrides), context())
    assert any(fragment in problem for problem in problems), problems


def test_quota_extra_platform_is_refused() -> None:
    c = campaign(
        quota={
            "youtube": PlatformQuota(2, 3),
            "instagram": PlatformQuota(6, 10),
            "tiktok": PlatformQuota(1, 2),
        }
    )
    assert any("not one of the campaign's platforms" in p for p in rules.validate(c, context()))


def test_a_logo_profile_with_its_logo_is_fine() -> None:
    assert rules.validate(campaign(render_profile="partner-logo-gtaclipper"), context()) == []


def test_drafts_and_paused_campaigns_may_have_old_end_dates() -> None:
    assert rules.validate(campaign(status="paused", ends_at=NOW - timedelta(days=1)), context()) == []


def test_ended_campaigns_only_check_numbers() -> None:
    assert rules.validate(campaign(status="ended", creator_ids=(), platforms=()), context()) == []


def test_quotas_must_fit_the_campaign_share() -> None:
    other = campaign(
        slug="tomo",
        creator_ids=("c2",),
        priority=1,
        quota={"youtube": PlatformQuota(2, 3), "instagram": PlatformQuota(6, 10)},
    )
    # 3 + 3 = 6 on YouTube fits the share of 6; 10 + 10 = 20 on Instagram does not fit 18.
    problems = rules.validate(campaign(), context(other))
    assert problems == [
        "instagram_main: campaign quotas add up to 20 a day, above the campaign share 18 "
        "(lower a quota or raise lanes.campaign_share)"
    ]
    # Ended campaigns hold no quota.
    assert rules.validate(campaign(), context(replace(other, status="ended"))) == []


def test_the_share_must_fit_the_account_cap() -> None:
    problems = rules.validate(campaign(), context(campaign_share={**SHARE, "youtube_main": 20}))
    assert any("above the account cap 16" in p for p in problems)


def test_same_streamer_same_platform_needs_a_priority() -> None:
    other = campaign(slug="other", quota={"youtube": PlatformQuota(0, 1), "instagram": PlatformQuota(0, 1)})
    problems = rules.validate(campaign(), context(other))
    assert any("same priority" in p for p in problems)
    assert rules.validate(campaign(priority=1), context(other)) == []
    # A paused campaign does not conflict.
    assert rules.validate(campaign(), context(replace(other, status="paused"))) == []


# ── tiers ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "before", "active", "expected"),
    [
        ("auto", None, True, ("campaign", "auto")),
        ("preferred", None, True, ("campaign", "preferred")),
        ("campaign", "preferred", True, ("campaign", "preferred")),
        ("campaign", "preferred", False, ("preferred", None)),
        ("campaign", None, False, ("auto", None)),
        ("auto", None, False, ("auto", None)),
        ("restricted", "auto", True, ("restricted", "auto")),
        ("blocked", None, False, ("blocked", None)),
    ],
)
def test_tier_for(current: str, before: str | None, active: bool, expected: tuple[str, str | None]) -> None:
    assert rules.tier_for(current, before, active) == expected


# ── audit diff ───────────────────────────────────────────────


def test_diff_lists_changed_fields_in_plain_values() -> None:
    old = campaign()
    new = replace(
        old, status="paused", quota={"youtube": PlatformQuota(1, 2), "instagram": PlatformQuota(6, 10)}
    )
    changes = rules.diff(old, new)
    assert changes == {
        "status": ["active", "paused"],
        "quota": [
            {"instagram": {"min": 6, "max": 10}, "youtube": {"min": 2, "max": 3}},
            {"instagram": {"min": 6, "max": 10}, "youtube": {"min": 1, "max": 2}},
        ],
    }
    assert rules.diff(old, old) == {}
    created = rules.diff(None, old)
    assert created["creator_ids"] == [None, ["c1"]]
    assert created["ends_at"][1] == old.ends_at.isoformat()  # type: ignore[union-attr]


@pytest.mark.parametrize("slug", ["a", "Dylan", "dylan page", "-dylan"])
def test_bad_slugs_are_refused(slug: str) -> None:
    assert any("slug" in p for p in rules.validate(campaign(slug=slug), context()))

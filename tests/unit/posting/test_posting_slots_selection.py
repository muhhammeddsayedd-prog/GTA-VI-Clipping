"""Hourly slot maths (slot time, jitter) and the choice of clip per account in a slot (R9, R10, D18, D42)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from posting_helpers import (
    BOTH,
    LAUNCH_AT,
    T0,
    acct,
    hourly_clip,
    make_params,
    post,
    production_accounts,
    small_account,
    spike_clip,
    spread,
    staging_accounts,
)

from sixstars.posting.models import Account, Action, Lane, PostRecord, Reason
from sixstars.posting.selection import (
    empty_slots_alert,
    hourly_candidates,
    last_hourly_creator,
    pick_clip,
    plan_hourly_slot,
)
from sixstars.posting.slots import (
    check_slot,
    fire_at,
    jitter_offset_s,
    jitter_window,
    next_slot,
    slot_is_open,
    slot_start,
)

SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
IG, YT, TT = "instagram_main", "youtube_main", "tiktok_main"
SLOT = T0  # 12:00 UTC
NOW = SLOT + 2 * MINUTE


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)  # type: ignore[arg-type, misc]


def chosen(plan: object) -> dict[str, str | None]:
    return {d.account_id: d.clip_id for d in plan.decisions}  # type: ignore[attr-defined]


def codes(plan: object) -> dict[str, Reason]:
    return {d.account_id: d.code for d in plan.decisions}  # type: ignore[attr-defined]


def prod() -> list[Account]:
    return list(production_accounts().values())


# ── slot time ─────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("now", "slot", "following"),
    [
        (utc(2026, 11, 20, 12, 0, 0), utc(2026, 11, 20, 12), utc(2026, 11, 20, 13)),
        (utc(2026, 11, 20, 12, 0, 1), utc(2026, 11, 20, 12), utc(2026, 11, 20, 13)),
        (utc(2026, 11, 20, 12, 59, 59), utc(2026, 11, 20, 12), utc(2026, 11, 20, 13)),
        (utc(2026, 11, 20, 23, 59, 59), utc(2026, 11, 20, 23), utc(2026, 11, 21, 0)),  # midnight
        (utc(2026, 11, 21, 0, 0, 0), utc(2026, 11, 21, 0), utc(2026, 11, 21, 1)),
        (utc(2026, 11, 30, 23, 30, 0), utc(2026, 11, 30, 23), utc(2026, 12, 1, 0)),  # month end
        (utc(2026, 12, 31, 23, 59, 59), utc(2026, 12, 31, 23), utc(2027, 1, 1, 0)),  # year end
        (utc(2028, 2, 28, 23, 10, 0), utc(2028, 2, 28, 23), utc(2028, 2, 29, 0)),  # leap day
    ],
)
def test_slot_start_and_next_slot(now: datetime, slot: datetime, following: datetime) -> None:
    assert slot_start(now) == slot
    assert next_slot(now) == following


def test_slots_are_utc_hours_whatever_zone_the_input_has() -> None:
    india = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 11, 20, 17, 45, tzinfo=india)  # 12:15 UTC

    assert slot_start(local) == utc(2026, 11, 20, 12)
    assert slot_start(local).tzinfo is UTC


def test_slot_maths_refuses_naive_times() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        slot_start(datetime(2026, 11, 20, 12, 0))


@pytest.mark.parametrize("bad", [SLOT + SECOND, SLOT + MINUTE, SLOT + timedelta(microseconds=1)])
def test_check_slot_refuses_a_time_off_the_hour(bad: datetime) -> None:
    with pytest.raises(ValueError, match="on the hour"):
        check_slot(bad)


@pytest.mark.parametrize(
    ("offset_s", "is_open"), [(-1, False), (0, True), (1, True), (3599, True), (3600, False)]
)
def test_slot_is_open_for_its_own_hour(offset_s: int, is_open: bool) -> None:
    assert slot_is_open(SLOT, SLOT + timedelta(seconds=offset_s)) is is_open


# ── jitter ────────────────────────────────────────────────────────────────────────────────────────────────
def test_jitter_stays_inside_zero_and_the_maximum_and_uses_the_range() -> None:
    offsets = [jitter_offset_s(SLOT + HOUR * i, 300) for i in range(2000)]

    assert min(offsets) >= 0
    assert max(offsets) <= 300
    assert len(set(offsets)) > 200  # spread over the window, not a constant
    assert min(offsets) < 15 and max(offsets) > 285


def test_jitter_is_the_same_every_time_for_one_slot() -> None:
    assert jitter_offset_s(SLOT, 300) == jitter_offset_s(SLOT, 300)
    assert jitter_offset_s(SLOT, 300, "a") == jitter_offset_s(SLOT.astimezone(timezone(HOUR)), 300, "a")
    assert fire_at(SLOT, 300) == SLOT + timedelta(seconds=jitter_offset_s(SLOT, 300))


def test_jitter_differs_by_seed() -> None:
    assert len({jitter_offset_s(SLOT, 300, f"seed-{i}") for i in range(50)}) > 20


@pytest.mark.parametrize("jitter_max_s", [0, 1, 2, 300, 3599])
def test_jitter_bounds_for_several_maxima(jitter_max_s: int) -> None:
    offsets = {jitter_offset_s(SLOT + HOUR * i, jitter_max_s) for i in range(400)}

    assert offsets <= set(range(jitter_max_s + 1))
    if jitter_max_s in (1, 2):
        assert offsets == set(range(jitter_max_s + 1))  # both ends of the window are reachable
    start, end = jitter_window(SLOT, jitter_max_s)
    assert (start, end) == (SLOT, SLOT + timedelta(seconds=jitter_max_s))
    assert start <= fire_at(SLOT, jitter_max_s) <= end


def test_jitter_refuses_a_slot_off_the_hour() -> None:
    with pytest.raises(ValueError, match="on the hour"):
        jitter_offset_s(SLOT + MINUTE, 300)


# ── the hourly queue ──────────────────────────────────────────────────────────────────────────────────────
def test_the_queue_is_ranked_best_first_with_stable_ties() -> None:
    clips = [
        hourly_clip("low", rank=2.0),
        hourly_clip("tie-b", rank=5.0),
        hourly_clip("tie-a", rank=5.0),
        hourly_clip("tie-older", rank=5.0, approved_at=T0 - 5 * HOUR),
        hourly_clip("top", rank=9.5),
    ]
    assert [c.id for c in hourly_candidates(clips, T0, make_params())] == [
        "top",
        "tie-older",
        "tie-a",
        "tie-b",
        "low",
    ]


@pytest.mark.parametrize(
    ("changes", "kept"),
    [
        ({}, True),
        ({"status": "rejected"}, False),
        ({"status": "posted"}, False),
        ({"status": "expired"}, False),
        ({"status": "rendering"}, False),
        ({"lane": Lane.SPIKE}, False),
        ({"approved_at": None}, False),
        ({"approved_at": T0 - timedelta(hours=72)}, True),  # exactly posting.max_clip_age_h is still fine
        ({"approved_at": T0 - timedelta(hours=72, seconds=1)}, False),
        ({"approved_at": T0 - timedelta(hours=71, minutes=59)}, True),
    ],
)
def test_what_is_in_the_hourly_queue(changes: dict[str, object], kept: bool) -> None:
    assert bool(hourly_candidates([hourly_clip(**changes)], T0, make_params())) is kept


def test_the_maximum_clip_age_comes_from_settings() -> None:
    clip = hourly_clip(approved_at=T0 - 10 * HOUR)

    assert hourly_candidates([clip], T0, make_params(max_clip_age_h=10))
    assert not hourly_candidates([clip], T0, make_params(max_clip_age_h=9.5))


def test_last_hourly_creator_ignores_spikes_other_accounts_and_dry_rows() -> None:
    history = [
        post(IG, T0 - 3 * HOUR, creator_id="older"),
        post(IG, T0 - 2 * HOUR, creator_id="latest-hourly"),
        post(IG, T0 - HOUR, Lane.SPIKE, creator_id="spike-creator"),
        post(YT, T0 - MINUTE, creator_id="other-account"),
        post(IG, T0 - MINUTE, status="skipped", dry_run=True, creator_id="dry-creator"),
    ]
    assert last_hourly_creator(acct(IG), history, dry=False) == "latest-hourly"
    assert last_hourly_creator(acct(IG), history, dry=True) == "dry-creator"
    assert last_hourly_creator(acct(TT), history, dry=False) is None


def test_pick_clip_takes_the_first_clip_the_account_can_have() -> None:
    queue = [
        hourly_clip("a", 9.0, "creator-a"),
        hourly_clip("b", 8.0, "creator-a"),
        hourly_clip("c", 7.0, "creator-b"),
    ]
    history = [post(IG, T0 - HOUR, creator_id="creator-a")]

    picked = pick_clip(acct(IG), queue, history, dry=False)

    assert picked is not None and picked.id == "c"
    assert pick_clip(acct(IG), queue[:2], history, dry=False) is None
    assert pick_clip(acct(IG), [], [], dry=False) is None


# ── one slot across accounts ──────────────────────────────────────────────────────────────────────────────
def test_d18_every_hourly_account_gets_the_same_top_clip() -> None:
    queue = [hourly_clip("second", 6.0, "creator-b"), hourly_clip("top", 9.0, "creator-a")]

    plan = plan_hourly_slot(prod(), [], queue, SLOT, NOW, make_params())

    assert chosen(plan) == {IG: "top", TT: "top", YT: "top", "tiktok_live": None}
    assert codes(plan) == {
        IG: Reason.OK,
        TT: Reason.OK,
        YT: Reason.OK,
        "tiktok_live": Reason.ACCOUNT_DISABLED,
    }
    assert [d.account_id for d in plan.posts] == [IG, TT, YT]
    assert all(d.may_send and d.slot_at == SLOT and d.lane is Lane.HOURLY for d in plan.posts)
    assert (plan.slot_at, plan.slot_empty, plan.fallback_used) == (SLOT, False, False)


def test_tiktok_main_sits_out_the_hours_it_does_not_post_in() -> None:
    slot = utc(2026, 11, 20, 13)  # 13 is not one of TikTok's hours

    plan = plan_hourly_slot(prod(), [], [hourly_clip(at=slot)], slot, slot + MINUTE, make_params())

    assert codes(plan)[TT] is Reason.OUTSIDE_HOURLY_HOURS
    assert chosen(plan) == {IG: "h1", YT: "h1", TT: None, "tiktok_live": None}


def test_r9_an_account_does_not_post_the_same_creator_twice_in_a_row() -> None:
    """docs/19 case 10."""
    queue = [hourly_clip("a-top", 9.0, "creator-a"), hourly_clip("b-next", 6.0, "creator-b")]
    history = [post(IG, SLOT - HOUR, creator_id="creator-a", slot_at=SLOT - HOUR)]

    plan = plan_hourly_slot(prod(), history, queue, SLOT, NOW, make_params())

    assert chosen(plan) == {IG: "b-next", TT: "a-top", YT: "a-top", "tiktok_live": None}


def test_r9_looks_only_at_the_previous_hourly_post() -> None:
    queue = [hourly_clip("a-top", 9.0, "creator-a")]
    history = [
        post(IG, SLOT - 2 * HOUR, creator_id="creator-a"),
        post(IG, SLOT - HOUR, creator_id="creator-b"),
        post(
            IG, SLOT - 40 * MINUTE, Lane.SPIKE, creator_id="creator-a"
        ),  # a spike post is not an hourly post
    ]
    plan = plan_hourly_slot([acct(IG)], history, queue, SLOT, NOW, make_params())

    assert chosen(plan) == {IG: "a-top"}


def test_r9_with_only_same_creator_clips_the_account_skips_the_slot() -> None:
    queue = [hourly_clip("a1", 9.0, "creator-a"), hourly_clip("a2", 6.0, "creator-a")]
    history = [post(IG, SLOT - HOUR, creator_id="creator-a")]

    plan = plan_hourly_slot([acct(IG), acct(YT)], history, queue, SLOT, NOW, make_params())

    assert codes(plan) == {IG: Reason.NO_ELIGIBLE_CLIP, YT: Reason.OK}
    assert chosen(plan) == {IG: None, YT: "a1"}
    assert not plan.slot_empty  # YouTube posted


@pytest.mark.parametrize("status", ["published", "pending", "failed", "removed"])
def test_no_clip_is_posted_twice_to_one_account(status: str) -> None:
    queue = [hourly_clip("top", 9.0, "creator-a"), hourly_clip("next", 6.0, "creator-b")]
    history = [post(IG, SLOT - 3 * HOUR, clip_id="top", status=status, creator_id="creator-z")]

    plan = plan_hourly_slot([acct(IG), acct(YT)], history, queue, SLOT, NOW, make_params())

    assert chosen(plan) == {IG: "next", YT: "top"}


def test_a_clip_that_is_everywhere_already_leaves_the_slot_empty() -> None:
    queue = [hourly_clip("top", 9.0)]
    history = [post(IG, SLOT - 3 * HOUR, clip_id="top"), post(YT, SLOT - 3 * HOUR, clip_id="top")]

    plan = plan_hourly_slot([acct(IG), acct(YT)], history, queue, SLOT, NOW, make_params())

    assert codes(plan) == {IG: Reason.NO_ELIGIBLE_CLIP, YT: Reason.NO_ELIGIBLE_CLIP}
    assert plan.slot_empty
    assert plan.posts == ()


def test_r10_an_empty_queue_skips_the_slot_and_reports_it() -> None:
    plan = plan_hourly_slot(prod(), [], [], SLOT, NOW, make_params())

    assert codes(plan) == {
        IG: Reason.QUEUE_EMPTY,
        TT: Reason.QUEUE_EMPTY,
        YT: Reason.QUEUE_EMPTY,
        "tiktok_live": Reason.ACCOUNT_DISABLED,
    }
    assert plan.slot_empty
    assert plan.posts == ()
    assert all(d.action is Action.SKIP for d in plan.decisions)


def test_r10_a_queue_of_unusable_clips_is_an_empty_queue() -> None:
    queue = [
        hourly_clip("expired", approved_at=T0 - timedelta(hours=73)),
        hourly_clip("rejected", status="rejected"),
        spike_clip("spike"),
    ]
    plan = plan_hourly_slot([acct(IG)], [], queue, SLOT, NOW, make_params())

    assert codes(plan) == {IG: Reason.QUEUE_EMPTY}
    assert plan.slot_empty


def test_r10_an_empty_queue_falls_back_to_the_best_compilation() -> None:
    fallback = [hourly_clip("comp-low", 3.0, "comp"), hourly_clip("comp-best", 4.0, "comp")]

    plan = plan_hourly_slot(prod(), [], [], SLOT, NOW, make_params(), fallback=fallback)

    assert chosen(plan) == {IG: "comp-best", TT: "comp-best", YT: "comp-best", "tiktok_live": None}
    assert (plan.slot_empty, plan.fallback_used) == (False, True)


def test_r10_the_fallback_is_used_only_when_the_queue_has_nothing_for_the_account() -> None:
    queue = [hourly_clip("regular", 1.0, "creator-a")]
    fallback = [hourly_clip("comp", 99.0, "comp")]
    history = [post(IG, SLOT - 3 * HOUR, clip_id="regular", creator_id="creator-z")]

    plan = plan_hourly_slot([acct(IG), acct(YT)], history, queue, SLOT, NOW, make_params(), fallback=fallback)

    assert chosen(plan) == {IG: "comp", YT: "regular"}
    assert plan.fallback_used


def test_r10_a_compilation_is_not_posted_twice_either() -> None:
    fallback = [hourly_clip("comp", 4.0, "comp")]
    history = [post(IG, SLOT - 5 * HOUR, clip_id="comp")]

    plan = plan_hourly_slot([acct(IG)], history, [], SLOT, NOW, make_params(), fallback=fallback)

    assert codes(plan) == {IG: Reason.NO_ELIGIBLE_CLIP}
    assert plan.slot_empty


def test_a_slot_where_no_account_was_free_is_not_an_empty_queue() -> None:
    blocked = [acct(IG, paused_until=NOW + HOUR), acct(YT, enabled=False)]

    plan = plan_hourly_slot(blocked, [], [], SLOT, NOW, make_params())

    assert codes(plan) == {IG: Reason.ACCOUNT_PAUSED, YT: Reason.ACCOUNT_DISABLED}
    assert not plan.slot_empty


@pytest.mark.parametrize(
    ("recent", "alert"),
    [
        ([], False),
        ([True], False),
        ([True, True], True),
        ([False, True, True], True),
        ([True, False], False),
        ([True, True, False], False),
        ([False, True], False),
        ([True, False, True], False),
    ],
)
def test_r10_alert_after_two_empty_slots_in_a_row(recent: list[bool], alert: bool) -> None:
    assert empty_slots_alert(recent) is alert


def test_r2_spike_at_the_hourly_slot_spike_posts_and_hourly_waits() -> None:
    """docs/19 case 1."""
    spikes = [spike_clip(at=NOW)]

    plan = plan_hourly_slot(prod(), [], [hourly_clip()], SLOT, NOW, make_params(), spike_clips=spikes)

    assert codes(plan) == {
        IG: Reason.SPIKE_PENDING,
        YT: Reason.SPIKE_PENDING,
        TT: Reason.OK,  # production TikTok takes no spike posts, so its slot goes ahead
        "tiktok_live": Reason.ACCOUNT_DISABLED,
    }
    assert chosen(plan)[TT] == "h1"


def test_r2_a_stale_spike_does_not_hold_the_slot() -> None:
    stale = [spike_clip(spike_at=NOW - HOUR)]

    plan = plan_hourly_slot([acct(IG)], [], [hourly_clip()], SLOT, NOW, make_params(), spike_clips=stale)

    assert codes(plan) == {IG: Reason.OK}


def test_r3_a_spike_20_minutes_before_the_slot_skips_it_on_that_account_only() -> None:
    """docs/19 case 3."""
    history = [post(IG, SLOT - 20 * MINUTE, Lane.SPIKE, clip_id="s-old")]

    plan = plan_hourly_slot(prod(), history, [hourly_clip()], SLOT, NOW, make_params())

    assert codes(plan)[IG] is Reason.SPIKE_RECENT
    assert chosen(plan) == {IG: None, TT: "h1", YT: "h1", "tiktok_live": None}


def test_r8_tiktok_at_14_of_14_is_skipped_and_the_others_post() -> None:
    """docs/19 case 6."""
    history = spread(TT, 14, SLOT - HOUR)

    plan = plan_hourly_slot(prod(), history, [hourly_clip()], SLOT, NOW, make_params())

    assert codes(plan)[TT] is Reason.AT_CAP
    assert chosen(plan) == {IG: "h1", YT: "h1", TT: None, "tiktok_live": None}


def test_the_reserve_is_protected_in_a_slot() -> None:
    """docs/19 case 7."""
    accounts = [small_account(cap=5, reserve=2), acct(YT)]
    history = spread(IG, 3, SLOT - 2 * HOUR)

    plan = plan_hourly_slot(accounts, history, [hourly_clip()], SLOT, NOW, make_params())

    assert codes(plan) == {IG: Reason.RESERVED_FOR_SPIKE, YT: Reason.OK}


def test_r11_emergency_stop_posts_nothing_in_a_slot() -> None:
    """docs/19 case 8."""
    plan = plan_hourly_slot(prod(), [], [hourly_clip()], SLOT, NOW, make_params(emergency_stop=True))

    assert set(codes(plan).values()) == {Reason.EMERGENCY_STOP}
    assert plan.posts == ()
    assert not plan.slot_empty


@pytest.mark.parametrize(
    ("dry_run", "now", "code"),
    [
        (True, NOW, Reason.DRY_RUN_SETTING),
        (False, LAUNCH_AT - 30 * MINUTE, Reason.BEFORE_LAUNCH),
        (True, LAUNCH_AT - 30 * MINUTE, Reason.DRY_RUN_SETTING),
    ],
)
def test_dry_run_plans_the_slot_but_nothing_may_be_sent(dry_run: bool, now: datetime, code: Reason) -> None:
    """docs/19 case 9."""
    slot = slot_start(now)
    accounts = [acct(IG), acct(YT)]

    plan = plan_hourly_slot(accounts, [], [hourly_clip(at=now)], slot, now, make_params(dry_run=dry_run))

    assert {(d.action, d.code, d.dry_run, d.clip_id) for d in plan.decisions} == {
        (Action.DRY_RUN, code, True, "h1")
    }
    assert len(plan.posts) == 2
    assert not any(d.may_send for d in plan.decisions)


def test_at_launch_time_the_first_real_slot_posts() -> None:
    plan = plan_hourly_slot([acct(IG)], [], [hourly_clip(at=LAUNCH_AT)], LAUNCH_AT, LAUNCH_AT, make_params())
    assert [(d.action, d.code) for d in plan.decisions] == [(Action.ALLOW, Reason.OK)]


def test_dry_rows_from_before_launch_do_not_block_the_real_post_of_the_same_clip() -> None:
    history = [
        post(IG, SLOT - 2 * HOUR, clip_id="h1", status="skipped", dry_run=True, creator_id="creator-a")
    ]

    live = plan_hourly_slot([acct(IG)], history, [hourly_clip("h1")], SLOT, NOW, make_params())
    dry = plan_hourly_slot([acct(IG)], history, [hourly_clip("h1")], SLOT, NOW, make_params(dry_run=True))

    assert chosen(live) == {IG: "h1"}
    assert codes(dry) == {IG: Reason.NO_ELIGIBLE_CLIP}


def test_running_the_same_slot_twice_posts_once() -> None:
    accounts = [acct(IG), acct(YT)]
    queue = [hourly_clip("a", 9.0, "creator-a"), hourly_clip("b", 8.0, "creator-b")]
    first = plan_hourly_slot(accounts, [], queue, SLOT, NOW, make_params())
    written = [
        post(d.account_id, NOW, clip_id=d.clip_id, status="pending", slot_at=SLOT, creator_id="creator-a")
        for d in first.posts
    ]

    again = plan_hourly_slot(accounts, written, queue, SLOT, NOW + 30 * SECOND, make_params())

    assert codes(again) == {IG: Reason.SLOT_ALREADY_FILLED, YT: Reason.SLOT_ALREADY_FILLED}
    assert again.posts == ()
    assert not again.slot_empty


def test_d42_two_tiktok_accounts_get_different_clips() -> None:
    accounts = production_accounts()
    accounts["tiktok_live"] = replace(
        accounts["tiktok_live"], enabled=True, lanes=BOTH, upload_post_user="live-profile", spike_reserve=0
    )
    queue = [hourly_clip("top", 9.0, "creator-a"), hourly_clip("next", 6.0, "creator-b")]

    plan = plan_hourly_slot(list(accounts.values()), [], queue, SLOT, NOW, make_params())

    assert chosen(plan) == {IG: "top", YT: "top", "tiktok_live": "top", TT: "next"}


def test_d42_with_one_clip_the_second_tiktok_account_skips() -> None:
    accounts = [
        acct("tiktok_live", enabled=True, lanes=BOTH, upload_post_user="live-profile", spike_reserve=0),
        acct(TT),
    ]
    plan = plan_hourly_slot(accounts, [], [hourly_clip("only")], SLOT, NOW, make_params())

    assert chosen(plan) == {"tiktok_live": "only", TT: None}
    assert codes(plan)[TT] is Reason.NO_ELIGIBLE_CLIP


def test_two_enabled_rows_on_one_real_account_are_refused_in_a_slot() -> None:
    accounts = production_accounts()
    accounts["tiktok_live"] = replace(accounts["tiktok_live"], enabled=True, lanes=BOTH, spike_reserve=0)

    plan = plan_hourly_slot(list(accounts.values()), [], [hourly_clip()], SLOT, NOW, make_params())

    assert codes(plan) == {
        IG: Reason.OK,
        YT: Reason.OK,
        TT: Reason.DUPLICATE_CHANNEL,
        "tiktok_live": Reason.DUPLICATE_CHANNEL,
    }
    assert chosen(plan) == {IG: "h1", YT: "h1", TT: None, "tiktok_live": None}


def test_staging_slot_posts_privately_and_never_to_instagram() -> None:
    accounts = list(staging_accounts().values())

    plan = plan_hourly_slot(accounts, [], [hourly_clip()], SLOT, NOW, make_params("staging"))

    assert codes(plan) == {
        TT: Reason.OK,
        YT: Reason.OK,
        IG: Reason.STAGING_INSTAGRAM_NOT_ALLOWED,
        "tiktok_live": Reason.ACCOUNT_DISABLED,
    }


def test_staging_instagram_posts_only_with_the_explicit_setting() -> None:
    accounts = list(staging_accounts().values())
    params = make_params("staging", staging_allow_instagram=True)

    plan = plan_hourly_slot(accounts, [], [hourly_clip()], SLOT, NOW, params)

    assert codes(plan)[IG] is Reason.OK


def test_the_plan_does_not_depend_on_the_order_of_the_input() -> None:
    queue = [
        hourly_clip("a", 9.0, "creator-a"),
        hourly_clip("b", 6.0, "creator-b"),
        hourly_clip("c", 6.0, "creator-c"),
    ]
    history = [post(IG, SLOT - HOUR, creator_id="creator-a"), post(YT, SLOT - 3 * HOUR, clip_id="a")]

    forward = plan_hourly_slot(prod(), history, queue, SLOT, NOW, make_params())
    backward = plan_hourly_slot(prod()[::-1], history[::-1], queue[::-1], SLOT, NOW, make_params())

    assert forward == backward
    assert chosen(forward) == {IG: "b", YT: "b", TT: "a", "tiktok_live": None}


def test_a_slot_asked_for_too_early_or_too_late_posts_nothing() -> None:
    early = plan_hourly_slot([acct(IG)], [], [hourly_clip()], SLOT, SLOT - SECOND, make_params())
    late = plan_hourly_slot([acct(IG)], [], [hourly_clip()], SLOT, SLOT + HOUR, make_params())

    assert [(d.action, d.code, d.defer_until) for d in early.decisions] == [
        (Action.DEFER, Reason.SLOT_NOT_DUE, SLOT)
    ]
    assert codes(late) == {IG: Reason.SLOT_EXPIRED}
    assert early.posts == late.posts == ()


@pytest.mark.parametrize(
    ("youtube_cap", "youtube_reserve", "youtube_posts"),
    [
        (40, 8, 22),  # the old seed: capped at Upload-Post's 30, and 8 of those are kept for spikes
        (28, 4, 24),  # the seeded values (migration 0005): every hourly slot posts and 4 stay free for spikes
        (30, 6, 24),
        (28, 8, 20),
    ],
)
def test_a_day_of_hourly_slots_on_production_accounts(
    youtube_cap: int, youtube_reserve: int, youtube_posts: int
) -> None:
    """24 slots from midnight to midnight: TikTok posts in its 14 hours, Instagram in all 24."""
    accounts = [
        replace(a, cap_24h=youtube_cap, spike_reserve=youtube_reserve) if a.id == YT else a for a in prod()
    ]
    params = make_params()
    history: list[PostRecord] = []
    posted: dict[str, int] = {IG: 0, YT: 0, TT: 0, "tiktok_live": 0}
    for hour in range(24):
        slot = utc(2026, 11, 21, hour)
        now = slot + timedelta(seconds=jitter_offset_s(slot, params.jitter_max_s))
        creator = f"creator-{hour}"  # a new creator every hour, so R9 never skips
        queue = [hourly_clip(f"clip-{hour}", 5.0, creator, at=now)]
        plan = plan_hourly_slot(accounts, history, queue, slot, now, params)
        for d in plan.posts:
            posted[d.account_id] += 1
            history.append(post(d.account_id, now, clip_id=d.clip_id, slot_at=slot, creator_id=creator))

    assert posted == {IG: 24, YT: youtube_posts, TT: 14, "tiktok_live": 0}

"""Precedence rules R1-R11 (docs/11 §4, D41) for one account and for the spike lane across accounts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from posting_helpers import (
    BOTH,
    LAUNCH_AT,
    T0,
    TIKTOK_HOURS,
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

from sixstars.posting.models import Action, Lane, Reason
from sixstars.posting.precedence import (
    HOLD_AGE_MARGIN_S,
    SPIKE_DEMOTION_BOOST,
    decide_hourly_account,
    decide_spike,
    demoted_rank,
    has_pending_spike,
    hold_release_at,
    plan_spikes,
)

SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
IG = "instagram_main"
YT = "youtube_main"
SLOT = T0  # 12:00 UTC, an hour in which every seeded account posts
NOW = SLOT + 2 * MINUTE  # after the jitter


def codes(decisions: object) -> dict[str, Reason]:
    return {d.account_id: d.code for d in decisions}  # type: ignore[attr-defined]


# ── spike lane, one account ───────────────────────────────────────────────────────────────────────────────
def test_r1_a_fresh_spike_posts_now() -> None:
    decision = decide_spike(spike_clip(), acct(IG), [], T0, make_params())

    assert (decision.action, decision.code) == (Action.ALLOW, Reason.OK)
    assert (decision.clip_id, decision.account_id, decision.lane) == ("s1", IG, Lane.SPIKE)
    assert decision.may_send


@pytest.mark.parametrize(
    ("last_post_ago_s", "approved_ago_s", "action", "code", "wait_s"),
    [
        (600, 0, Action.DEFER, Reason.MIN_GAP_WAIT, 300),  # docs/19 case 2: 10 min after a post, wait 5
        (899, 0, Action.DEFER, Reason.MIN_GAP_WAIT, 1),
        (900, 0, Action.ALLOW, Reason.OK, None),  # gap exactly met
        (901, 0, Action.ALLOW, Reason.OK, None),
        (0, 0, Action.DEFER, Reason.MIN_GAP_WAIT, 900),  # the longest wait that is still allowed
        (0, 1, Action.SKIP, Reason.SPIKE_WAIT_EXCEEDED, None),  # 1 s past posting.spike_max_wait_s
        (240, 600, Action.SKIP, Reason.SPIKE_WAIT_EXCEEDED, None),  # waited 10 min, gap needs 11 more
        (600, 600, Action.DEFER, Reason.MIN_GAP_WAIT, 300),  # waited 10 min, gap ends exactly at 15
        (599, 600, Action.SKIP, Reason.SPIKE_WAIT_EXCEEDED, None),
    ],
)
def test_r4_a_spike_waits_for_the_gap_up_to_the_maximum_wait(
    last_post_ago_s: int, approved_ago_s: int, action: Action, code: Reason, wait_s: int | None
) -> None:
    clip = spike_clip(approved_at=T0 - timedelta(seconds=approved_ago_s), spike_at=T0 - 15 * MINUTE)
    history = [post(IG, T0 - timedelta(seconds=last_post_ago_s))]

    decision = decide_spike(clip, acct(IG), history, T0, make_params())

    assert (decision.action, decision.code) == (action, code)
    assert decision.defer_until == (None if wait_s is None else T0 + timedelta(seconds=wait_s))
    assert not decision.may_send or action is Action.ALLOW


def test_r4_after_the_wait_the_spike_posts() -> None:
    clip = spike_clip()
    history = [post(IG, T0 - 10 * MINUTE)]
    first = decide_spike(clip, acct(IG), history, T0, make_params())
    assert first.defer_until == T0 + 5 * MINUTE

    assert (
        decide_spike(clip, acct(IG), history, first.defer_until - SECOND, make_params()).action
        is Action.DEFER
    )
    assert decide_spike(clip, acct(IG), history, first.defer_until, make_params()).action is Action.ALLOW


def test_r4_the_wait_follows_the_larger_gap_of_account_and_setting() -> None:
    history = [post(IG, T0 - 10 * MINUTE)]
    wide = decide_spike(spike_clip(), acct(IG, min_gap_s=1200), history, T0, make_params())
    narrow = decide_spike(spike_clip(), acct(IG, min_gap_s=60), history, T0, make_params(min_gap_s=600))

    assert wide.defer_until == T0 + 10 * MINUTE
    assert narrow.action is Action.ALLOW


@pytest.mark.parametrize(
    ("max_wait_s", "action"),
    [(0, Action.SKIP), (299, Action.SKIP), (300, Action.DEFER), (301, Action.DEFER)],
)
def test_r4_the_maximum_wait_comes_from_settings(max_wait_s: int, action: Action) -> None:
    history = [post(IG, T0 - 10 * MINUTE)]  # the gap ends in 300 s
    decision = decide_spike(spike_clip(), acct(IG), history, T0, make_params(spike_max_wait_s=max_wait_s))
    assert decision.action is action


@pytest.mark.parametrize(
    ("spike_age_s", "action", "code"),
    [
        (240, Action.ALLOW, Reason.OK),
        (1799, Action.ALLOW, Reason.OK),
        (1800, Action.ALLOW, Reason.OK),  # "older than": exactly the limit is still a spike
        (1801, Action.SKIP, Reason.SPIKE_TOO_OLD),  # docs/19 case 5
        (7200, Action.SKIP, Reason.SPIKE_TOO_OLD),
    ],
)
def test_r7_a_spike_older_than_the_maximum_age_leaves_the_spike_lane(
    spike_age_s: int, action: Action, code: Reason
) -> None:
    clip = spike_clip(spike_at=T0 - timedelta(seconds=spike_age_s))
    decision = decide_spike(clip, acct(IG), [], T0, make_params())
    assert (decision.action, decision.code) == (action, code)


@pytest.mark.parametrize(
    ("spike_age_s", "action", "code"),
    [(1500, Action.DEFER, Reason.MIN_GAP_WAIT), (1501, Action.SKIP, Reason.SPIKE_TOO_OLD)],
)
def test_r7_a_spike_does_not_wait_for_a_gap_that_ends_after_it_is_too_old(
    spike_age_s: int, action: Action, code: Reason
) -> None:
    clip = spike_clip(spike_at=T0 - timedelta(seconds=spike_age_s))
    history = [post(IG, T0 - 10 * MINUTE)]  # the gap ends in 300 s

    decision = decide_spike(clip, acct(IG), history, T0, make_params())

    assert (decision.action, decision.code) == (action, code)


def test_r7_a_spike_without_a_spike_time_is_not_posted_by_the_spike_lane() -> None:
    decision = decide_spike(spike_clip(spike_at=None), acct(IG), [], T0, make_params())
    assert (decision.action, decision.code) == (Action.SKIP, Reason.SPIKE_TIME_MISSING)


@pytest.mark.parametrize(
    ("spike_ages_min", "action", "code"),
    [
        ((), Action.ALLOW, Reason.OK),
        ((20,), Action.ALLOW, Reason.OK),
        ((20, 40), Action.SKIP, Reason.SPIKE_HOURLY_LIMIT),  # docs/19 case 4: the third spike in an hour
        ((20, 60), Action.SKIP, Reason.SPIKE_HOURLY_LIMIT),  # exactly one hour old still counts
        ((20, 61), Action.ALLOW, Reason.OK),
        ((20, 40, 59), Action.SKIP, Reason.SPIKE_HOURLY_LIMIT),
    ],
)
def test_r6_at_most_two_spike_posts_per_account_per_hour(
    spike_ages_min: tuple[int, ...], action: Action, code: Reason
) -> None:
    history = [post(IG, T0 - age * MINUTE, Lane.SPIKE) for age in spike_ages_min]
    decision = decide_spike(spike_clip(), acct(IG), history, T0, make_params())
    assert (decision.action, decision.code) == (action, code)


def test_r6_hourly_posts_do_not_use_the_spike_allowance() -> None:
    history = [post(IG, T0 - 20 * MINUTE), post(IG, T0 - 40 * MINUTE), post(IG, T0 - 59 * MINUTE)]
    assert decide_spike(spike_clip(), acct(IG), history, T0, make_params()).action is Action.ALLOW


@pytest.mark.parametrize(
    ("used", "action", "code"),
    [
        (3, Action.ALLOW, Reason.OK),
        (4, Action.ALLOW, Reason.OK),
        (5, Action.SKIP, Reason.AT_CAP),
        (6, Action.SKIP, Reason.AT_CAP),
    ],
)
def test_r8_a_spike_is_skipped_only_when_the_account_is_at_its_cap(
    used: int, action: Action, code: Reason
) -> None:
    account = small_account(cap=5, reserve=2)
    history = spread(IG, used, T0 - 2 * HOUR)

    decision = decide_spike(spike_clip(), account, history, T0, make_params())

    assert (decision.action, decision.code) == (action, code)


def test_a_spike_may_use_the_reserve_and_ordinary_capacity() -> None:
    account = small_account(cap=5, reserve=1)
    spikes = [post(IG, T0 - 5 * HOUR, Lane.SPIKE), post(IG, T0 - 4 * HOUR, Lane.SPIKE)]

    assert decide_spike(spike_clip(), account, spikes, T0, make_params()).action is Action.ALLOW


@pytest.mark.parametrize(
    ("account_id", "code"),
    [("tiktok_main", Reason.LANE_NOT_ALLOWED), ("tiktok_live", Reason.ACCOUNT_DISABLED)],
)
def test_production_tiktok_takes_no_spike_posts(account_id: str, code: Reason) -> None:
    decision = decide_spike(spike_clip(), production_accounts()[account_id], [], T0, make_params())
    assert (decision.action, decision.code) == (Action.SKIP, code)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"status": "rejected"}, Reason.CLIP_NOT_APPROVED),
        ({"status": "posted"}, Reason.CLIP_NOT_APPROVED),
        ({"status": "expired"}, Reason.CLIP_NOT_APPROVED),
        ({"status": "gating"}, Reason.CLIP_NOT_APPROVED),
        ({"lane": Lane.HOURLY}, Reason.CLIP_WRONG_LANE),
    ],
)
def test_only_approved_spike_clips_post_in_the_spike_lane(changes: dict[str, object], code: Reason) -> None:
    decision = decide_spike(spike_clip(**changes), acct(IG), [], T0, make_params())
    assert (decision.action, decision.code) == (Action.SKIP, code)


@pytest.mark.parametrize("status", ["published", "pending", "failed", "removed", "remove_failed"])
def test_a_clip_never_posts_twice_to_one_account(status: str) -> None:
    history = [post(IG, T0 - 2 * HOUR, Lane.SPIKE, clip_id="s1", status=status)]
    decision = decide_spike(spike_clip("s1"), acct(IG), history, T0, make_params())
    assert (decision.action, decision.code) == (Action.SKIP, Reason.ALREADY_POSTED)


def test_a_clip_already_on_one_account_can_still_go_to_another_platform() -> None:
    history = [post(IG, T0 - 2 * HOUR, Lane.SPIKE, clip_id="s1")]
    assert decide_spike(spike_clip("s1"), acct(YT), history, T0, make_params()).action is Action.ALLOW


def test_d42_a_clip_never_goes_to_two_accounts_of_one_platform() -> None:
    second_tiktok = acct("tiktok_live", enabled=True, upload_post_user="live-profile")
    history = [post("tiktok_main", T0 - 2 * HOUR, clip_id="s1")]

    decision = decide_spike(spike_clip("s1"), second_tiktok, history, T0, make_params())

    assert (decision.action, decision.code) == (Action.SKIP, Reason.POSTED_ON_SAME_PLATFORM)
    assert "tiktok_main" in decision.reason


def test_dry_run_rows_do_not_block_a_real_spike_but_do_block_a_dry_one() -> None:
    dry_rows = [post(IG, T0 - 5 * MINUTE, Lane.SPIKE, clip_id="s1", status="skipped", dry_run=True)]

    live = decide_spike(spike_clip("s1"), acct(IG), dry_rows, T0, make_params())
    dry = decide_spike(spike_clip("s1"), acct(IG), dry_rows, T0, make_params(dry_run=True))
    other_dry = decide_spike(spike_clip("s2"), acct(IG), dry_rows, T0, make_params(dry_run=True))

    assert (live.action, live.code) == (Action.ALLOW, Reason.OK)
    assert (dry.action, dry.code, dry.dry_run) == (Action.SKIP, Reason.ALREADY_POSTED, True)
    assert (other_dry.action, other_dry.code) == (
        Action.DEFER,
        Reason.MIN_GAP_WAIT,
    )  # the dry run keeps the gap
    assert other_dry.dry_run


@pytest.mark.parametrize(
    ("dry_run", "now", "action", "code"),
    [
        (True, T0, Action.DRY_RUN, Reason.DRY_RUN_SETTING),
        (False, LAUNCH_AT - SECOND, Action.DRY_RUN, Reason.BEFORE_LAUNCH),
        (False, LAUNCH_AT, Action.ALLOW, Reason.OK),
        (False, LAUNCH_AT + SECOND, Action.ALLOW, Reason.OK),
    ],
)
def test_spike_in_dry_mode_is_decided_but_never_sendable(
    dry_run: bool, now: datetime, action: Action, code: Reason
) -> None:
    decision = decide_spike(spike_clip(at=now), acct(IG), [], now, make_params(dry_run=dry_run))

    assert (decision.action, decision.code, decision.clip_id) == (action, code, "s1")
    assert decision.may_send is (action is Action.ALLOW)


def test_r11_emergency_stop_blocks_a_perfect_spike() -> None:
    decision = decide_spike(spike_clip(), acct(IG), [], T0, make_params(emergency_stop=True))
    assert (decision.action, decision.code, decision.clip_id) == (Action.SKIP, Reason.EMERGENCY_STOP, "s1")


def test_a_paused_account_is_skipped_not_waited_for() -> None:
    decision = decide_spike(spike_clip(), acct(IG, paused_until=T0 + MINUTE), [], T0, make_params())
    assert (decision.action, decision.code) == (Action.SKIP, Reason.ACCOUNT_PAUSED)


# ── hourly lane, one account ──────────────────────────────────────────────────────────────────────────────
def test_an_hourly_slot_is_allowed_on_a_free_account() -> None:
    decision = decide_hourly_account(acct(IG), [], SLOT, NOW, make_params())

    assert (decision.action, decision.code, decision.lane) == (Action.ALLOW, Reason.OK, Lane.HOURLY)
    assert decision.slot_at == SLOT
    assert decision.clip_id is None


@pytest.mark.parametrize(
    ("spike_before_slot_s", "code"),
    [
        (1200, Reason.SPIKE_RECENT),  # docs/19 case 3: spike 20 min before the slot
        (1799, Reason.SPIKE_RECENT),
        (1800, Reason.OK),  # "<": exactly 30 min before the slot no longer skips it
        (1801, Reason.OK),
        (901, Reason.SPIKE_RECENT),
        (0, Reason.SPIKE_RECENT),
        (-60, Reason.SPIKE_RECENT),  # the spike posted during the jitter, after the slot started
    ],
)
def test_r3_an_hourly_slot_is_skipped_after_a_recent_spike(spike_before_slot_s: int, code: Reason) -> None:
    history = [post(IG, SLOT - timedelta(seconds=spike_before_slot_s), Lane.SPIKE)]

    decision = decide_hourly_account(acct(IG), history, SLOT, NOW, make_params())

    assert decision.code is code
    assert decision.would_post is (code is Reason.OK)


@pytest.mark.parametrize(
    ("window_s", "code"), [(1200, Reason.OK), (1201, Reason.SPIKE_RECENT), (0, Reason.OK)]
)
def test_r3_the_window_comes_from_settings(window_s: int, code: Reason) -> None:
    history = [post(IG, SLOT - 20 * MINUTE, Lane.SPIKE)]
    decision = decide_hourly_account(
        acct(IG), history, SLOT, NOW, make_params(skip_hourly_after_spike_s=window_s)
    )
    assert decision.code is code


def test_r3_only_spike_posts_of_this_account_count() -> None:
    history = [
        post(YT, SLOT - 20 * MINUTE, Lane.SPIKE),
        post(IG, SLOT - 20 * MINUTE, Lane.HOURLY),
        post(IG, SLOT - 20 * MINUTE, Lane.SPIKE, status="skipped", dry_run=True),
    ]
    assert decide_hourly_account(acct(IG), history, SLOT, NOW, make_params()).code is Reason.OK


def test_r3_the_window_crosses_midnight() -> None:
    midnight = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
    spike_before = [post(IG, datetime(2026, 11, 20, 23, 45, tzinfo=UTC), Lane.SPIKE)]
    old_spike = [post(IG, datetime(2026, 11, 20, 23, 30, tzinfo=UTC), Lane.SPIKE)]

    assert (
        decide_hourly_account(acct(IG), spike_before, midnight, midnight + MINUTE, make_params()).code
        is Reason.SPIKE_RECENT
    )
    assert (
        decide_hourly_account(acct(IG), old_spike, midnight, midnight + MINUTE, make_params()).code
        is Reason.OK
    )


def test_r2_a_pending_spike_takes_the_slot() -> None:
    decision = decide_hourly_account(acct(IG), [], SLOT, NOW, make_params(), spike_pending=True)
    assert (decision.action, decision.code) == (Action.SKIP, Reason.SPIKE_PENDING)


@pytest.mark.parametrize("hour", range(24))
def test_tiktok_main_posts_hourly_only_in_its_best_hours(hour: int) -> None:
    slot = datetime(2026, 11, 20, hour, 0, tzinfo=UTC)

    decision = decide_hourly_account(
        production_accounts()["tiktok_main"], [], slot, slot + MINUTE, make_params()
    )

    if hour in TIKTOK_HOURS:
        assert (decision.action, decision.code) == (Action.ALLOW, Reason.OK)
    else:
        assert (decision.action, decision.code) == (Action.SKIP, Reason.OUTSIDE_HOURLY_HOURS)


def test_hours_roll_over_midnight() -> None:
    tiktok = production_accounts()["tiktok_main"]
    params = make_params()
    result = {}
    for slot in (
        datetime(2026, 11, 20, 23, 0, tzinfo=UTC),
        datetime(2026, 11, 21, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 21, 2, 0, tzinfo=UTC),
        datetime(2026, 11, 21, 3, 0, tzinfo=UTC),
    ):
        result[slot.hour] = decide_hourly_account(tiktok, [], slot, slot + MINUTE, params).would_post
    assert result == {23: True, 0: True, 2: True, 3: False}


@pytest.mark.parametrize(
    ("hours", "allowed"),
    [(None, True), (frozenset(), False), (frozenset({12}), True), (frozenset({11, 13}), False)],
)
def test_hour_list_null_means_all_and_empty_means_none(hours: frozenset[int] | None, allowed: bool) -> None:
    decision = decide_hourly_account(acct(IG, hourly_hours_utc=hours), [], SLOT, NOW, make_params())
    assert decision.would_post is allowed


@pytest.mark.parametrize(
    ("offset_s", "action", "code"),
    [
        (-1, Action.DEFER, Reason.SLOT_NOT_DUE),
        (0, Action.ALLOW, Reason.OK),
        (300, Action.ALLOW, Reason.OK),
        (3599, Action.ALLOW, Reason.OK),
        (3600, Action.SKIP, Reason.SLOT_EXPIRED),  # the next slot has started
        (7200, Action.SKIP, Reason.SLOT_EXPIRED),
    ],
)
def test_a_slot_is_only_served_inside_its_own_hour(offset_s: int, action: Action, code: Reason) -> None:
    decision = decide_hourly_account(acct(IG), [], SLOT, SLOT + timedelta(seconds=offset_s), make_params())

    assert (decision.action, decision.code) == (action, code)
    assert decision.defer_until == (SLOT if action is Action.DEFER else None)


def test_a_slot_must_be_exactly_on_the_hour() -> None:
    with pytest.raises(ValueError, match="on the hour"):
        decide_hourly_account(acct(IG), [], SLOT + MINUTE, NOW, make_params())


@pytest.mark.parametrize("status", ["pending", "published", "failed"])
def test_one_hourly_post_per_slot_per_account(status: str) -> None:
    history = [post(IG, SLOT + 10 * SECOND, status=status, slot_at=SLOT)]
    decision = decide_hourly_account(acct(IG), history, SLOT, NOW, make_params())
    assert (decision.action, decision.code) == (Action.SKIP, Reason.SLOT_ALREADY_FILLED)


def test_a_post_of_the_previous_slot_does_not_fill_this_one() -> None:
    history = [post(IG, SLOT - HOUR + MINUTE, slot_at=SLOT - HOUR)]
    assert decide_hourly_account(acct(IG), history, SLOT, NOW, make_params()).code is Reason.OK


@pytest.mark.parametrize(
    ("used", "code"),
    [(12, Reason.OK), (13, Reason.OK), (14, Reason.AT_CAP), (15, Reason.AT_CAP)],
)
def test_r8_tiktok_main_at_its_cap_is_skipped(used: int, code: Reason) -> None:
    """docs/19 case 6."""
    tiktok = production_accounts()["tiktok_main"]
    history = spread("tiktok_main", used, SLOT - HOUR)

    decision = decide_hourly_account(tiktok, history, SLOT, NOW, make_params())

    assert decision.code is code
    assert decision.would_post is (code is Reason.OK)


@pytest.mark.parametrize(
    ("hourly_used", "spikes_used", "code"),
    [
        (2, 0, Reason.OK),  # free 3 > reserve 2
        (3, 0, Reason.RESERVED_FOR_SPIKE),  # docs/19 case 7: the last 2 posts belong to spikes
        (4, 0, Reason.RESERVED_FOR_SPIKE),
        (5, 0, Reason.AT_CAP),
        (3, 1, Reason.RESERVED_FOR_SPIKE),  # free 1, reserve left 1
        (2, 2, Reason.OK),  # reserve used by spikes, 1 ordinary post free
        (3, 2, Reason.AT_CAP),
    ],
)
def test_the_hourly_lane_cannot_take_the_spike_reserve(
    hourly_used: int, spikes_used: int, code: Reason
) -> None:
    account = small_account(cap=5, reserve=2)
    history = spread(IG, hourly_used, SLOT - 2 * HOUR) + spread(IG, spikes_used, SLOT - 12 * HOUR, Lane.SPIKE)

    decision = decide_hourly_account(account, history, SLOT, NOW, make_params())

    assert decision.code is code
    assert decision.would_post is (code is Reason.OK)


@pytest.mark.parametrize(
    ("last_ago_s", "code"),
    [(1, Reason.MIN_GAP), (899, Reason.MIN_GAP), (900, Reason.OK), (901, Reason.OK)],
)
def test_the_hourly_lane_skips_instead_of_waiting_for_the_gap(last_ago_s: int, code: Reason) -> None:
    now = SLOT + 20 * MINUTE
    history = [post(IG, now - timedelta(seconds=last_ago_s), slot_at=None)]

    decision = decide_hourly_account(acct(IG), history, SLOT, now, make_params())

    assert decision.code is code
    assert decision.action is (Action.ALLOW if code is Reason.OK else Action.SKIP)
    assert decision.defer_until is None


@pytest.mark.parametrize(
    ("dry_run", "now", "action", "code"),
    [
        (True, NOW, Action.DRY_RUN, Reason.DRY_RUN_SETTING),
        (False, LAUNCH_AT - SECOND, Action.DRY_RUN, Reason.BEFORE_LAUNCH),  # 10:59:59, slot 10:00
        (False, LAUNCH_AT, Action.ALLOW, Reason.OK),  # 11:00:00, slot 11:00
        (False, LAUNCH_AT + SECOND, Action.ALLOW, Reason.OK),
    ],
)
def test_hourly_in_dry_mode_is_decided_but_never_sendable(
    dry_run: bool, now: datetime, action: Action, code: Reason
) -> None:
    slot = now.replace(minute=0, second=0)
    decision = decide_hourly_account(acct(IG), [], slot, now, make_params(dry_run=dry_run))

    assert (decision.action, decision.code) == (action, code)
    assert decision.may_send is (action is Action.ALLOW)


def test_hourly_dry_run_respects_caps_using_its_own_dry_rows() -> None:
    account = small_account(cap=3, reserve=0)
    dry_rows = [post(IG, SLOT - (i + 1) * HOUR, status="skipped", dry_run=True) for i in range(3)]

    dry = decide_hourly_account(account, dry_rows, SLOT, NOW, make_params(dry_run=True))
    live = decide_hourly_account(account, dry_rows, SLOT, NOW, make_params())

    assert (dry.action, dry.code, dry.dry_run) == (Action.SKIP, Reason.AT_CAP, True)
    assert (live.action, live.code) == (Action.ALLOW, Reason.OK)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"enabled": False}, Reason.ACCOUNT_DISABLED),
        ({"lanes": frozenset({Lane.SPIKE})}, Reason.LANE_NOT_ALLOWED),
        ({"paused_until": NOW + MINUTE}, Reason.ACCOUNT_PAUSED),
    ],
)
def test_hourly_gate_checks(changes: dict[str, object], code: Reason) -> None:
    decision = decide_hourly_account(acct(IG, **changes), [], SLOT, NOW, make_params())
    assert (decision.action, decision.code, decision.slot_at) == (Action.SKIP, code, SLOT)


def test_r11_emergency_stop_blocks_a_perfect_hourly_slot() -> None:
    decision = decide_hourly_account(acct(IG), [], SLOT, NOW, make_params(emergency_stop=True, dry_run=True))
    assert (decision.action, decision.code) == (Action.SKIP, Reason.EMERGENCY_STOP)


# ── two lanes competing ───────────────────────────────────────────────────────────────────────────────────
def test_two_lanes_competing_for_the_last_post_without_reserve_spike_wins() -> None:
    account = small_account(cap=5, reserve=0)
    history = spread(IG, 4, SLOT - HOUR)
    clip = spike_clip(at=NOW)
    params = make_params()

    pending = has_pending_spike(account, [clip], history, NOW, params)
    hourly = decide_hourly_account(account, history, SLOT, NOW, params, spike_pending=pending)
    spike = decide_spike(clip, account, history, NOW, params)

    assert pending
    assert (hourly.action, hourly.code) == (Action.SKIP, Reason.SPIKE_PENDING)
    assert (spike.action, spike.code) == (Action.ALLOW, Reason.OK)


def test_two_lanes_competing_for_the_last_post_with_reserve_hourly_is_refused_even_without_a_spike() -> None:
    account = small_account(cap=5, reserve=1)
    history = spread(IG, 4, SLOT - HOUR)
    params = make_params()

    hourly = decide_hourly_account(account, history, SLOT, NOW, params)
    spike = decide_spike(spike_clip(at=NOW), account, history, NOW, params)

    assert (hourly.action, hourly.code) == (Action.SKIP, Reason.RESERVED_FOR_SPIKE)
    assert spike.action is Action.ALLOW


def test_without_a_spike_the_hourly_lane_takes_the_last_unreserved_post() -> None:
    account = small_account(cap=5, reserve=0)
    history = spread(IG, 4, SLOT - HOUR)
    params = make_params()

    assert not has_pending_spike(account, [], history, NOW, params)
    assert decide_hourly_account(account, history, SLOT, NOW, params).action is Action.ALLOW


@pytest.mark.parametrize(
    ("clip_changes", "history_ago_min", "account_id", "pending"),
    [
        ({}, None, IG, True),  # can post now
        ({}, 10, IG, True),  # waiting for the gap still counts as due
        ({"spike_at": T0 - 2 * HOUR}, None, IG, False),  # too old: no longer a spike
        ({"status": "posted"}, None, IG, False),
        ({"lane": Lane.HOURLY}, None, IG, False),
        ({}, None, "tiktok_main", False),  # production TikTok takes no spikes, so its slot is not held
    ],
)
def test_what_counts_as_a_pending_spike(
    clip_changes: dict[str, object], history_ago_min: int | None, account_id: str, pending: bool
) -> None:
    history = [] if history_ago_min is None else [post(account_id, T0 - history_ago_min * MINUTE)]
    clip = spike_clip(**clip_changes)

    assert has_pending_spike(production_accounts()[account_id], [clip], history, T0, make_params()) is pending


def test_a_spike_already_posted_on_the_account_is_not_pending_there() -> None:
    history = [post(IG, T0 - HOUR, Lane.SPIKE, clip_id="s1")]
    assert not has_pending_spike(acct(IG), [spike_clip("s1")], history, T0, make_params())


# ── spike lane across accounts ────────────────────────────────────────────────────────────────────────────
def test_r1_a_spike_goes_to_every_enabled_spike_account() -> None:
    accounts = list(production_accounts().values())

    (plan,) = plan_spikes([spike_clip()], accounts, [], T0, make_params())

    assert plan.clip_id == "s1"
    assert codes(plan.decisions) == {
        "instagram_main": Reason.OK,
        "youtube_main": Reason.OK,
        "tiktok_main": Reason.LANE_NOT_ALLOWED,
        "tiktok_live": Reason.ACCOUNT_DISABLED,
    }
    assert [d.account_id for d in plan.decisions if d.may_send] == ["instagram_main", "youtube_main"]
    assert not plan.demote_to_hourly


def test_r5_the_higher_ranked_spike_posts_first_and_the_second_waits_for_the_gap() -> None:
    accounts = [acct(IG), acct(YT)]
    low, high = spike_clip("low", rank=7.0), spike_clip("high", rank=9.0)

    first, second = plan_spikes([low, high], accounts, [], T0, make_params())

    assert (first.clip_id, second.clip_id) == ("high", "low")
    assert {d.action for d in first.decisions} == {Action.ALLOW}
    assert {(d.action, d.code, d.defer_until) for d in second.decisions} == {
        (Action.DEFER, Reason.MIN_GAP_WAIT, T0 + 15 * MINUTE)
    }
    assert not second.demote_to_hourly


def test_r5_a_second_spike_that_cannot_wait_the_whole_gap_is_demoted() -> None:
    accounts = [acct(IG), acct(YT)]
    waited = spike_clip("low", rank=7.0, approved_at=T0 - MINUTE)

    first, second = plan_spikes([spike_clip("high", rank=9.0), waited], accounts, [], T0, make_params())

    assert {d.code for d in second.decisions} == {Reason.SPIKE_WAIT_EXCEEDED}
    assert second.demote_to_hourly
    assert not first.demote_to_hourly


# ── R12 holding window ────────────────────────────────────────────────────────────────────────────────────
HOLD = make_params(spike_hold_s=420)


def test_r12_without_a_holding_window_a_spike_posts_at_once() -> None:
    (plan,) = plan_spikes([spike_clip()], [acct(IG)], [], T0, make_params())
    assert {d.action for d in plan.decisions} == {Action.ALLOW}
    assert hold_release_at([spike_clip()], make_params()) is None


def test_r12_a_spike_waits_for_the_holding_window_and_is_not_demoted() -> None:
    (plan,) = plan_spikes([spike_clip()], [acct(IG), acct(YT)], [], T0 + MINUTE, HOLD)

    assert {(d.action, d.code, d.defer_until) for d in plan.decisions} == {
        (Action.DEFER, Reason.SPIKE_HOLD, T0 + 7 * MINUTE)
    }
    assert not plan.demote_to_hourly


def test_r12_a_better_clip_that_arrives_during_the_hold_takes_the_spike_post() -> None:
    # The owner's example: a 7 is approved first, a 10 five minutes later. When the hold of the first
    # ends, the batch is decided best first: the 10 posts; the 7 cannot wait out another 15-minute gap
    # within posting.spike_max_wait_s, so it moves to the hourly queue with the boosted rank (R7).
    early = spike_clip("early-7", rank=7.0, at=T0)
    late = spike_clip("late-10", rank=10.0, at=T0 + 5 * MINUTE)
    accounts = [acct(IG), acct(YT)]

    before = plan_spikes([early, late], accounts, [], T0 + 6 * MINUTE, HOLD)
    assert {d.code for p in before for d in p.decisions} == {Reason.SPIKE_HOLD}

    first, second = plan_spikes([early, late], accounts, [], T0 + 7 * MINUTE, HOLD)
    assert first.clip_id == "late-10" and {d.action for d in first.decisions} == {Action.ALLOW}
    assert second.clip_id == "early-7"
    assert {(d.action, d.code) for d in second.decisions} == {(Action.SKIP, Reason.SPIKE_WAIT_EXCEEDED)}
    assert second.demote_to_hourly and second.demoted_rank_score == pytest.approx(7.0 * SPIKE_DEMOTION_BOOST)


def test_r12_the_hold_ends_before_any_clip_gets_too_old() -> None:
    # The spike happened 25 minutes before approval: max age 30 min minus the 2-minute margin wins.
    stale = spike_clip("stale", spike_at=T0 - 25 * MINUTE)
    assert hold_release_at([stale], HOLD) == T0 + 3 * MINUTE
    assert HOLD_AGE_MARGIN_S == 120


def test_r12_only_approved_spike_lane_clips_start_the_hold() -> None:
    hourly = replace(spike_clip("h"), lane=Lane.HOURLY)
    assert hold_release_at([hourly], HOLD) is None
    assert hold_release_at([replace(spike_clip("x"), status="posted")], HOLD) is None


def test_r12_a_held_spike_still_counts_as_pending_for_the_hourly_lane() -> None:
    assert has_pending_spike(acct(IG), [spike_clip()], [], T0 + MINUTE, HOLD)


def test_r5_ties_are_broken_by_the_earlier_spike_then_the_id() -> None:
    a = spike_clip("b-later", rank=8.0, spike_at=T0 - 2 * MINUTE)
    b = spike_clip("a-earlier", rank=8.0, spike_at=T0 - 3 * MINUTE)
    c = spike_clip("0-same-time", rank=8.0, spike_at=T0 - 2 * MINUTE)

    plans = plan_spikes([a, b, c], [acct(IG)], [], T0, make_params())

    assert [p.clip_id for p in plans] == ["a-earlier", "0-same-time", "b-later"]


def test_r6_the_third_spike_is_demoted_on_that_account_only() -> None:
    history = [post(IG, T0 - 20 * MINUTE, Lane.SPIKE), post(IG, T0 - 40 * MINUTE, Lane.SPIKE)]

    (plan,) = plan_spikes([spike_clip()], [acct(IG), acct(YT)], history, T0, make_params())

    assert codes(plan.decisions) == {IG: Reason.SPIKE_HOURLY_LIMIT, YT: Reason.OK}
    assert not plan.demote_to_hourly


def test_r8_an_account_at_its_cap_is_skipped_and_the_others_still_post() -> None:
    accounts = [small_account(cap=3, reserve=0), acct(YT)]
    history = spread(IG, 3, T0 - HOUR)

    (plan,) = plan_spikes([spike_clip()], accounts, history, T0, make_params())

    assert codes(plan.decisions) == {IG: Reason.AT_CAP, YT: Reason.OK}
    assert not plan.demote_to_hourly


def test_r7_a_stale_spike_is_demoted_to_hourly_with_a_boosted_rank() -> None:
    """docs/19 case 5."""
    stale = spike_clip(rank=6.0, spike_at=T0 - 31 * MINUTE)

    (plan,) = plan_spikes([stale], list(production_accounts().values()), [], T0, make_params())

    assert not any(d.would_post for d in plan.decisions)
    assert plan.demote_to_hourly
    assert plan.demoted_rank_score == pytest.approx(9.0)
    assert demoted_rank(6.0) == pytest.approx(6.0 * SPIKE_DEMOTION_BOOST)
    assert SPIKE_DEMOTION_BOOST == 1.5


def test_a_spike_that_could_not_post_anywhere_is_demoted() -> None:
    accounts = [small_account(cap=2, reserve=0), acct(YT, paused_until=T0 + HOUR)]
    history = spread(IG, 2, T0 - HOUR)

    (plan,) = plan_spikes([spike_clip()], accounts, history, T0, make_params())

    assert codes(plan.decisions) == {IG: Reason.AT_CAP, YT: Reason.ACCOUNT_PAUSED}
    assert plan.demote_to_hourly


def test_a_spike_with_no_spike_account_at_all_is_demoted() -> None:
    (plan,) = plan_spikes([spike_clip()], [production_accounts()["tiktok_main"]], [], T0, make_params())
    assert plan.demote_to_hourly


def test_r11_emergency_stop_blocks_every_spike_and_demotes_nothing() -> None:
    accounts = list(production_accounts().values())
    clips = [spike_clip("a"), spike_clip("b", rank=3.0)]

    plans = plan_spikes(clips, accounts, [], T0, make_params(emergency_stop=True))

    assert all(d.code is Reason.EMERGENCY_STOP for plan in plans for d in plan.decisions)
    assert not any(plan.demote_to_hourly for plan in plans)
    assert not any(
        plan.demote_to_hourly for plan in plan_spikes(clips, [], [], T0, make_params(emergency_stop=True))
    )


def test_spike_plan_in_dry_run_sends_nothing_and_keeps_the_rules() -> None:
    accounts = [acct(IG), acct(YT)]
    clips = [spike_clip("high", rank=9.0), spike_clip("low", rank=7.0)]

    first, second = plan_spikes(clips, accounts, [], T0, make_params(dry_run=True))

    assert {(d.action, d.code, d.dry_run) for d in first.decisions} == {
        (Action.DRY_RUN, Reason.DRY_RUN_SETTING, True)
    }
    assert {d.action for d in second.decisions} == {Action.DEFER}  # the planned dry post holds the gap
    assert not any(d.may_send for plan in (first, second) for d in plan.decisions)


def test_an_unapproved_clip_is_neither_posted_nor_demoted() -> None:
    (plan,) = plan_spikes([spike_clip(status="rejected")], [acct(IG)], [], T0, make_params())

    assert codes(plan.decisions) == {IG: Reason.CLIP_NOT_APPROVED}
    assert not plan.demote_to_hourly


def test_an_hourly_clip_passed_to_the_spike_lane_is_refused_and_not_demoted_again() -> None:
    (plan,) = plan_spikes([hourly_clip()], [acct(IG)], [], T0, make_params())

    assert codes(plan.decisions) == {IG: Reason.CLIP_WRONG_LANE}
    assert not plan.demote_to_hourly


def test_two_enabled_rows_on_one_real_tiktok_account_are_both_refused() -> None:
    accounts = production_accounts()
    accounts["tiktok_main"] = replace(accounts["tiktok_main"], lanes=BOTH)
    accounts["tiktok_live"] = replace(accounts["tiktok_live"], enabled=True)

    (plan,) = plan_spikes([spike_clip()], list(accounts.values()), [], T0, make_params())

    assert codes(plan.decisions) == {
        "tiktok_main": Reason.DUPLICATE_CHANNEL,
        "tiktok_live": Reason.DUPLICATE_CHANNEL,
        "instagram_main": Reason.OK,
        "youtube_main": Reason.OK,
    }


def test_d42_two_real_tiktok_accounts_never_get_the_same_spike_clip() -> None:
    accounts = production_accounts()
    accounts["tiktok_main"] = replace(accounts["tiktok_main"], lanes=BOTH)
    accounts["tiktok_live"] = replace(accounts["tiktok_live"], enabled=True, upload_post_user="live-profile")

    (plan,) = plan_spikes([spike_clip()], list(accounts.values()), [], T0, make_params())

    result = codes(plan.decisions)
    assert result["tiktok_live"] is Reason.OK  # decided first: accounts are taken in id order
    assert result["tiktok_main"] is Reason.POSTED_ON_SAME_PLATFORM


def test_staging_spikes_go_only_to_private_accounts() -> None:
    accounts = list(staging_accounts().values())

    (plan,) = plan_spikes([spike_clip()], accounts, [], T0, make_params("staging"))

    assert codes(plan.decisions) == {
        "tiktok_main": Reason.OK,  # SELF_ONLY
        "tiktok_live": Reason.ACCOUNT_DISABLED,
        "instagram_main": Reason.STAGING_INSTAGRAM_NOT_ALLOWED,
        "youtube_main": Reason.OK,  # private
    }


def test_spike_plans_do_not_depend_on_the_order_of_the_input() -> None:
    accounts = list(production_accounts().values())
    clips = [spike_clip("a", rank=5.0), spike_clip("b", rank=9.0), spike_clip("c", rank=7.0)]
    history = [post(IG, T0 - 10 * MINUTE)]

    forward = plan_spikes(clips, accounts, history, T0, make_params())
    backward = plan_spikes(clips[::-1], accounts[::-1], history[::-1], T0, make_params())

    assert forward == backward
    assert [p.clip_id for p in forward] == ["b", "c", "a"]


def test_every_decision_explains_itself() -> None:
    accounts = list(production_accounts().values())
    history = spread(IG, 45, T0 - HOUR)

    plans = plan_spikes(
        [spike_clip(), spike_clip("old", spike_at=T0 - HOUR)], accounts, history, T0, make_params()
    )

    for plan in plans:
        for decision in plan.decisions:
            assert decision.reason.strip()
            assert decision.code in Reason
            assert decision.clip_id == plan.clip_id


def test_a_paused_hourly_lane_skips_the_slot() -> None:
    slot = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    account = replace(production_accounts()["instagram_main"], hourly_paused_until=slot + timedelta(days=365))

    decision = decide_hourly_account(account, [], slot, slot + MINUTE, make_params())

    assert (decision.action, decision.code) == (Action.SKIP, Reason.HOURLY_PAUSED)


def test_an_expired_hourly_pause_does_not_skip() -> None:
    slot = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    account = replace(production_accounts()["instagram_main"], hourly_paused_until=slot - MINUTE)

    decision = decide_hourly_account(account, [], slot, slot + MINUTE, make_params())

    assert decision.code is not Reason.HOURLY_PAUSED

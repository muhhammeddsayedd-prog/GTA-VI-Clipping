"""Cap manager maths: rolling 24 h cap, spike reserve, minimum gap, spike posts per hour, platform ceiling,
reserve release and the learned limit after a rate-limit answer."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from posting_helpers import T0, acct, make_params, post, production_accounts, small_account, spread

from sixstars.posting.caps import (
    DAY,
    HOUR,
    LEARNED_CAP_FLOOR,
    RATE_LIMIT_PAUSE,
    cap_state,
    counts,
    effective_cap,
    effective_gap_s,
    on_rate_limit,
    reserve_left,
)
from sixstars.posting.models import Lane

SECOND = timedelta(seconds=1)
IG = "instagram_main"


# ── rolling 24 h cap ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("used", "free", "at_cap"),
    [(0, 5, False), (3, 2, False), (4, 1, False), (5, 0, True), (6, -1, True)],
)
def test_cap_one_under_exactly_at_and_one_over(used: int, free: int, at_cap: bool) -> None:
    account = small_account(cap=5, reserve=0)
    history = spread(IG, used, T0 - HOUR)

    state = cap_state(account, history, T0, make_params())

    assert (state.cap, state.used_24h, state.free, state.at_cap) == (5, used, free, at_cap)
    assert state.spike_allowed is (not at_cap)
    assert state.hourly_allowed is (not at_cap)


@pytest.mark.parametrize(
    ("age", "counted"),
    [
        (DAY - SECOND, True),
        (DAY, True),  # exactly 24 h old still counts: the slot frees up strictly after 24 h
        (DAY + SECOND, False),
        (timedelta(0), True),
        (-timedelta(minutes=5), True),  # stamped in the future (clock skew): still counts
    ],
)
def test_window_edge_of_the_24_h_cap(age: timedelta, counted: bool) -> None:
    state = cap_state(small_account(), [post(IG, T0 - age)], T0, make_params())
    assert state.used_24h == (1 if counted else 0)


def test_the_window_rolls_over_midnight_and_does_not_reset_at_00_00() -> None:
    account = small_account(cap=3, reserve=0)
    evening = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)
    history = [
        post(IG, evening - timedelta(minutes=40)),
        post(IG, evening - timedelta(minutes=20)),
        post(IG, evening),
    ]
    params = make_params()

    just_after_midnight = datetime(2026, 11, 21, 0, 10, tzinfo=UTC)
    assert cap_state(account, history, just_after_midnight, params).at_cap
    # The oldest post (22:50) leaves the window one second after 22:50 the next day.
    assert cap_state(account, history, datetime(2026, 11, 21, 22, 50, tzinfo=UTC), params).free == 0
    assert cap_state(account, history, datetime(2026, 11, 21, 22, 50, 1, tzinfo=UTC), params).free == 1
    assert cap_state(account, history, datetime(2026, 11, 21, 23, 30, 1, tzinfo=UTC), params).free == 3


def test_the_window_rolls_over_a_month_and_year_end() -> None:
    account = small_account(cap=1, reserve=0)
    history = [post(IG, datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC))]
    params = make_params()

    assert cap_state(account, history, datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC), params).at_cap
    assert cap_state(account, history, datetime(2027, 1, 1, 23, 59, 59, tzinfo=UTC), params).at_cap
    assert not cap_state(account, history, datetime(2027, 1, 2, 0, 0, 0, tzinfo=UTC), params).at_cap


@pytest.mark.parametrize(
    ("status", "dry_run", "live_counts", "dry_counts"),
    [
        ("published", False, True, True),
        ("pending", False, True, True),  # being sent right now
        ("failed", False, True, True),  # may have reached the platform
        ("removed", False, True, True),  # was published; the platform counted it
        ("remove_failed", False, True, True),
        ("skipped", False, False, False),  # a decision that was never sent
        ("skipped", True, False, True),  # a dry-run row counts only while the system is in dry mode
        ("published", True, False, True),
    ],
)
def test_which_posts_count(status: str, dry_run: bool, live_counts: bool, dry_counts: bool) -> None:
    record = post(IG, T0 - HOUR, status=status, dry_run=dry_run)
    account = small_account()

    assert counts(record, dry=False) is live_counts
    assert counts(record, dry=True) is dry_counts
    assert cap_state(account, [record], T0, make_params()).used_24h == int(live_counts)
    assert cap_state(account, [record], T0, make_params(), dry=True).used_24h == int(dry_counts)


def test_posts_of_other_accounts_do_not_count() -> None:
    history = spread("youtube_main", 4, T0 - HOUR) + spread("tiktok_main", 4, T0 - HOUR)
    state = cap_state(small_account(), history, T0, make_params())

    assert (state.used_24h, state.last_post_at, state.gap_ok) == (0, None, True)


# ── platform ceiling ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("account_id", "cap_24h", "expected"),
    [
        ("youtube_main", 40, 30),  # the cap seeded before migration 0005 is above Upload-Post's 30
        ("youtube_main", 30, 30),
        ("youtube_main", 28, 28),
        ("tiktok_main", 14, 14),
        ("tiktok_main", 20, 15),
        ("instagram_main", 45, 45),
        ("instagram_main", 500, 50),
        ("instagram_main", 0, 0),
    ],
)
def test_the_cap_never_exceeds_the_platform_limit(account_id: str, cap_24h: int, expected: int) -> None:
    assert effective_cap(acct(account_id, cap_24h=cap_24h), make_params()) == expected


def test_a_youtube_cap_of_40_stops_at_30() -> None:
    youtube = acct("youtube_main", cap_24h=40, spike_reserve=8)  # the values seeded before migration 0005
    params = make_params()

    assert cap_state(youtube, spread("youtube_main", 29, T0 - HOUR), T0, params).free == 1
    assert cap_state(youtube, spread("youtube_main", 30, T0 - HOUR), T0, params).at_cap


def test_a_platform_limit_lowered_in_settings_applies_at_once() -> None:
    params = make_params(platform_max_24h={**make_params().platform_max_24h, "instagram": 3})
    state = cap_state(small_account(cap=5, reserve=0), spread(IG, 3, T0 - HOUR), T0, params)

    assert (state.cap, state.at_cap) == (3, True)


# ── spike reserve ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("reserve", "spikes", "left"),
    [(0, 0, 0), (0, 3, 0), (2, 0, 2), (2, 1, 1), (2, 2, 0), (2, 3, 0), (10, 4, 6)],
)
def test_reserve_left_is_reserve_minus_spikes_in_24_h_floor_zero(
    reserve: int, spikes: int, left: int
) -> None:
    account = small_account(cap=20, reserve=reserve)
    history = spread(IG, spikes, T0 - HOUR * 2, Lane.SPIKE)

    assert cap_state(account, history, T0, make_params()).reserve_left == left


@pytest.mark.parametrize(
    ("hourly", "spikes", "hourly_allowed", "spike_allowed"),
    [
        # cap 5, reserve 2: the hourly lane may use 3, the last 2 belong to spikes
        (0, 0, True, True),
        (2, 0, True, True),  # free 3 > reserve 2
        (3, 0, False, True),  # free 2 = reserve 2: hourly stops, spike goes
        (4, 0, False, True),  # hourly cannot be here by itself, but a lowered cap can leave this state
        (5, 0, False, False),
        (2, 1, True, True),  # free 2 > reserve left 1
        (3, 1, False, True),  # free 1 = reserve left 1
        (2, 2, True, True),  # reserve used up by spikes: free 1 > 0
        (3, 2, False, False),  # at cap
        (0, 3, True, True),  # spikes beyond the reserve use ordinary capacity
        (1, 4, False, False),
    ],
)
def test_reserve_arithmetic(hourly: int, spikes: int, hourly_allowed: bool, spike_allowed: bool) -> None:
    account = small_account(cap=5, reserve=2)
    history = spread(IG, hourly, T0 - HOUR * 2) + spread(IG, spikes, T0 - HOUR * 12, Lane.SPIKE)

    state = cap_state(account, history, T0, make_params())

    assert (state.hourly_allowed, state.spike_allowed) == (hourly_allowed, spike_allowed)


def test_a_spike_leaving_the_window_gives_its_reserve_slot_back() -> None:
    account = small_account(cap=5, reserve=2)
    spike_at = T0 - DAY
    history = [post(IG, spike_at, Lane.SPIKE)]

    assert cap_state(account, history, T0, make_params()).reserve_left == 1
    assert cap_state(account, history, T0 + SECOND, make_params()).reserve_left == 2


def test_reserve_larger_than_the_cap_leaves_nothing_for_hourly() -> None:
    state = cap_state(small_account(cap=3, reserve=5), [], T0, make_params())
    assert (state.hourly_allowed, state.spike_allowed) == (False, True)


def test_cap_zero_allows_nothing() -> None:
    state = cap_state(small_account(cap=0, reserve=0), [], T0, make_params())
    assert (state.at_cap, state.hourly_allowed, state.spike_allowed) == (True, False, False)


# ── reserve release (optional settings) ───────────────────────────────────────────────────────────────────
def at_hour(hour: int, minute: int = 0, second: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "spikes", "left"),
    [
        (at_hour(17, 59, 59), 0, 10),
        (at_hour(18), 0, 4),  # from 18:00 UTC only 4 stay reserved
        (at_hour(23, 59, 59), 0, 4),
        (at_hour(0, day=21), 0, 10),  # the release ends at midnight
        (at_hour(18), 7, 3),  # fewer than 4 left anyway
        (at_hour(18), 10, 0),
        (at_hour(12), 7, 3),
    ],
)
def test_reserve_release_after_the_configured_hour(now: datetime, spikes: int, left: int) -> None:
    params = make_params(reserve_release_hour_utc=18, reserve_release_keep=4)
    assert reserve_left(small_account(cap=45, reserve=10), spikes, now, params) == left


def test_without_the_release_settings_the_reserve_is_never_released() -> None:
    account = small_account(cap=45, reserve=10)
    for hour in range(24):
        assert reserve_left(account, 0, at_hour(hour), make_params()) == 10


def test_release_lets_the_hourly_lane_use_the_released_posts() -> None:
    account = small_account(cap=12, reserve=10)
    history = spread(IG, 4, at_hour(16))  # free 8
    released = make_params(reserve_release_hour_utc=18, reserve_release_keep=4)

    assert not cap_state(account, history, at_hour(17, 30), released).hourly_allowed
    assert cap_state(account, history, at_hour(18), released).hourly_allowed  # free 8 > 4
    assert not cap_state(account, history, at_hour(18), make_params()).hourly_allowed


# ── spike posts per hour ──────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("ages_min", "limit", "ok"),
    [
        ((), 2, True),
        ((30,), 2, True),
        ((30, 50), 2, False),
        ((30, 60), 2, False),  # exactly one hour old still counts
        ((30, 61), 2, True),
        ((20, 40, 59), 3, False),
        ((30,), 1, False),
        ((30,), 0, False),  # a limit of zero switches spike posts off
        ((), 0, False),
    ],
)
def test_spike_posts_per_rolling_hour(ages_min: tuple[int, ...], limit: int, ok: bool) -> None:
    account = small_account(cap=20, reserve=0)
    history = [post(IG, T0 - timedelta(minutes=age), Lane.SPIKE) for age in ages_min]

    state = cap_state(account, history, T0, make_params(spike_max_per_hour=limit))

    assert state.spike_rate_ok is ok
    assert state.spike_allowed is ok


def test_hourly_posts_do_not_count_as_spikes() -> None:
    history = spread(IG, 3, T0 - timedelta(minutes=5))
    state = cap_state(small_account(cap=20), history, T0, make_params())

    assert (state.spikes_1h, state.spikes_24h, state.spike_rate_ok) == (0, 0, True)


# ── minimum gap ───────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("seconds_ago", "ok"),
    [(0, False), (1, False), (899, False), (900, True), (901, True), (86400, True), (-60, False)],
)
def test_minimum_gap_edges(seconds_ago: int, ok: bool) -> None:
    last = T0 - timedelta(seconds=seconds_ago)
    state = cap_state(small_account(), [post(IG, last)], T0, make_params())

    assert state.gap_ok is ok
    assert state.last_post_at == last
    assert state.gap_ends_at == last + timedelta(seconds=900)


def test_no_post_yet_means_the_gap_is_met() -> None:
    state = cap_state(small_account(), [], T0, make_params())
    assert (state.gap_ok, state.last_post_at, state.gap_ends_at) == (True, None, None)


@pytest.mark.parametrize(
    ("account_gap", "setting_gap", "expected"),
    [(900, 900, 900), (1200, 900, 1200), (600, 900, 900), (0, 0, 0), (0, 900, 900), (900, 0, 900)],
)
def test_the_larger_of_the_account_gap_and_the_setting_applies(
    account_gap: int, setting_gap: int, expected: int
) -> None:
    account = small_account(gap=account_gap)
    params = make_params(min_gap_s=setting_gap)

    assert effective_gap_s(account, params) == expected
    assert cap_state(account, [], T0, params).gap_s == expected


def test_the_gap_counts_from_the_newest_post_of_either_lane() -> None:
    history = [post(IG, T0 - HOUR), post(IG, T0 - timedelta(minutes=5), Lane.SPIKE), post(IG, T0 - HOUR * 3)]
    state = cap_state(small_account(), history, T0, make_params())

    assert state.last_post_at == T0 - timedelta(minutes=5)
    assert not state.gap_ok


def test_the_gap_crosses_midnight() -> None:
    last = datetime(2026, 11, 20, 23, 50, tzinfo=UTC)
    account = small_account()

    assert not cap_state(
        account, [post(IG, last)], datetime(2026, 11, 21, 0, 4, 59, tzinfo=UTC), make_params()
    ).gap_ok
    assert cap_state(
        account, [post(IG, last)], datetime(2026, 11, 21, 0, 5, tzinfo=UTC), make_params()
    ).gap_ok


def test_a_pending_post_holds_the_gap_from_its_creation_time() -> None:
    state = cap_state(
        small_account(), [post(IG, T0 - timedelta(minutes=1), status="pending")], T0, make_params()
    )
    assert not state.gap_ok


# ── rate limit reported by a platform ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("account_id", ["tiktok_main", "instagram_main"])
def test_tiktok_and_instagram_pause_one_hour_and_keep_their_cap(account_id: str) -> None:
    account = production_accounts()[account_id]
    history = spread(account_id, 5, T0 - HOUR)

    outcome = on_rate_limit(account, history, T0, make_params())

    assert outcome.paused_until == T0 + RATE_LIMIT_PAUSE
    assert (outcome.new_cap_24h, outcome.cap_changed) == (account.cap_24h, False)
    assert account_id in outcome.reason


@pytest.mark.parametrize(
    ("cap", "new_cap"),
    [
        (40, 28),  # clamped to the platform's 30 first, then lowered by 2
        (30, 28),
        (28, 26),
        (12, 10),
        (11, 10),  # never below the floor
        (10, 10),
        (8, 8),  # already under the floor: never raised
        (0, 0),
    ],
)
def test_youtube_cap_drops_by_two_down_to_the_floor(cap: int, new_cap: int) -> None:
    outcome = on_rate_limit(acct("youtube_main", cap_24h=cap), [], T0, make_params())

    assert outcome.new_cap_24h == new_cap
    assert outcome.cap_changed is (new_cap != cap)
    assert outcome.new_cap_24h <= cap
    assert LEARNED_CAP_FLOOR == 10


@pytest.mark.parametrize(
    ("oldest_age", "pause"),
    [
        (timedelta(hours=20), timedelta(hours=4)),  # until the oldest post in the window is 24 h old
        (timedelta(hours=2), timedelta(hours=22)),
        (timedelta(hours=23, minutes=30), HOUR),  # never shorter than the basic pause
        (DAY, HOUR),
        (DAY + SECOND, HOUR),  # outside the window: ignored
    ],
)
def test_youtube_pause_lasts_until_the_oldest_post_leaves_the_window(
    oldest_age: timedelta, pause: timedelta
) -> None:
    youtube = production_accounts()["youtube_main"]
    history = [post("youtube_main", T0 - oldest_age), post("instagram_main", T0 - timedelta(hours=1))]
    if oldest_age <= DAY:
        history.append(post("youtube_main", T0 - timedelta(minutes=30)))  # a newer post changes nothing

    outcome = on_rate_limit(youtube, history, T0, make_params())

    assert outcome.paused_until == T0 + pause


def test_youtube_without_posts_in_the_window_pauses_one_hour() -> None:
    outcome = on_rate_limit(production_accounts()["youtube_main"], [], T0, make_params())
    assert outcome.paused_until == T0 + HOUR


def test_dry_run_rows_do_not_extend_a_youtube_pause() -> None:
    history = [post("youtube_main", T0 - HOUR * 2, status="skipped", dry_run=True)]
    outcome = on_rate_limit(production_accounts()["youtube_main"], history, T0, make_params())
    assert outcome.paused_until == T0 + HOUR


def test_a_learned_limit_below_the_configured_cap_blocks_what_the_old_cap_allowed() -> None:
    youtube = acct("youtube_main", cap_24h=28, spike_reserve=4)
    params = make_params()
    history = spread("youtube_main", 26, T0 - HOUR)
    assert cap_state(youtube, history, T0, params).free == 2

    outcome = on_rate_limit(youtube, history, T0, params)
    learned = replace(youtube, cap_24h=outcome.new_cap_24h, paused_until=outcome.paused_until)

    after_pause = outcome.paused_until + SECOND
    state = cap_state(learned, history, after_pause, params)
    assert (state.cap, state.used_24h) == (26, 25)  # the oldest post has just left the window
    assert not state.hourly_allowed  # free 1 is inside the spike reserve of 4
    assert cap_state(learned, history, T0, params).at_cap  # 26 used of the learned 26


def test_a_cap_lowered_below_the_posts_already_made_gives_negative_free_and_blocks_both_lanes() -> None:
    account = small_account(cap=3, reserve=0)
    state = cap_state(account, spread(IG, 5, T0 - HOUR), T0, make_params())

    assert (state.free, state.at_cap, state.hourly_allowed, state.spike_allowed) == (-2, True, False, False)

"""The emergency-stop rules as pure functions: the strike window, the limit, and the account-level rule."""

from datetime import timedelta

import pytest
from guardian_helpers import NOW, post

from sixstars.guardian import claims, stop
from sixstars.guardian.claims import Finding, Observation

DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)


@pytest.mark.parametrize(
    ("strikes", "limit", "expected"),
    [
        (0, 2, False),
        (1, 2, False),  # one below the limit
        (2, 2, True),  # exactly the limit stops
        (3, 2, True),
        (1, 1, True),
        (0, 1, False),
        (5, 10, False),
        (10, 10, True),
        # A broken limit (0 or negative) must never mean "no limit" or "always stopped with no strikes".
        (0, 0, False),
        (1, 0, True),
        (1, -3, True),
    ],
)
def test_the_strike_limit(strikes: int, limit: int, expected: bool) -> None:
    assert stop.should_stop(strikes, limit) is expected


@pytest.mark.parametrize(
    ("ages", "window_d", "expected"),
    [
        ([], 7, 0),
        ([timedelta(0)], 7, 1),  # a strike at this very moment counts
        ([timedelta(hours=1), timedelta(days=6, hours=23)], 7, 2),
        ([7 * DAY], 7, 1),  # exactly at the edge of the window still counts
        ([7 * DAY + SECOND], 7, 0),  # one second older does not
        ([DAY, 8 * DAY, 30 * DAY], 7, 1),
        ([DAY, 2 * DAY], 1, 1),
        ([-SECOND], 7, 0),  # a time after "now" is not in the window
    ],
)
def test_strikes_are_counted_inside_the_window(ages: list[timedelta], window_d: float, expected: int) -> None:
    since = stop.window_start(NOW, window_d, None)
    assert stop.strikes_in_window([NOW - age for age in ages], NOW, since) == expected


def test_strikes_before_the_last_guardian_stop_do_not_count_again() -> None:
    # The stop was set two days ago and the owner restarted posting: the two strikes that caused it are
    # history, a new one counts.
    last_stop = NOW - 2 * DAY
    since = stop.window_start(NOW, 7, last_stop)
    assert since == last_stop
    times = [NOW - 3 * DAY, NOW - 2 * DAY - SECOND, NOW - DAY]
    assert stop.strikes_in_window(times, NOW, since) == 1


def test_a_stop_older_than_the_window_does_not_extend_it() -> None:
    assert stop.window_start(NOW, 7, NOW - 30 * DAY) == NOW - 7 * DAY


def gone(post_id: str, platform: str = "youtube") -> tuple[Observation, Finding]:
    ref = post(post_id, platform)
    return (
        Observation(ref, "gone", "removed_by_platform", "gone", "test"),
        Finding(ref, "removed_by_platform", "gone", exists=False),
    )


def fine(post_id: str, platform: str = "youtube") -> Observation:
    return Observation(post(post_id, platform), "ok", source="test")


@pytest.mark.parametrize(
    ("gone_n", "ok_n", "minimum", "ratio", "expected"),
    [
        (5, 0, 5, 0.8, ["youtube"]),  # all five posts gone
        (4, 0, 5, 0.8, []),  # below the minimum: handled creator by creator
        (5, 1, 5, 0.8, ["youtube"]),  # 5 of 6 = 83 %
        (8, 2, 5, 0.8, ["youtube"]),  # exactly 80 %
        (7, 3, 5, 0.8, []),  # 70 %: most posts are fine, so these are separate problems
        (5, 20, 5, 0.8, []),
        (1, 0, 1, 1.0, ["youtube"]),
        (0, 10, 1, 0.0, []),  # nothing gone is never a mass event
    ],
)
def test_the_account_level_rule(
    gone_n: int, ok_n: int, minimum: int, ratio: float, expected: list[str]
) -> None:
    pairs = [gone(f"g{index}") for index in range(gone_n)]
    observations = [pair[0] for pair in pairs] + [fine(f"k{index}") for index in range(ok_n)]
    findings = [pair[1] for pair in pairs]
    assert claims.mass_platforms(observations, findings, minimum, ratio) == expected


def test_the_account_level_rule_is_per_platform_and_ignores_restrictions() -> None:
    youtube = [gone(f"y{index}", "youtube") for index in range(5)]
    tiktok_ok = [fine(f"t{index}", "tiktok") for index in range(5)]
    blocked = Finding(post("b1", "tiktok"), "blocked", "blocked in DE", exists=True)
    observations = [pair[0] for pair in youtube] + tiktok_ok
    findings = [pair[1] for pair in youtube] + [blocked]
    assert claims.mass_platforms(observations, findings, 5, 0.8) == ["youtube"]

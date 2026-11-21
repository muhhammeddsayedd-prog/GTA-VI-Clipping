"""D116: the hourly lane keeps one clip apart across platforms, and platforms pick in priority order."""

from dataclasses import replace
from datetime import timedelta

import pytest
from posting_helpers import (
    DEFAULT_SETTINGS,
    T0,
    acct,
    hourly_clip,
    make_params,
    post,
    production_accounts,
    spike_clip,
)

from sixstars.posting.models import Account, Action, Lane, Platform, Reason
from sixstars.posting.params import PostingParams
from sixstars.posting.precedence import cross_platform_block, decide_spike
from sixstars.posting.selection import hourly_candidates, hourly_order, plan_hourly_slot

IG, TT, YT = "instagram_main", "tiktok_main", "youtube_main"
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
SLOT = T0  # 12:00 UTC, an hour of every seeded hourly account in the helpers
NOW = SLOT + 2 * MINUTE
PRIORITY = (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)


def d116(**changes: object) -> PostingParams:
    """Production parameters with migration 0035's values."""
    return make_params(cross_platform_min_gap_s=2700, platform_priority=PRIORITY, **changes)


def prod() -> list[Account]:
    return list(production_accounts().values())


def chosen(plan: object) -> dict[str, str | None]:
    return {d.account_id: d.clip_id for d in plan.decisions}  # type: ignore[attr-defined]


def codes(plan: object) -> dict[str, Reason]:
    return {d.account_id: d.code for d in plan.decisions}  # type: ignore[attr-defined]


# ── one slot ──────────────────────────────────────────────────────────────────────────────────────────────
def test_youtube_picks_first_and_the_others_take_the_next_clips() -> None:
    queue = [hourly_clip("top", 9.0, "a"), hourly_clip("second", 7.0, "b"), hourly_clip("third", 5.0, "c")]

    plan = plan_hourly_slot(prod(), [], queue, SLOT, NOW, d116())

    assert chosen(plan) == {YT: "top", TT: "second", IG: "third", "tiktok_live": None}
    assert [d.account_id for d in plan.decisions] == [YT, "tiktok_live", TT, IG]  # platform, then id


def test_one_good_clip_goes_to_youtube_and_instagram_takes_clip_two() -> None:
    accounts = [a for a in prod() if a.platform is not Platform.TIKTOK]  # production: TikTok's lanes are off
    queue = [hourly_clip("top", 9.0, "a"), hourly_clip("second", 4.0, "b")]

    plan = plan_hourly_slot(accounts, [], queue, SLOT, NOW, d116())

    assert chosen(plan) == {YT: "top", IG: "second"}


def test_an_account_with_nothing_left_says_why() -> None:
    accounts = [a for a in prod() if a.platform is not Platform.TIKTOK]

    plan = plan_hourly_slot(accounts, [], [hourly_clip("only", 9.0)], SLOT, NOW, d116())

    assert chosen(plan) == {YT: "only", IG: None}
    assert codes(plan)[IG] is Reason.CROSS_PLATFORM_TOO_SOON
    assert not plan.slot_empty or plan.decisions[0].would_post


@pytest.mark.parametrize(
    ("ago", "youtube_gets"), [(50 * MINUTE, "top"), (30 * MINUTE, "second"), (45 * MINUTE, "top")]
)
def test_a_clip_posted_on_instagram_earlier(ago: timedelta, youtube_gets: str) -> None:
    queue = [hourly_clip("top", 9.0, "a"), hourly_clip("second", 4.0, "b")]
    history = [post(IG, NOW - ago, clip_id="top", creator_id="a", slot_at=SLOT - HOUR)]
    youtube = [acct(YT)]

    plan = plan_hourly_slot(youtube, history, queue, SLOT, NOW, d116())

    assert chosen(plan) == {YT: youtube_gets}


def test_without_the_setting_every_platform_gets_the_same_clip() -> None:
    queue = [hourly_clip("top", 9.0, "a"), hourly_clip("second", 4.0, "b")]

    plan = plan_hourly_slot(prod(), [], queue, SLOT, NOW, make_params())

    assert chosen(plan) == {IG: "top", TT: "top", YT: "top", "tiktok_live": None}  # D18, id order


# ── the rule itself ───────────────────────────────────────────────────────────────────────────────────────
def test_skipped_posts_and_the_same_platform_do_not_count() -> None:
    clip = hourly_clip("c1")
    history = [
        post(IG, NOW - 5 * MINUTE, clip_id="c1", status="skipped"),
        post(YT, NOW - 5 * MINUTE, clip_id="c1"),  # same platform: D42's business, not D116's
    ]

    assert cross_platform_block(clip, acct(YT), history, NOW, d116(), dry=False) is None


def test_dry_run_rows_count_only_for_a_dry_decision() -> None:
    clip = hourly_clip("c1")
    history = [post(IG, NOW - 5 * MINUTE, clip_id="c1", status="skipped", dry_run=True)]

    assert cross_platform_block(clip, acct(YT), history, NOW, d116(), dry=False) is None
    blocked = cross_platform_block(clip, acct(YT), history, NOW, d116(), dry=True)
    assert blocked is not None and blocked[0] is Reason.CROSS_PLATFORM_TOO_SOON


def test_spikes_are_unchanged() -> None:
    history = [post(IG, T0 - 5 * MINUTE, Lane.SPIKE, clip_id="s1")]

    decision = decide_spike(spike_clip("s1"), acct(YT), history, T0, d116())

    assert decision.action is Action.ALLOW


# ── settings ──────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "order"),
    [
        (["youtube", "tiktok", "instagram"], PRIORITY),
        (["youtube", "bogus", "youtube", 3, "instagram"], (Platform.YOUTUBE, Platform.INSTAGRAM)),
        ("youtube", ()),
        (None, ()),
        ({"youtube": 1}, ()),
    ],
)
def test_platform_priority_is_read_leniently(raw: object, order: tuple[Platform, ...]) -> None:
    values = {**DEFAULT_SETTINGS, "posting.platform_priority": raw}
    if raw is None:
        values.pop("posting.platform_priority")
    params = PostingParams.from_settings(values, "production")
    assert params.platform_priority == order


def test_a_malformed_priority_falls_back_to_id_order() -> None:
    params = make_params(platform_priority=())
    assert [a.id for a in hourly_order(prod(), params)] == sorted(a.id for a in prod())


def test_unlisted_platforms_pick_last() -> None:
    params = make_params(platform_priority=(Platform.YOUTUBE,))
    assert [a.id for a in hourly_order(prod(), params)] == [YT, IG, "tiktok_live", TT]


def test_the_gap_is_read_and_must_not_be_negative() -> None:
    params = PostingParams.from_settings(
        {**DEFAULT_SETTINGS, "posting.cross_platform_min_gap_s": 2700}, "production"
    )
    assert params.cross_platform_min_gap_s == 2700
    assert PostingParams.from_settings(DEFAULT_SETTINGS, "production").cross_platform_min_gap_s == 0
    with pytest.raises(ValueError, match="cross_platform_min_gap_s"):
        replace(params, cross_platform_min_gap_s=-1)


# ── a clip one platform skipped stays available to it ─────────────────────────────────────────────────────
def test_posted_clips_stay_in_the_hourly_queue_only_with_d116() -> None:
    posted = hourly_clip("posted", 9.0, status="posted")
    fresh = hourly_clip("fresh", 5.0)

    assert [c.id for c in hourly_candidates([posted, fresh], NOW, d116())] == ["posted", "fresh"]
    assert [c.id for c in hourly_candidates([posted, fresh], NOW, make_params())] == ["fresh"]


def test_a_posted_clip_goes_only_to_the_platform_that_skipped_it() -> None:
    accounts = [a for a in prod() if a.platform is not Platform.TIKTOK]
    queue = [hourly_clip("yt-hit", 9.0, "a", status="posted"), hourly_clip("new", 6.0, "b")]
    history = [post(YT, NOW - HOUR, clip_id="yt-hit", creator_id="a", slot_at=SLOT - HOUR)]

    plan = plan_hourly_slot(accounts, history, queue, SLOT, NOW, d116())

    assert chosen(plan) == {YT: "new", IG: "yt-hit"}  # YouTube cannot repeat it; Instagram takes it now

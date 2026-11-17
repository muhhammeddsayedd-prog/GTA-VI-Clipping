"""Posting parameters (`from_settings`) and the posting gate: stop, dry run, launch time, account state,
lanes, privacy and the staging safety rule."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from posting_helpers import (
    DEFAULT_SETTINGS,
    LAUNCH_AT,
    T0,
    acct,
    make_params,
    production_accounts,
    staging_accounts,
)

from sixstars.posting.gate import Mode, check_gate, duplicate_channels, is_dry, posting_mode
from sixstars.posting.models import Account, Action, Decision, Lane, Platform, PostRecord, Reason
from sixstars.posting.params import OPTIONAL_KEYS, REQUIRED_KEYS, PostingParams

SECOND = timedelta(seconds=1)


# ── params ────────────────────────────────────────────────────────────────────────────────────────────────
def test_from_settings_reads_the_documented_defaults() -> None:
    params = PostingParams.from_settings(DEFAULT_SETTINGS, "production")

    assert params.emergency_stop is False
    assert params.dry_run is True
    assert params.launch_at == LAUNCH_AT
    assert (params.jitter_max_s, params.min_gap_s, params.spike_max_per_hour) == (300, 900, 2)
    assert (params.skip_hourly_after_spike_s, params.spike_max_wait_s, params.spike_max_age_s) == (
        1800,
        900,
        1800,
    )
    assert (params.freshness_half_life_live_h, params.freshness_half_life_vod_h) == (6.0, 24.0)
    assert params.max_clip_age_h == 72.0
    assert params.base_hashtags == ("#GTA6", "#GTAVI", "#GTA6Clips", "#Gaming")
    assert params.youtube_extra_hashtags == ("#Shorts",)
    assert dict(params.platform_max_24h) == {
        Platform.TIKTOK: 15,
        Platform.INSTAGRAM: 50,
        Platform.YOUTUBE: 30,
    }


def test_optional_keys_default_to_the_reading_that_posts_less() -> None:
    params = PostingParams.from_settings(DEFAULT_SETTINGS, "staging")

    assert params.staging_allow_instagram is False
    assert params.reserve_release_hour_utc is None
    assert params.reserve_release_keep is None


def test_optional_keys_are_read_when_present() -> None:
    values = {
        **DEFAULT_SETTINGS,
        "posting.staging_allow_instagram": True,
        "posting.reserve_release_hour_utc": 18,
        "posting.reserve_release_keep": 4,
    }
    params = PostingParams.from_settings(values, "staging")

    assert params.staging_allow_instagram is True
    assert (params.reserve_release_hour_utc, params.reserve_release_keep) == (18, 4)


def test_every_settings_key_is_listed_once() -> None:
    assert len(set(REQUIRED_KEYS) | set(OPTIONAL_KEYS)) == len(REQUIRED_KEYS) + len(OPTIONAL_KEYS)
    assert set(DEFAULT_SETTINGS) == set(REQUIRED_KEYS)


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_a_missing_required_setting_raises(key: str) -> None:
    values = {k: v for k, v in DEFAULT_SETTINGS.items() if k != key}

    with pytest.raises(KeyError, match=key.replace(".", r"\.")):
        PostingParams.from_settings(values, "production")


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("control.emergency_stop", "false"),
        ("control.emergency_stop", 0),
        ("control.emergency_stop", None),
        ("launch.dry_run", "true"),
        ("launch.dry_run", 1),
        ("launch.launch_at", 1763463600),
        ("launch.launch_at", None),
        ("posting.min_gap_s", "900"),
        ("posting.min_gap_s", 900.5),
        ("posting.min_gap_s", True),
        ("posting.spike_max_per_hour", None),
        ("posting.freshness_half_life_h", 6),
        ("posting.max_clip_age_h", "72"),
        ("posting.first_comment", None),
        ("posting.base_hashtags", "#GTA6"),
        ("posting.base_hashtags", [1, 2]),
        ("posting.platform_max_24h", 30),
        ("posting.platform_max_24h", {"tiktok": "15", "instagram": 50, "youtube": 30}),
        ("posting.staging_allow_instagram", "true"),
        ("posting.reserve_release_hour_utc", "18"),
    ],
)
def test_a_value_of_the_wrong_type_raises(key: str, bad: object) -> None:
    with pytest.raises(TypeError, match=key.replace(".", r"\.")):
        PostingParams.from_settings({**DEFAULT_SETTINGS, key: bad}, "production")


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("launch.launch_at", "2026-11-18T11:00:00"),  # no time zone
        ("launch.launch_at", "soon"),
        ("posting.min_gap_s", -1),
        ("posting.jitter_max_s", -5),
        ("posting.spike_max_per_hour", -1),
        ("posting.max_clip_age_h", 0),
        ("posting.freshness_half_life_h", {"live": 0, "vod": 24}),
        ("posting.platform_max_24h", {"tiktok": 15, "instagram": 50}),  # no YouTube limit
        ("posting.platform_max_24h", {"tiktok": 15, "instagram": 50, "youtube": -1}),
        ("posting.platform_max_24h", {"tiktok": 15, "instagram": 50, "youtube": 30, "x": 5}),
        ("posting.reserve_release_hour_utc", 18),  # without posting.reserve_release_keep
        ("posting.reserve_release_keep", 4),  # without posting.reserve_release_hour_utc
    ],
)
def test_an_invalid_value_raises(key: str, bad: object) -> None:
    with pytest.raises(ValueError):
        PostingParams.from_settings({**DEFAULT_SETTINGS, key: bad}, "production")


def test_half_life_needs_both_sources() -> None:
    with pytest.raises(KeyError, match="vod"):
        PostingParams.from_settings(
            {**DEFAULT_SETTINGS, "posting.freshness_half_life_h": {"live": 6}}, "staging"
        )


@pytest.mark.parametrize("hour", [-1, 24])
def test_release_hour_outside_the_day_raises(hour: int) -> None:
    values = {
        **DEFAULT_SETTINGS,
        "posting.reserve_release_hour_utc": hour,
        "posting.reserve_release_keep": 4,
    }
    with pytest.raises(ValueError, match="reserve_release_hour_utc"):
        PostingParams.from_settings(values, "production")


@pytest.mark.parametrize("app_env", ["", "prod", "Production", "development", "test"])
def test_an_unknown_app_env_raises(app_env: str) -> None:
    with pytest.raises(ValueError, match="app_env"):
        PostingParams.from_settings(DEFAULT_SETTINGS, app_env)


def test_launch_at_in_another_zone_is_the_same_moment() -> None:
    params = PostingParams.from_settings(
        {**DEFAULT_SETTINGS, "launch.launch_at": "2026-11-18T13:00:00+02:00"}, "production"
    )
    assert params.launch_at == LAUNCH_AT


# ── models ────────────────────────────────────────────────────────────────────────────────────────────────
def test_naive_times_are_refused_everywhere() -> None:
    naive = datetime(2026, 11, 20, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        PostRecord("instagram_main", Platform.INSTAGRAM, "c", Lane.HOURLY, "published", naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        acct(paused_until=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        check_gate(acct(), Lane.HOURLY, naive, make_params())
    with pytest.raises(ValueError, match="timezone-aware"):
        posting_mode(naive, make_params())


def test_unknown_post_status_is_refused() -> None:
    with pytest.raises(ValueError, match="status"):
        PostRecord("instagram_main", Platform.INSTAGRAM, "c", Lane.HOURLY, "done", T0)


@pytest.mark.parametrize(
    "changes",
    [{"cap_24h": -1}, {"spike_reserve": -1}, {"min_gap_s": -1}, {"hourly_hours_utc": frozenset({24})}],
)
def test_impossible_account_values_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        acct(**changes)


def test_decision_shape_is_enforced() -> None:
    with pytest.raises(ValueError):
        Decision(Action.ALLOW, Reason.OK, "x", "a", Lane.HOURLY, dry_run=True)
    with pytest.raises(ValueError):
        Decision(Action.DRY_RUN, Reason.DRY_RUN_SETTING, "x", "a", Lane.HOURLY, dry_run=False)
    with pytest.raises(ValueError):
        Decision(Action.DEFER, Reason.MIN_GAP_WAIT, "x", "a", Lane.SPIKE)
    with pytest.raises(ValueError):
        Decision(Action.SKIP, Reason.AT_CAP, "x", "a", Lane.SPIKE, defer_until=T0)


def test_only_allow_may_send() -> None:
    sendable = {
        action: Decision(
            action,
            Reason.OK,
            "x",
            "a",
            Lane.HOURLY,
            dry_run=action is Action.DRY_RUN,
            defer_until=T0 if action is Action.DEFER else None,
        ).may_send
        for action in Action
    }
    assert sendable == {Action.ALLOW: True, Action.DRY_RUN: False, Action.SKIP: False, Action.DEFER: False}


# ── global mode ───────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("stop", "dry_run", "now", "mode", "code"),
    [
        (False, False, LAUNCH_AT - SECOND, Mode.DRY, Reason.BEFORE_LAUNCH),
        (False, False, LAUNCH_AT, Mode.LIVE, Reason.OK),  # exactly at launch_at posting is live
        (False, False, LAUNCH_AT + SECOND, Mode.LIVE, Reason.OK),
        (False, True, LAUNCH_AT - SECOND, Mode.DRY, Reason.DRY_RUN_SETTING),
        (False, True, LAUNCH_AT, Mode.DRY, Reason.DRY_RUN_SETTING),
        (False, True, T0, Mode.DRY, Reason.DRY_RUN_SETTING),
        (True, False, LAUNCH_AT - SECOND, Mode.STOPPED, Reason.EMERGENCY_STOP),
        (True, False, T0, Mode.STOPPED, Reason.EMERGENCY_STOP),
        (True, True, LAUNCH_AT - SECOND, Mode.STOPPED, Reason.EMERGENCY_STOP),
        (True, True, T0, Mode.STOPPED, Reason.EMERGENCY_STOP),
    ],
)
def test_posting_mode(stop: bool, dry_run: bool, now: datetime, mode: Mode, code: Reason) -> None:
    params = make_params(emergency_stop=stop, dry_run=dry_run)

    assert posting_mode(now, params) == (mode, code)
    assert is_dry(now, params) is (mode is not Mode.LIVE)


def test_launch_time_is_compared_as_a_moment_not_as_wall_clock() -> None:
    plus_two = timezone(timedelta(hours=2))
    just_before = datetime(2026, 11, 18, 12, 59, 59, tzinfo=plus_two)  # 10:59:59 UTC
    at_launch = datetime(2026, 11, 18, 13, 0, 0, tzinfo=plus_two)  # 11:00:00 UTC

    assert posting_mode(just_before, make_params())[0] is Mode.DRY
    assert posting_mode(at_launch, make_params())[0] is Mode.LIVE


# ── account gate ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lane", list(Lane))
def test_a_healthy_account_passes_live(lane: Lane) -> None:
    decision = check_gate(acct("instagram_main"), lane, T0, make_params())

    assert (decision.action, decision.code, decision.dry_run) == (Action.ALLOW, Reason.OK, False)
    assert decision.may_send
    assert decision.account_id == "instagram_main"
    assert decision.lane is lane


@pytest.mark.parametrize(
    ("changes", "lane", "code"),
    [
        ({"enabled": False}, Lane.HOURLY, Reason.ACCOUNT_DISABLED),
        ({"enabled": False}, Lane.SPIKE, Reason.ACCOUNT_DISABLED),
        ({"lanes": frozenset({Lane.HOURLY})}, Lane.SPIKE, Reason.LANE_NOT_ALLOWED),
        ({"lanes": frozenset({Lane.SPIKE})}, Lane.HOURLY, Reason.LANE_NOT_ALLOWED),
        ({"lanes": frozenset()}, Lane.HOURLY, Reason.LANE_NOT_ALLOWED),
        ({"lanes": frozenset()}, Lane.SPIKE, Reason.LANE_NOT_ALLOWED),
        ({"paused_until": T0 + SECOND}, Lane.HOURLY, Reason.ACCOUNT_PAUSED),
        ({"paused_until": T0 + SECOND}, Lane.SPIKE, Reason.ACCOUNT_PAUSED),
        ({"paused_until": T0}, Lane.HOURLY, Reason.ACCOUNT_PAUSED),  # the pause ends strictly after
        ({"paused_until": T0 + timedelta(days=3)}, Lane.SPIKE, Reason.ACCOUNT_PAUSED),
    ],
)
def test_account_state_blocks(changes: dict[str, object], lane: Lane, code: Reason) -> None:
    decision = check_gate(acct("instagram_main", **changes), lane, T0, make_params())

    assert (decision.action, decision.code) == (Action.SKIP, code)
    assert not decision.may_send
    assert decision.reason


def test_a_pause_that_has_ended_no_longer_blocks() -> None:
    decision = check_gate(acct(paused_until=T0 - SECOND), Lane.HOURLY, T0, make_params())
    assert decision.action is Action.ALLOW


def test_the_gate_reports_the_first_failing_check() -> None:
    broken = acct("tiktok_live", paused_until=T0 + SECOND, privacy=None)  # disabled, paused, no privacy
    stopped = make_params(emergency_stop=True, dry_run=True)

    assert check_gate(broken, Lane.HOURLY, T0, stopped).code is Reason.EMERGENCY_STOP
    assert check_gate(broken, Lane.HOURLY, T0, make_params()).code is Reason.ACCOUNT_DISABLED
    enabled = replace(broken, enabled=True)
    assert check_gate(enabled, Lane.HOURLY, T0, make_params()).code is Reason.LANE_NOT_ALLOWED
    assert check_gate(enabled, Lane.SPIKE, T0, make_params()).code is Reason.ACCOUNT_PAUSED
    unpaused = replace(enabled, paused_until=None)
    assert check_gate(unpaused, Lane.SPIKE, T0, make_params()).code is Reason.PRIVACY_INVALID
    assert check_gate(unpaused, Lane.SPIKE, T0, make_params(dry_run=True)).code is Reason.PRIVACY_INVALID


@pytest.mark.parametrize(
    ("account_id", "privacy", "ok"),
    [
        ("tiktok_main", "PUBLIC_TO_EVERYONE", True),
        ("tiktok_main", "SELF_ONLY", True),
        ("tiktok_main", "MUTUAL_FOLLOW_FRIENDS", True),
        ("tiktok_main", "FOLLOWER_OF_CREATOR", True),
        ("tiktok_main", None, False),
        ("tiktok_main", "", False),
        ("tiktok_main", "public", False),  # a YouTube value
        ("tiktok_main", "self_only", False),
        ("youtube_main", "public", True),
        ("youtube_main", "unlisted", True),
        ("youtube_main", "private", True),
        ("youtube_main", None, False),
        ("youtube_main", "PRIVATE", False),
        ("youtube_main", "SELF_ONLY", False),  # a TikTok value
        ("instagram_main", None, True),
        ("instagram_main", "anything", True),  # Instagram has no privacy option; the value is not sent
    ],
)
def test_privacy_must_be_a_value_the_platform_knows(account_id: str, privacy: str | None, ok: bool) -> None:
    decision = check_gate(acct(account_id, privacy=privacy), Lane.HOURLY, T0, make_params())

    assert decision.would_post is ok
    if not ok:
        assert decision.code is Reason.PRIVACY_INVALID


# ── emergency stop combined with everything ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("app_env", ["production", "staging"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("now", [LAUNCH_AT - SECOND, LAUNCH_AT, T0])
@pytest.mark.parametrize("lane", list(Lane))
@pytest.mark.parametrize("account_id", ["tiktok_main", "tiktok_live", "instagram_main", "youtube_main"])
def test_emergency_stop_blocks_every_account_in_every_mode(
    app_env: str, dry_run: bool, now: datetime, lane: Lane, account_id: str
) -> None:
    params = make_params(app_env, emergency_stop=True, dry_run=dry_run, staging_allow_instagram=True)
    accounts = staging_accounts() if app_env == "staging" else production_accounts()

    decision = check_gate(accounts[account_id], lane, now, params)

    assert (decision.action, decision.code) == (Action.SKIP, Reason.EMERGENCY_STOP)
    assert not decision.may_send
    assert not decision.would_post


# ── dry run and launch time ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("dry_run", "now", "action", "code"),
    [
        (True, T0, Action.DRY_RUN, Reason.DRY_RUN_SETTING),
        (True, LAUNCH_AT - SECOND, Action.DRY_RUN, Reason.DRY_RUN_SETTING),
        (False, LAUNCH_AT - timedelta(days=1), Action.DRY_RUN, Reason.BEFORE_LAUNCH),
        (False, LAUNCH_AT - SECOND, Action.DRY_RUN, Reason.BEFORE_LAUNCH),
        (False, LAUNCH_AT, Action.ALLOW, Reason.OK),
        (False, LAUNCH_AT + SECOND, Action.ALLOW, Reason.OK),
    ],
)
def test_dry_mode_never_allows_a_real_post(
    dry_run: bool, now: datetime, action: Action, code: Reason
) -> None:
    decision = check_gate(acct("youtube_main"), Lane.HOURLY, now, make_params(dry_run=dry_run))

    assert (decision.action, decision.code) == (action, code)
    assert decision.dry_run is (action is Action.DRY_RUN)
    assert decision.may_send is (action is Action.ALLOW)
    assert decision.would_post


def test_a_blocked_account_is_skipped_in_dry_mode_too() -> None:
    decision = check_gate(acct(enabled=False), Lane.HOURLY, T0, make_params(dry_run=True))

    assert (decision.action, decision.code, decision.dry_run) == (Action.SKIP, Reason.ACCOUNT_DISABLED, True)


# ── staging safety ────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("account_id", "privacy", "allow_instagram", "code"),
    [
        ("tiktok_main", "SELF_ONLY", False, Reason.OK),
        ("tiktok_main", "PUBLIC_TO_EVERYONE", False, Reason.STAGING_PRIVACY_NOT_PRIVATE),
        ("tiktok_main", "MUTUAL_FOLLOW_FRIENDS", False, Reason.STAGING_PRIVACY_NOT_PRIVATE),
        ("tiktok_main", "FOLLOWER_OF_CREATOR", True, Reason.STAGING_PRIVACY_NOT_PRIVATE),
        ("tiktok_main", None, False, Reason.PRIVACY_INVALID),
        ("youtube_main", "private", False, Reason.OK),
        ("youtube_main", "public", False, Reason.STAGING_PRIVACY_NOT_PRIVATE),
        ("youtube_main", "unlisted", True, Reason.STAGING_PRIVACY_NOT_PRIVATE),
        ("youtube_main", None, False, Reason.PRIVACY_INVALID),
        ("instagram_main", None, False, Reason.STAGING_INSTAGRAM_NOT_ALLOWED),
        ("instagram_main", "private", False, Reason.STAGING_INSTAGRAM_NOT_ALLOWED),
        ("instagram_main", None, True, Reason.OK),
    ],
)
def test_staging_refuses_real_posts_that_would_be_public(
    account_id: str, privacy: str | None, allow_instagram: bool, code: Reason
) -> None:
    params = make_params("staging", staging_allow_instagram=allow_instagram)
    account = replace(staging_accounts()[account_id], privacy=privacy, enabled=True)

    decision = check_gate(account, Lane.SPIKE, T0, params)

    assert decision.code is code
    assert decision.may_send is (code is Reason.OK)


def test_production_posts_publicly_with_the_same_accounts() -> None:
    for account_id in ("tiktok_main", "instagram_main", "youtube_main"):
        decision = check_gate(production_accounts()[account_id], Lane.HOURLY, T0, make_params("production"))
        assert decision.action is Action.ALLOW, account_id


def test_the_production_seed_is_refused_if_it_ever_runs_in_staging() -> None:
    params = make_params("staging")
    codes = {
        account.id: check_gate(
            replace(account, enabled=True, lanes=frozenset(Lane)), Lane.HOURLY, T0, params
        ).code
        for account in production_accounts().values()
    }
    assert codes == {
        "tiktok_main": Reason.STAGING_PRIVACY_NOT_PRIVATE,
        "tiktok_live": Reason.STAGING_PRIVACY_NOT_PRIVATE,
        "instagram_main": Reason.STAGING_INSTAGRAM_NOT_ALLOWED,
        "youtube_main": Reason.STAGING_PRIVACY_NOT_PRIVATE,
    }


def test_staging_dry_run_records_would_be_posts_without_the_staging_check() -> None:
    """The staging rule is about real posts; a dry run sends nothing, whatever the privacy."""
    decision = check_gate(
        staging_accounts()["instagram_main"], Lane.HOURLY, T0, make_params("staging", dry_run=True)
    )

    assert (decision.action, decision.code) == (Action.DRY_RUN, Reason.DRY_RUN_SETTING)
    assert not decision.may_send


# ── duplicate channels ────────────────────────────────────────────────────────────────────────────────────
def test_seeded_accounts_have_no_duplicate_channel_because_tiktok_live_is_disabled() -> None:
    assert duplicate_channels(production_accounts().values()) == frozenset()
    assert duplicate_channels(staging_accounts().values()) == frozenset()


def test_two_enabled_rows_for_one_real_account_are_both_flagged() -> None:
    accounts = production_accounts()
    accounts["tiktok_live"] = replace(accounts["tiktok_live"], enabled=True)

    assert duplicate_channels(accounts.values()) == frozenset({"tiktok_main", "tiktok_live"})


def test_a_second_profile_is_a_different_channel() -> None:
    accounts: list[Account] = list(production_accounts().values())
    accounts[1] = replace(accounts[1], enabled=True, upload_post_user="live-profile")

    assert duplicate_channels(accounts) == frozenset()


def test_times_in_decisions_are_utc() -> None:
    paused = acct(paused_until=datetime(2026, 11, 20, 15, 0, tzinfo=timezone(timedelta(hours=2))))
    decision = check_gate(paused, Lane.HOURLY, T0.astimezone(UTC), make_params())
    assert decision.code is Reason.ACCOUNT_PAUSED  # 13:00 UTC is after 12:00 UTC

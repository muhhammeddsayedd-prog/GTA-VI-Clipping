"""YouTube lane tuning C: hourly length per platform (`gate.hourly_len_by_platform`) in posting."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from posting_helpers import DEFAULT_SETTINGS, acct

from sixstars.lengths import LengthWindow, overrides, union, window_for
from sixstars.posting.models import Account, Clip, Lane, Platform
from sixstars.posting.params import PostingParams, with_cut_minimum
from sixstars.posting.selection import family_block

OVERRIDE = {"youtube": {"min_len": 8, "max_len": 30}}
GLOBAL = LengthWindow(20, 60)
NOW = datetime(2026, 11, 21, 14, 2, tzinfo=UTC)


# ── lengths ───────────────────────────────────────────────────────────────────────────────────────────────
def test_a_platform_uses_its_override_or_the_global_window() -> None:
    assert window_for("youtube", GLOBAL, OVERRIDE) == LengthWindow(8, 30)
    assert window_for("instagram", GLOBAL, OVERRIDE) == GLOBAL
    assert window_for("youtube", GLOBAL, None) == GLOBAL


def test_the_union_is_the_widest_window() -> None:
    assert union(GLOBAL, OVERRIDE) == LengthWindow(8, 60)
    assert union(GLOBAL, {"youtube": {"min_len": 25, "max_len": 90}}) == LengthWindow(20, 90)
    assert union(GLOBAL, None) == GLOBAL


@pytest.mark.parametrize(
    "raw",
    [
        "nope",
        {"youtube": "8-30"},
        {"youtube": {"min_len": 30, "max_len": 8}},
        {"youtube": {"min_len": True, "max_len": 30}},
        {"youtube": {"min_len": -1, "max_len": 30}},
        {"youtube": {"max_len": 30}},
    ],
)
def test_a_malformed_override_is_ignored(raw: object) -> None:
    assert overrides(raw) == {}


# ── posting sends a short hourly clip only where it fits ──────────────────────────────────────────────────
def params(**changes: object) -> PostingParams:
    values = {
        **DEFAULT_SETTINGS,
        "launch.dry_run": False,
        "gate.hourly": {"min_len": 20, "max_len": 60},
        "gate.hourly_len_by_platform": OVERRIDE,
    }
    return replace(PostingParams.from_settings(values, "production"), **changes)  # type: ignore[arg-type]


def account(platform: Platform) -> Account:
    return acct({"youtube": "youtube_main", "instagram": "instagram_main"}[platform.value])


def clip(duration_s: float, *, profile: str = "hourly", family: str | None = None) -> Clip:
    return Clip(
        id="c1",
        creator_id="cr",
        lane=Lane.HOURLY,
        rank_score=5.0,
        approved_at=NOW - timedelta(hours=1),
        duration_s=duration_s,
        gate_profile=profile,
        family_id=family,
    )


@pytest.mark.parametrize(
    ("duration", "youtube_ok", "instagram_ok"),
    [
        (7.9, False, False),
        (8, True, False),
        (18, True, False),
        (25, True, True),
        (30, True, True),
        (31, False, True),
    ],
)
def test_each_platform_gets_only_what_fits_its_window(
    duration: float, youtube_ok: bool, instagram_ok: bool
) -> None:
    p = params()
    yt = family_block(clip(duration), account(Platform.YOUTUBE), [], False, NOW, p)
    ig = family_block(clip(duration), account(Platform.INSTAGRAM), [], False, NOW, p)
    assert (not yt, not ig) == (youtube_ok, instagram_ok)


def test_spike_clips_and_story_cuts_are_not_judged_by_the_hourly_window() -> None:
    p = params()
    yt = account(Platform.YOUTUBE)
    assert not family_block(clip(45, profile="express"), yt, [], False, NOW, p)
    assert not family_block(clip(90, family="parent"), yt, [], False, NOW, p)


def test_the_end_card_counts() -> None:
    p = params(end_card_s=1.5)
    assert family_block(clip(29), account(Platform.YOUTUBE), [], False, NOW, p)


def test_without_the_setting_posting_does_not_check_length() -> None:
    p = replace(params(), hourly_len={})
    assert not family_block(clip(5), account(Platform.YOUTUBE), [], False, NOW, p)


def test_the_cut_adaptive_minimum_keeps_instagram_as_the_gate_judged_it() -> None:
    values = {
        **DEFAULT_SETTINGS,
        "gate.hourly": {"min_len": 20, "max_len": 60},
        "gate.hourly_len_by_platform": OVERRIDE,
    }
    lowered = PostingParams.from_settings(with_cut_minimum(values, 6.0), "production")

    assert lowered.hourly_len[Platform.INSTAGRAM] == LengthWindow(
        6, 60
    )  # what the gate approves with the flag
    assert lowered.hourly_len[Platform.YOUTUBE] == LengthWindow(8, 30)
    assert with_cut_minimum(values, None) == values

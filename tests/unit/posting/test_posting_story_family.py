"""Posting a family of clips (development-plan/M18 Phase C): a short clip and its story cut go to one
account at least `posting.sibling_gap_h` apart, and no clip goes to a platform it is too long for."""

from datetime import UTC, datetime, timedelta

from posting_helpers import DEFAULT_SETTINGS, make_params, production_accounts

from sixstars.posting.models import Clip, Lane, Platform, PostRecord
from sixstars.posting.params import PostingParams
from sixstars.posting.selection import family_block, pick_clip

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


# ── posting a family of clips ────────────────────────────────


def record(clip_id: str, family: str, account: str, hours_ago: float) -> PostRecord:
    return PostRecord(
        account_id=account,
        platform=Platform.INSTAGRAM,
        clip_id=clip_id,
        lane=Lane.HOURLY,
        status="published",
        created_at=NOW - timedelta(hours=hours_ago),
        published_at=NOW - timedelta(hours=hours_ago),
        creator_id="other-creator",
        family_id=family,
    )


def test_the_story_of_a_posted_clip_waits_out_the_sibling_gap() -> None:
    params = make_params(sibling_gap_h=12.0)
    account = production_accounts()["instagram_main"]
    story = Clip(
        "story-1", "creator", Lane.HOURLY, 9.0, approved_at=NOW, family_id="short-1", duration_s=120.0
    )
    recent = [record("short-1", "short-1", "instagram_main", hours_ago=3)]
    assert family_block(story, account, recent, False, NOW, params)
    assert pick_clip(account, [story], recent, False, NOW, params) is None
    old = [record("short-1", "short-1", "instagram_main", hours_ago=13)]
    assert not family_block(story, account, old, False, NOW, params)
    other_account = [record("short-1", "short-1", "youtube_main", hours_ago=1)]
    assert not family_block(story, account, other_account, False, NOW, params)
    assert not family_block(story, account, recent, False, None, None), "no rules without params"


def test_a_clip_longer_than_the_platform_takes_is_not_posted_there() -> None:
    params = make_params(max_duration_s={Platform.YOUTUBE: 180.0, Platform.TIKTOK: 600.0})
    accounts = production_accounts()
    long_clip = Clip("c", "creator", Lane.HOURLY, 9.0, approved_at=NOW, duration_s=200.0)
    assert family_block(long_clip, accounts["youtube_main"], [], False, NOW, params)
    assert not family_block(long_clip, accounts["tiktok_main"], [], False, NOW, params)
    assert not family_block(long_clip, accounts["instagram_main"], [], False, NOW, params), "no limit set"
    assert params.too_long(Platform.YOUTUBE, 181.0) and not params.too_long(Platform.YOUTUBE, None)


def test_posting_params_read_the_phase_c_settings() -> None:
    params = make_params()
    assert params.sibling_gap_h == 12.0 and dict(params.max_duration_s) == {}
    values = {
        **DEFAULT_SETTINGS,
        "launch.dry_run": False,
        "posting.sibling_gap_h": 6,
        "posting.max_duration_s": {"tiktok": 600, "instagram": 900, "youtube": 180, "myspace": 5},
    }
    read = PostingParams.from_settings(values, "production")
    assert read.sibling_gap_h == 6.0
    assert dict(read.max_duration_s) == {
        Platform.TIKTOK: 600.0,
        Platform.INSTAGRAM: 900.0,
        Platform.YOUTUBE: 180.0,
    }

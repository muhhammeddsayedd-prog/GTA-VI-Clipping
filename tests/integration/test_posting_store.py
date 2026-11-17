"""The posting store against Postgres: migration 0005, the seeds, the loaders and the account pause."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from posting_support import (
    IG,
    MIGRATION_0005,
    NOW,
    T0,
    TT,
    TT_LIVE,
    YT,
    account_row,
    add_clip,
    add_creator,
    add_post,
    add_spike_clip,
    fixed,
    go_live,
    posts,
    production,
    staging,
    use_env,
    video,
)

from sixstars import db, settings
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.clients.upload_post import RateLimited, UploadStatus
from sixstars.posting.hourly import run_hourly_slot
from sixstars.posting.models import Lane, Platform, Reason
from sixstars.posting.publisher import Outcome
from sixstars.posting.store import DbStore, app_env

__all__ = ["production", "staging", "video"]  # fixtures


# ── migration 0005 and the seeds ──────────────────────────────────────────────────────────────────────────
def test_migration_0005_adds_the_two_settings() -> None:
    assert settings.get("posting.platform_max_24h") == {"tiktok": 15, "instagram": 50, "youtube": 30}
    assert settings.get("posting.staging_allow_instagram") is False


@pytest.mark.parametrize("name", ["production", "staging"])
def test_both_seeds_give_youtube_a_cap_of_28_with_4_reserved(
    name: str, request: pytest.FixtureRequest
) -> None:
    request.getfixturevalue(name)

    row = account_row(YT)
    assert (row["cap_24h"], row["spike_reserve"]) == (28, 4)


def test_migration_0005_lowers_an_old_seeded_youtube_cap_and_keeps_a_learned_one(production: None) -> None:
    db.execute("update platform_accounts set cap_24h = 40, spike_reserve = 8 where id = %s", (YT,))
    db.execute(MIGRATION_0005.read_bytes())  # type: ignore[arg-type]
    assert (account_row(YT)["cap_24h"], account_row(YT)["spike_reserve"]) == (28, 4)

    db.execute("update platform_accounts set cap_24h = 26 where id = %s", (YT,))
    db.execute(MIGRATION_0005.read_bytes())  # type: ignore[arg-type]
    assert account_row(YT)["cap_24h"] == 26


# ── loaders ───────────────────────────────────────────────────────────────────────────────────────────────
def test_params_come_from_the_settings_table_and_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    use_env(monkeypatch, "production")
    params = DbStore().load_params()

    assert params.app_env == "production" and params.dry_run is True and params.emergency_stop is False
    assert params.launch_at.isoformat() == "2026-11-18T11:00:00+00:00"
    assert params.platform_max_24h[Platform.YOUTUBE] == 30 and params.min_gap_s == 900


def test_params_are_read_fresh_not_from_the_settings_cache(production: None) -> None:
    store = DbStore()
    assert store.load_params().emergency_stop is False
    db.execute("update settings set value = 'true'::jsonb where key = 'control.emergency_stop'")

    assert store.load_params().emergency_stop is True


@pytest.mark.parametrize(("env_name", "expected"), [("test", "staging"), ("dev", "staging")])
def test_an_environment_that_is_not_production_gets_the_staging_rules(
    monkeypatch: pytest.MonkeyPatch, env_name: str, expected: str
) -> None:
    use_env(monkeypatch, env_name)
    assert app_env() == expected


def test_a_missing_required_setting_stops_the_loader(production: None) -> None:
    db.execute("delete from settings where key = 'posting.platform_max_24h'")

    with pytest.raises(KeyError):
        DbStore().load_params()


def test_accounts_load_with_lanes_hours_and_pause(production: None) -> None:
    db.execute("update platform_accounts set paused_until = %s where id = %s", (NOW, IG))
    accounts = {account.id: account for account in DbStore().load_accounts()}

    tiktok = accounts[TT]
    assert tiktok.platform is Platform.TIKTOK and tiktok.lanes == frozenset({Lane.HOURLY})
    assert tiktok.hourly_hours_utc == frozenset({0, 1, 2, 11, 12, 14, 15, 17, 18, 19, 20, 21, 22, 23})
    assert (tiktok.cap_24h, tiktok.spike_reserve, tiktok.min_gap_s) == (14, 0, 900)
    assert tiktok.privacy == "PUBLIC_TO_EVERYONE" and tiktok.upload_post_user == "default"
    assert accounts[TT_LIVE].enabled is False
    assert accounts[IG].paused_until == NOW and accounts[IG].hourly_hours_utc is None
    assert accounts[IG].lanes == frozenset({Lane.HOURLY, Lane.SPIKE}) and accounts[IG].privacy is None


def test_clips_load_with_creator_handle_and_spike_time(production: None, video: Path) -> None:
    creator = add_creator("LiveStreamer")
    spike_at = NOW - timedelta(minutes=4)
    spike = add_spike_clip(creator, video, spike_wall=spike_at, rank=9.5)
    low = add_clip(creator, video, rank=2.0)
    high = add_clip(creator, video, rank=7.25)
    add_clip(creator, video, rank=99.0, status="rejected")
    store = DbStore()

    queue = store.load_hourly_queue()
    assert [item.clip.id for item in queue] == [high, low]
    top = queue[0]
    assert top.clip.rank_score == 7.25 and top.clip.creator_handle == "LiveStreamer"
    assert top.clip.creator_id == creator and top.clip.hashtags == ("#stunt",)
    assert top.clip.approved_at == NOW - timedelta(hours=1) and top.local_path == str(video)
    (loaded,) = store.load_spike_clips()
    assert loaded.clip.id == spike and loaded.clip.spike_at == spike_at and loaded.clip.lane is Lane.SPIKE
    assert store.get_clip("00000000-0000-0000-0000-000000000000") is None


def test_history_has_the_last_day_the_last_hourly_post_and_the_asked_clips(
    production: None, video: Path
) -> None:
    creator = add_creator()
    recent, old_hourly, older_hourly, old_spike, asked = (add_clip(creator, video) for _ in range(5))
    add_post(recent, IG, NOW - timedelta(hours=2))
    add_post(old_hourly, YT, NOW - timedelta(days=3))  # the latest hourly post of youtube_main
    add_post(older_hourly, YT, NOW - timedelta(days=4))
    add_post(old_spike, IG, NOW - timedelta(days=3), lane="spike")
    add_post(asked, TT, NOW - timedelta(days=9))
    store = DbStore()

    plain = {(r.clip_id, r.account_id) for r in store.load_history(NOW)}
    with_clip = {(r.clip_id, r.account_id) for r in store.load_history(NOW, [asked])}

    assert plain == {(recent, IG), (old_hourly, YT), (asked, TT)}  # tiktok_main's latest hourly post
    assert with_clip == plain
    record = next(r for r in store.load_history(NOW) if r.clip_id == recent)
    assert record.creator_id == creator and record.platform is Platform.INSTAGRAM
    assert record.published_at == NOW - timedelta(hours=2) and record.status == "published"


def test_history_brings_every_post_of_an_asked_clip(production: None, video: Path) -> None:
    creator = add_creator()
    asked, other = add_clip(creator, video), add_clip(creator, video)
    add_post(asked, IG, NOW - timedelta(days=9), lane="spike")
    add_post(other, IG, NOW - timedelta(days=8), lane="spike")
    store = DbStore()

    assert store.load_history(NOW) == []
    assert [r.clip_id for r in store.load_history(NOW, [asked])] == [asked]


# ── rate limit ────────────────────────────────────────────────────────────────────────────────────────────
def test_a_rate_limit_pause_and_the_learned_cap_are_stored_and_respected(
    production: None, video: Path
) -> None:
    go_live()
    clip = add_clip(add_creator(), video)
    second = add_clip(add_creator("Other"), video, rank=1.0)

    class YoutubeLimited(FakeUploadPostClient):
        def upload_video(self, user: str, platform: str, *args: Any, **kwargs: Any) -> UploadStatus:
            if platform == "youtube":
                raise RateLimited("default/youtube", None, "youtube_channel_daily_limit")
            return super().upload_video(user, platform, *args, **kwargs)

    run = run_hourly_slot(T0, YoutubeLimited(), clock=fixed())

    by_account = {row["account_id"]: row for row in posts()}
    assert by_account[YT]["status"] == "failed" and by_account[YT]["error"].startswith("rate_limited")
    assert by_account[IG]["status"] == "published"
    assert {r.outcome for r in run.results} == {Outcome.PUBLISHED, Outcome.FAILED}
    youtube = account_row(YT)
    assert youtube["cap_24h"] == 26 and youtube["paused_until"] == NOW + timedelta(hours=24)
    assert "rate limit" in youtube["pause_reason"]
    warn = db.fetch_one("select * from notify_outbox where title = %s", (f"Rate limit on {YT}",))
    assert warn is not None and warn["severity"] == "warn"
    event = db.fetch_one("select * from events where type = 'account.rate_limited'")
    assert event is not None and event["entity_id"] == YT and event["level"] == "warn"

    later = T0 + timedelta(hours=1)
    again = run_hourly_slot(later, FakeUploadPostClient(), clock=fixed(later + timedelta(minutes=1)))
    assert again.plan is not None
    codes = {d.account_id: d.code for d in again.plan.decisions}
    assert codes[YT] is Reason.ACCOUNT_PAUSED
    assert [row["clip"] for row in posts("account_id = %s", (YT,))] == [clip]
    assert second in {row["clip"] for row in posts("account_id = %s", (IG,))}

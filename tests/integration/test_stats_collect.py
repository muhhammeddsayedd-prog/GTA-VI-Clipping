"""Stats collection and the daily report against Postgres, with fake platforms."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from guardian_support import IG, TT, YT, UploadPost, YouTube, creator, now, published, row, world
from posting_support import add_clip
from psycopg.types.json import Jsonb

from sixstars import clock, db, settings, usage
from sixstars.clients.upload_post import RateLimited
from sixstars.stats import collector, report

__all__ = ["world"]  # fixture

# Profile analytics in the real shape (profile `default`, read 2026-11-17; numbers replaced).
PROFILE = {
    "tiktok": {
        "followers": 1240,
        "reach": 150000,
        "impressions": 162000,
        "profileViews": 900,
        "reach_timeseries": [{"date": "2026-11-18", "value": 5}],
        "metric_type": "views",
        "primary_impressions_field": "impressions",
    },
    "instagram": {
        "followers": 820,
        "reach": 90000.0,
        "views": 150000.0,
        "impressions": 150000.0,
        "follower_demographics": {},
        "metric_type": "reach",
        "primary_impressions_field": "reach",
    },
    "youtube": {
        "followers": 1050,
        "reach": 100000,
        "impressions": 100000,
        "stale": True,
        "stale_reason": "quota",
        "metric_type": "views",
        "primary_impressions_field": "impressions",
    },
}


def clients() -> tuple[UploadPost, YouTube]:
    upload_post = UploadPost()
    upload_post.analytics = PROFILE
    return upload_post, YouTube()


def snapshots() -> list[dict[str, Any]]:
    return db.fetch_all("select * from account_stats order by account_id, captured_at")


def test_one_pass_stores_account_snapshots_and_post_numbers(world: None) -> None:
    star = creator("Star")
    yt = published(star, YT, "ytSTATS0001")
    tt = published(star, TT, "7300000000000000001")
    ig = published(star, IG, "17900000000000001")
    old = published(star, YT, "ytOLD000001", age=timedelta(days=8))
    dry = published(star, IG, "ig-dry", dry_run=True, status="skipped")
    upload_post, youtube = clients()
    upload_post.metrics["7300000000000000001"] = {"views": 9000, "likes": 700, "comments": 30, "shares": 12}
    upload_post.metrics["17900000000000001"] = {"play_count": 4000, "like_count": 150}
    youtube.items["ytSTATS0001"] = {
        "statistics": {"viewCount": "5200", "likeCount": "210", "commentCount": "9"}
    }

    result = collector.collect(upload_post, youtube, now())

    assert result.as_dict() == {
        "status": "ok",
        "accounts": 3,
        "posts": 3,
        "skipped": 0,
        "rate_limited": False,
        "snapshots": 0,
        "snapshots_left": 0,
        "handed_to_job": False,
        "errors": [],
    }
    stored = {
        s["account_id"]: (s["followers"], s["views_total"], s["reach"], s["impressions"]) for s in snapshots()
    }
    assert stored == {
        IG: (820, 150000, 90000, 150000),
        TT: (1240, 162000, 150000, 162000),  # TikTok has no `views`: the impressions are the views
        YT: (1050, 100000, 100000, 100000),
    }  # the disabled second TikTok account gets no snapshot
    raw = snapshots()[0]["raw"]
    assert raw["metric_type"] == "reach" and "follower_demographics" not in raw
    assert upload_post.calls_to("get_profile_analytics") == [
        {"user": "default", "platforms": ["instagram", "tiktok", "youtube"]}
    ]

    assert [row("posts", yt)[key] for key in ("views", "likes", "comments", "shares")] == [5200, 210, 9, None]
    assert [row("posts", tt)[key] for key in ("views", "likes", "comments", "shares")] == [9000, 700, 30, 12]
    assert [row("posts", ig)[key] for key in ("views", "likes")] == [4000, 150]
    assert row("posts", yt)["stats_updated_at"] is not None
    assert row("posts", old)["views"] is None and row("posts", dry)["views"] is None
    assert youtube.calls_to("videos_list") == [{"ids": ["ytSTATS0001"], "parts": ["statistics"]}]
    assert float(row("creators", star)["avg_clip_views"]) == pytest.approx((5200 + 9000 + 4000) / 3)
    assert upload_post.uploads == {} and upload_post.deleted == []  # read-only


def test_a_repeated_trigger_stores_nothing_twice(world: None) -> None:
    star = creator("Star")
    yt = published(star, YT, "ytSTATS0001")
    upload_post, youtube = clients()

    collector.collect(upload_post, youtube, now())
    again = collector.collect(upload_post, youtube, now())

    assert again.accounts == 0 and again.posts == 1  # post numbers are simply overwritten
    assert len(snapshots()) == 3 and len(upload_post.calls_to("get_profile_analytics")) == 1
    assert row("posts", yt)["views"] == 5000

    settings.set("stats.account_min_interval_s", 0)
    assert collector.collect(upload_post, youtube, now()).accounts == 3 and len(snapshots()) == 6


def test_a_rate_limit_ends_the_pass_and_keeps_what_was_read(world: None) -> None:
    star = creator("Star")
    first = published(star, TT, "7300000000000000001", age=timedelta(hours=1))
    second = published(star, TT, "7300000000000000002", age=timedelta(hours=2))
    upload_post, youtube = clients()
    calls = {"n": 0}
    original = upload_post.get_post_analytics_by_platform_id

    def limited(user: str, platform: str, platform_post_id: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RateLimited("default/tiktok", 300.0, "100 requests per 5 minutes")
        return original(user, platform, platform_post_id)

    upload_post.get_post_analytics_by_platform_id = limited  # type: ignore[method-assign]

    result = collector.collect(upload_post, youtube, now())

    assert result.rate_limited is True and result.posts == 1 and result.skipped == 1
    assert result.errors == ["post analytics: rate limited"]
    assert row("posts", first)["views"] == 1000 and row("posts", second)["views"] is None

    # The next pass starts with the post that has no numbers yet.
    upload_post.get_post_analytics_by_platform_id = original  # type: ignore[method-assign]
    upload_post.calls.clear()
    collector.collect(upload_post, youtube, now())
    assert upload_post.calls_to("by_platform_id")[0]["platform_post_id"] == "7300000000000000002"
    assert row("posts", second)["views"] == 1000


def test_the_time_budget_and_the_post_limit_bound_a_pass(world: None) -> None:
    star = creator("Star")
    for index in range(5):
        published(star, TT, f"730000000000000000{index}")
    upload_post, youtube = clients()

    settings.set("stats.max_posts_per_run", 2)
    assert collector.collect(upload_post, youtube, now()).posts == 2

    settings.set("stats.max_posts_per_run", 150)
    ticks = iter([0.0, 1.0, 25.0, 30.0, 35.0, 40.0, 45.0])
    result = collector.collect(upload_post, youtube, now(), clock=lambda: next(ticks))
    assert result.posts == 1 and result.skipped == 4 and "time budget" in result.errors[0]


def test_a_removed_video_and_a_missing_api_key_do_not_break_the_pass(world: None) -> None:
    star = creator("Star")
    gone = published(star, YT, "ytGONE00001")
    published(star, TT, "7300000000000000001")
    upload_post, youtube = clients()
    youtube.missing = {"ytGONE00001"}
    upload_post.gone = {"7300000000000000001"}

    result = collector.collect(upload_post, youtube, now())

    assert result.posts == 0 and result.skipped == 2 and result.errors == []
    assert row("posts", gone)["views"] is None and row("posts", gone)["status"] == "published"


# ── the daily report ──────────────────────────────────────────────────────────────────────────────────────

DAY = date(2026, 11, 19)
NOON = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
NEXT_DAY = datetime(2026, 11, 20, 0, 0, 5, tzinfo=UTC)


def post_at(creator_id: str, account: str, post_id: str, at: datetime, **more: Any) -> str:
    created = published(creator_id, account, post_id, **more)
    db.execute(
        "update posts set created_at = %s, published_at = %s, "
        "slot_at = date_trunc('hour', %s::timestamptz) where id = %s",
        (at, at, at, created),
    )
    return created


def snapshot(account: str, at: datetime, followers: int, views: int) -> None:
    db.execute(
        "insert into account_stats (account_id, captured_at, followers, views_total) values (%s, %s, %s, %s)",
        (account, at, followers, views),
    )


def test_the_report_counts_the_day_from_the_database(world: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "app_zone", lambda: UTC)  # the data below is laid out on the UTC day
    star = creator("Star")
    db.execute("update creators set discovered_at = %s", (NOON,))
    post_at(star, YT, "yt1", NOON, views=88000, title="He did NOT expect that <3 & more")
    post_at(star, YT, "yt2", NOON + timedelta(hours=1), views=2000)
    post_at(star, TT, "tt1", NOON, views=6100, lane="spike")
    post_at(star, IG, "ig-failed", NOON, status="failed")
    post_at(star, IG, "ig-yesterday", NOON - timedelta(days=1), views=50)
    post_at(star, IG, "ig-today", NEXT_DAY, views=999_999)  # after the day: not in this report
    manual = post_at(star, TT, "tt-manual", NOON, status="remove_failed")
    for account, before, after, views_before, views_after in [
        (YT, 450, 1050, 50000, 100000),
        (TT, 600, 1240, 92000, 162000),
        (IG, 430, 820, 90000, 150000),
    ]:
        snapshot(account, NOON - timedelta(days=1), before, views_before)
        snapshot(account, NOON + timedelta(hours=10), after, views_after)
    for reason in ("music: ratio 0.7", "music: ratio 0.9", "score: 5.1 < 7"):
        db.execute(
            "insert into clips (creator_id, source_platform, source_id, source_url, source_type, lane, "
            "start_s, end_s, status, reject_reason, created_at, updated_at) "
            "values (%s, 'youtube', 'v', 'https://x.test', 'vod', 'hourly', 0, 30, 'rejected', %s, %s, %s)",
            (star, reason, NOON, NOON),
        )
    add_clip(star, None, status="approved", approved_at=NOON)
    for tier, latency in [("S", 190), ("S", 250), ("A", None), ("rejected", None)]:
        db.execute(
            "insert into spikes (mode, creator_id, tier, spike_to_post_s, detected_at) "
            "values ('live', %s, %s, %s, %s)",
            (star, tier, latency, NOON),
        )
    db.execute(
        "insert into takedowns (source, is_takedown, status, received_at, actions) values "
        "('claim', true, 'actioned', %(at)s, %(strike)s), ('email', false, 'ignored', %(at)s, null), "
        "('email', true, 'open', %(at)s, null)",
        {"at": NOON, "strike": Jsonb({"strike": True})},
    )
    usage.record("anthropic", "clip_writer", cost_usd=14.2)
    db.execute("update api_usage set created_at = %s", (NOON,))
    db.execute(
        "insert into events (type, level, message, created_at) values "
        "('account.rate_limited', 'warn', 'youtube_main rate limited: cap 28 -> 26', %s), "
        "('post.published', 'info', 'not an issue', %s)",
        (NOON, NOON),
    )

    data = report.build(DAY, NEXT_DAY)

    assert (data["day"], data["timezone"], data["env"]) == ("2026-11-19", "UTC", "production")
    accounts = {item["account_id"]: item for item in data["accounts"]}
    assert (accounts[YT]["followers"], accounts[YT]["followers_change"], accounts[YT]["published"]) == (
        1050,
        600,
        2,
    )
    assert (accounts[TT]["followers_change"], accounts[TT]["published"]) == (
        640,
        2,
    )  # the flagged post was posted
    assert (accounts[IG]["published"], accounts[IG]["failed"]) == (0, 1)
    assert accounts["tiktok_live"]["enabled"] is False and accounts["tiktok_live"]["followers"] is None
    assert data["views"]["total"] == 412000 and data["views"]["change"] == 180000
    assert data["views"]["median_by_platform"] == {"instagram": 50, "tiktok": 6100, "youtube": 45000}
    assert [(clip["views"], clip["platform"]) for clip in data["top_clips"]] == [
        (88000, "youtube"),
        (6100, "tiktok"),
        (2000, "youtube"),
    ]
    assert data["ypp"] == {
        "subscribers": 1050,
        "subscribers_target": 1000,
        "shorts_views_90d": 90000,
        "shorts_views_target": 10000000,
    }
    assert data["posts"] == {
        "hourly_published": 3,
        "spike_published": 1,
        "failed": 1,
        "dry_run": 0,
        "slots_used": 2,
        "slots_skipped_events": 0,
    }
    assert data["spikes"] == {
        "detected": 4,
        "S": 2,
        "A": 1,
        "B": 0,
        "rejected": 1,
        "median_spike_to_post_s": 220,
    }
    assert data["clips"]["rejected"] == 3 and data["clips"]["rejected_by_reason"] == {"music": 2, "score": 1}
    assert data["clips"]["approved"] == 1 and data["clips"]["queue"]["hourly"] == 1
    assert data["discovery"]["new_creators"] == 1 and data["discovery"]["new_by_tier"] == {"auto": 1}
    guardian = data["guardian"]
    assert (
        guardian["claims"],
        guardian["takedowns"],
        guardian["emails_ignored"],
        guardian["emails_open"],
    ) == (1, 0, 1, 1)
    assert guardian["strike_limit"] == 2 and guardian["emergency_stop"] is False
    assert guardian["manual_removals"] == [
        {"platform": "tiktok", "post_url": row("posts", manual)["post_url"]}
    ]
    assert data["spend"][0] == {"provider": "anthropic", "spent_usd": 14.2, "budget_usd": 25.0}
    assert data["issues"] == ["12:00 youtube_main rate limited: cap 28 -> 26"]

    text = report.render_telegram(data)
    assert len(text) <= report.TELEGRAM_MAX_CHARS
    assert "“He did NOT expect that &lt;3 &amp; more” — 88k views (YT)" in text
    assert "youtube_main 1.1k (+600)" in text and "strikes" in text
    assert "1 post(s) still need manual removal" in text and "$14.20/$25" in text


def test_an_empty_database_gives_a_report_of_zeros(world: None) -> None:
    data = report.build(now=NEXT_DAY)
    assert data["day"] == "2026-11-19"  # the day that just ended
    assert data["posts"]["hourly_published"] == 0 and data["views"]["total"] is None
    assert data["top_clips"] == [] and data["guardian"]["manual_removals"] == []
    text = report.render_telegram(data)
    assert "daily report (2026-11-19 Cairo, production)" in text and "Posts  hourly 0" in text
    assert data["timezone"] == "Africa/Cairo"


def test_the_report_day_runs_midnight_to_midnight_in_cairo(world: None) -> None:
    star = creator("Star")
    post_at(star, YT, "yt-in", datetime(2026, 11, 19, 21, 30, tzinfo=UTC))  # 23:30 Cairo, 19 Nov
    post_at(star, YT, "yt-out", datetime(2026, 11, 19, 22, 30, tzinfo=UTC))  # 00:30 Cairo, 20 Nov
    post_at(star, YT, "yt-early", datetime(2026, 11, 18, 22, 30, tzinfo=UTC))  # 00:30 Cairo, 19 Nov
    data = report.build(now=datetime(2026, 11, 19, 22, 5, tzinfo=UTC))  # 00:05 Cairo: 19 Nov just ended
    assert data["day"] == "2026-11-19"
    assert {item["account_id"]: item["published"] for item in data["accounts"]}[YT] == 2

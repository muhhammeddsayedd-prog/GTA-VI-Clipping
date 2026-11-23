"""Fixed-age snapshots: due windows and parsing of real Upload-Post answers (docs/28 Part A, L1)."""

from datetime import UTC, datetime, timedelta

import httpx
import respx

from sixstars.clients.youtube_analytics import REPORTS_URL, YouTubeAnalyticsClient, parse_report
from sixstars.learning.snapshots import (
    DuePost,
    SnapshotNumbers,
    SnapshotRules,
    due_readings,
    merge,
    oldest_target_window_h,
    parse_snapshot,
)
from sixstars.stats.snapshot_pass import TokenBucket

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
RULES = SnapshotRules()

# Real post_metrics (production, 19 Nov 2026), trimmed.
TIKTOK = {
    "platforms": {
        "tiktok": {
            "platform_post_id": "7709696982310276359",
            "post_metrics": {
                "views": 120,
                "likes": 7,
                "comments": 1,
                "shares": 2,
                "reach": 110,
                "favorites": 3,
                "new_followers": 1,
                "profile_views": 0,
                "full_video_watched_rate": 0.31,
                "average_time_watched": 6.2,
                "total_time_watched": 744,
                "impression_sources": [
                    {"impression_source": "For You", "percentage": 0.9},
                    {"impression_source": "Follow", "percentage": 0.1},
                ],
                "audience_types": [{"percentage": 0.95, "type": "NEW_VIEWER"}],
                "avg_watch_seconds": 6.2,
                "watch_time_seconds": 744.0,
                "completion_rate": 0.31,
            },
        }
    }
}
INSTAGRAM = {
    "platforms": {
        "instagram": {
            "post_metrics": {
                "likes": 1,
                "comments": 2,
                "media_product_type": "REELS",
                "views": 263,
                "impressions": 263,
                "reach": 224,
                "saves": 0,
                "shares": 0,
                "avg_watch_seconds": 18.442,
                "watch_time_seconds": 4278.724,
            }
        }
    }
}
YOUTUBE = {
    "platforms": {"youtube": {"post_metrics": {"views": 514, "likes": 4, "comments": 1, "favorites": 0}}}
}


def at(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def test_a_post_is_due_inside_each_target_window() -> None:
    for age, target in [(1.0, 1), (1.7, 1), (3.2, 3), (7.0, 6), (29.0, 24), (80, 72), (200, 168)]:
        [reading] = due_readings([DuePost("p", "instagram", at(age))], NOW, RULES)
        assert reading.target_age_h == target, age


def test_a_post_between_windows_or_too_young_is_not_due() -> None:
    for age in (0.5, 2.0, 4.0, 10.0, 40.0, 100.0, 220.0):
        assert due_readings([DuePost("p", "instagram", at(age))], NOW, RULES) == [], age


def test_a_taken_target_is_not_due_again() -> None:
    assert due_readings([DuePost("p", "tiktok", at(3.1), frozenset({3.0}))], NOW, RULES) == []


def test_youngest_post_goes_first() -> None:
    posts = [
        DuePost("old", "tiktok", at(25)),
        DuePost("young", "tiktok", at(1.2)),
        DuePost("mid", "tiktok", at(6.5)),
    ]
    assert [r.post_id for r in due_readings(posts, NOW, RULES)] == ["young", "mid", "old"]


def test_lookback_covers_the_7_day_window() -> None:
    assert oldest_target_window_h(RULES) == 210


def test_rules_from_flag() -> None:
    rules = SnapshotRules.from_flag({"ages_h": [6, 1], "min_tolerance_h": 1, "tolerance_frac": "x"})
    assert rules.ages_h == (1.0, 6.0)
    assert rules.window(1) == (1, 2)
    assert rules.tolerance_frac == 0.25


def test_tiktok_answer_is_parsed_with_every_field() -> None:
    n = parse_snapshot(TIKTOK, "tiktok")
    assert (n.views, n.likes, n.comments, n.shares, n.saves, n.reach, n.new_followers) == (
        120,
        7,
        1,
        2,
        3,
        110,
        1,
    )
    assert n.completion_rate == 0.31
    assert n.avg_watch_s == 6.2
    assert n.total_watch_s == 744.0
    assert n.impression_sources == {"For You": 0.9, "Follow": 0.1}
    assert n.audience_types == {"NEW_VIEWER": 0.95}


def test_instagram_has_no_completion_or_followers() -> None:
    n = parse_snapshot(INSTAGRAM, "instagram")
    assert (n.views, n.reach, n.saves, n.shares) == (263, 224, 0, 0)
    assert n.avg_watch_s == 18.442
    assert n.completion_rate is None
    assert n.new_followers is None


def test_youtube_through_upload_post_has_counts_only() -> None:
    n = parse_snapshot(YOUTUBE, "youtube")
    assert (n.views, n.likes, n.comments, n.saves) == (514, 4, 1, 0)
    assert n.avg_view_pct is None and n.new_followers is None and n.shares is None


def test_missing_fields_are_none_not_zero() -> None:
    n = parse_snapshot({"platforms": {"tiktok": {"post_metrics": {"views": 5}}}}, "tiktok")
    assert n.views == 5
    assert n.likes is None and n.completion_rate is None and n.impression_sources is None


def test_an_answer_without_metrics_is_empty() -> None:
    body = {"platforms": {"tiktok": {"post_metrics_error": "TikTok post ID could not be resolved yet."}}}
    assert parse_snapshot(body, "tiktok").empty
    assert parse_snapshot(None, "tiktok").empty


def test_a_percentage_rate_is_turned_into_a_fraction() -> None:
    n = parse_snapshot({"post_metrics": {"full_video_watched_rate": 42.0}}, "tiktok")
    assert n.completion_rate == 0.42


def test_merge_fills_only_missing_fields() -> None:
    merged = merge(SnapshotNumbers(views=10), SnapshotNumbers(views=99, new_followers=2, avg_view_pct=0.6))
    assert (merged.views, merged.new_followers, merged.avg_view_pct) == (10, 2, 0.6)


def test_token_bucket_paces_calls() -> None:
    clock = [0.0]
    bucket = TokenBucket(2, 0.5, lambda: clock[0])
    assert bucket.try_take() and bucket.try_take()
    assert not bucket.try_take()
    clock[0] = 2.0
    assert bucket.try_take()


REPORT = {
    "kind": "youtubeAnalytics#resultTable",
    "columnHeaders": [
        {"name": "video", "columnType": "DIMENSION", "dataType": "STRING"},
        {"name": "views", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "averageViewPercentage", "columnType": "METRIC", "dataType": "FLOAT"},
        {"name": "averageViewDuration", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "subscribersGained", "columnType": "METRIC", "dataType": "INTEGER"},
        {"name": "estimatedMinutesWatched", "columnType": "METRIC", "dataType": "INTEGER"},
    ],
    "rows": [["abc", 514, 87.5, 12, 3, 100]],
}


def test_analytics_report_is_parsed() -> None:
    [row] = parse_report(REPORT).values()
    assert row.video_id == "abc"
    assert (row.views, row.avg_view_pct, row.avg_view_duration_s, row.subscribers_gained) == (
        514,
        0.875,
        12,
        3,
    )
    assert parse_report({"columnHeaders": REPORT["columnHeaders"]}) == {}  # no rows: no data yet


@respx.mock
def test_analytics_client_sends_the_documented_query() -> None:
    from sixstars.clients.youtube import YouTubeClient

    youtube = YouTubeClient(
        api_key="k", oauth_client_id="c", oauth_client_secret="s", oauth_refresh_token="r"
    )
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )
    route = respx.get(REPORTS_URL).mock(return_value=httpx.Response(200, json=REPORT))

    out = YouTubeAnalyticsClient(youtube).video_report(["abc", "def"], NOW.date(), NOW.date())

    assert out["abc"].subscribers_gained == 3
    params = route.calls[0].request.url.params
    assert params["ids"] == "channel==MINE"
    assert params["dimensions"] == "video"
    assert params["filters"] == "video==abc,def"
    assert route.calls[0].request.headers["Authorization"] == "Bearer t"

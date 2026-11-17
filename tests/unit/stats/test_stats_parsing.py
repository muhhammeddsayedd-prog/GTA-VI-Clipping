"""Parsing of the analytics answers. The profile shapes below are the real ones of profile `default`
(read 2026-11-17, values replaced); the per-post metric names are not confirmed by a real post yet, so
the parser accepts the usual spellings."""

from typing import Any

import pytest

from sixstars.stats import collector
from sixstars.stats.collector import PostNumbers

TIKTOK = {
    "followers": 1200,
    "reach": 45000,
    "impressions": 51000,
    "profileViews": 300,
    "reach_timeseries": [{"date": "2026-10-19", "value": 10}] * 30,
    "likes": 900,
    "comments": 40,
    "shares": 12,
    "video_count": 55,
    "following": 3,
    "metric_type": "views",
    "available_metrics": ["followers", "reach"],
    "metric_labels": {"impressions": "Video views", "profileViews": "Profile views"},
    "primary_impressions_field": "impressions",
}
INSTAGRAM = {
    "followers": 800,
    "reach": 9000.0,
    "views": 20000.0,
    "impressions": 20000.0,
    "profileViews": 120.0,
    "likes": 300.0,
    "reach_timeseries": [{"date": "2026-10-20", "value": 1.0}] * 28,
    "follower_demographics": {},
    "engaged_audience_demographics": {},
    "metric_type": "reach",
    "breakdowns": {"follows_and_unfollows": {}},
    "available_metrics": ["followers"],
    "metric_labels": {"reach": "Accounts reached"},
    "primary_impressions_field": "reach",
}
YOUTUBE = {
    "followers": 300,
    "reach": 15000,
    "impressions": 15000,
    "likes": 100,
    "watch_time_minutes": 4000,
    "average_view_duration_seconds": 21,
    "metric_type": "views",
    "data_source": "snapshot_deltas",
    "stale": True,
    "stale_reason": "quota",
    "reach_timeseries": [],
    "available_metrics": ["followers"],
    "metric_labels": {"followers": "Subscribers"},
    "primary_impressions_field": "impressions",
}


@pytest.mark.parametrize(
    ("body", "followers", "views", "reach", "impressions"),
    [
        (TIKTOK, 1200, 51000, 45000, 51000),  # no `views` key: the primary impressions field is the views
        (INSTAGRAM, 800, 20000, 9000, 20000),  # floats; has its own `views`
        (YOUTUBE, 300, 15000, 15000, 15000),
    ],
)
def test_real_profile_shapes(
    body: dict[str, Any], followers: int, views: int, reach: int, impressions: int
) -> None:
    numbers = collector.parse_account(body)
    assert numbers is not None
    assert (numbers.followers, numbers.views_total, numbers.reach, numbers.impressions) == (
        followers,
        views,
        reach,
        impressions,
    )
    assert all(isinstance(value, int) for value in (numbers.followers, numbers.views_total))


def test_the_stored_raw_answer_keeps_scalars_and_drops_the_bulky_parts() -> None:
    numbers = collector.parse_account(INSTAGRAM)
    assert numbers is not None
    assert numbers.raw["metric_type"] == "reach" and numbers.raw["likes"] == 300.0
    for dropped in (
        "reach_timeseries",
        "follower_demographics",
        "breakdowns",
        "metric_labels",
        "available_metrics",
    ):
        assert dropped not in numbers.raw
    youtube = collector.parse_account(YOUTUBE)
    assert youtube is not None and youtube.raw["stale"] is True


@pytest.mark.parametrize("body", [None, {}, [], "error", {"error": "not connected"}, {"followers": "n/a"}])
def test_an_entry_without_numbers_is_no_snapshot(body: Any) -> None:
    assert collector.parse_account(body) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5),
        (5.9, 5),
        ("120000", 120000),
        ("1,234", 1234),
        (" 7 ", 7),
        (0, 0),
        (-1, None),
        (True, None),
        (None, None),
        ("abc", None),
        (float("nan"), None),
        (float("inf"), None),
        ([1], None),
    ],
)
def test_counts_are_read_leniently_but_never_invented(value: Any, expected: int | None) -> None:
    assert collector.to_int(value) == expected


@pytest.mark.parametrize(
    ("body", "platform", "expected"),
    [
        (
            {
                "platforms": {
                    "tiktok": {"post_metrics": {"views": 1000, "likes": 50, "comments": 5, "shares": 2}}
                }
            },
            "tiktok",
            PostNumbers(1000, 50, 5, 2),
        ),
        (
            {
                "platforms": {
                    "instagram": {"post_metrics": {"play_count": "90", "like_count": 4, "comment_count": 0}}
                }
            },
            "instagram",
            PostNumbers(90, 4, 0, None),
        ),
        ({"post_metrics": {"viewCount": "7", "likeCount": "1"}}, "youtube", PostNumbers(7, 1, None, None)),
        (
            {"platforms": {"tiktok": {"post_metrics_error": "TikTok video not found (ID: 1)."}}},
            "tiktok",
            PostNumbers(),
        ),
        ({"platforms": {"tiktok": {"post_metrics": {"views": 1}}}}, "instagram", PostNumbers()),
        ({"platforms": {"tiktok": {"post_metrics": "broken"}}}, "tiktok", PostNumbers()),
        ({}, "tiktok", PostNumbers()),
        (None, "tiktok", PostNumbers()),
        ("text", "tiktok", PostNumbers()),
    ],
)
def test_post_metrics(body: Any, platform: str, expected: PostNumbers) -> None:
    assert collector.parse_post_metrics(body, platform) == expected


def test_youtube_statistics_arrive_as_strings() -> None:
    item = {"id": "v", "statistics": {"viewCount": "120000", "likeCount": "4000", "commentCount": "300"}}
    assert collector.parse_youtube_statistics(item) == PostNumbers(120000, 4000, 300, None)
    assert collector.parse_youtube_statistics({"id": "v"}).empty
    # Likes can be hidden by the owner of a video: the key is simply absent.
    assert collector.parse_youtube_statistics({"statistics": {"viewCount": "5"}}) == PostNumbers(
        5, None, None, None
    )


# Real post-analytics answers of the first private staging posts (2026-11-17; values trimmed).
REAL_YOUTUBE_PRIVATE_POST = {
    "success": True,
    "post": {"request_id": "post:c1:youtube_main", "profile_username": "default", "media_type": "video"},
    "platforms": {
        "youtube": {
            "success": True,
            "platform_post_id": "kwtCUdnP_Zk",
            "post_url": "Post uploaded as Private. No public URL available.",
            "error_message": None,
            "media_size_bytes": 23925395,
            "post_metrics": {
                "views": 0,
                "likes": 0,
                "comments": 0,
                "favorites": 0,
                "watch_time_minutes": 0,
                "average_view_duration_seconds": 0,
                "average_view_percentage": 0.0,
                "engaged_views": 0,
                "subscribers_gained": 0,
                "shares": 0,
            },
            "post_metrics_source": "platform_api",
        }
    },
}
REAL_TIKTOK_NO_ID_YET = {
    "success": True,
    "post": {"request_id": "post:c1:tiktok_main", "profile_username": "default", "media_type": "video"},
    "platforms": {
        "tiktok": {
            "success": True,
            "platform_post_id": None,
            "post_url": None,
            "error_message": None,
            "original_publish_id": "v_pub_url~v2.7709065962657630260",
            "post_metrics_error": "TikTok post ID could not be resolved yet. Moderation may still be in "
            "progress. Please try again later.",
            "profile_snapshot_latest": {"followers": 0, "reach": 0, "impressions": 0, "video_count": 0},
            "profile_snapshot_latest_date": "2026-11-17",
        }
    },
}


def test_real_post_analytics_answers() -> None:
    assert collector.parse_post_metrics(REAL_YOUTUBE_PRIVATE_POST, "youtube") == PostNumbers(0, 0, 0, 0)
    # The profile snapshot beside a missing post must not be read as the numbers of the post.
    assert collector.parse_post_metrics(REAL_TIKTOK_NO_ID_YET, "tiktok").empty


@pytest.mark.parametrize(
    ("body", "platform", "expected"),
    [
        (REAL_YOUTUBE_PRIVATE_POST, "youtube", ("kwtCUdnP_Zk", None)),  # a note is not a link
        (REAL_TIKTOK_NO_ID_YET, "tiktok", (None, None)),  # the publish id is not a post id
        (
            {
                "platforms": {
                    "tiktok": {"platform_post_id": 7300, "post_url": "https://www.tiktok.com/@x/video/7300"}
                }
            },
            "tiktok",
            ("7300", "https://www.tiktok.com/@x/video/7300"),
        ),
        (
            {
                "platforms": {
                    "tiktok": {"platform_post_id": "", "post_url": "Video sent to Inbox (No Public URL)"}
                }
            },
            "tiktok",
            (None, None),
        ),
        ({"platforms": {"tiktok": {"platform_post_id": "7300"}}}, "youtube", (None, None)),
        ({"platforms": {"tiktok": {"platform_post_id": True, "post_url": 5}}}, "tiktok", (None, None)),
        ({"platforms": "broken"}, "tiktok", (None, None)),
        (None, "tiktok", (None, None)),
    ],
)
def test_post_link_in_an_analytics_answer(
    body: Any, platform: str, expected: tuple[str | None, str | None]
) -> None:
    assert collector.parse_post_link(body, platform) == expected

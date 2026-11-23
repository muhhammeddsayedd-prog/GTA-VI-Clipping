"""The zero-views delivery guard: pure rules (docs/28 "Fix first", owner amendment 19 Nov 2026)."""

from datetime import UTC, datetime, timedelta

from sixstars.learning.delivery import GuardRules, PostView, counted_views, evaluate

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
RULES = GuardRules()


def post(
    n: int,
    *,
    age_h: float = 8,
    views: int | None = 0,
    read: bool = True,
    platform: str = "instagram",
    post_id: str | None = "pid",
) -> PostView:
    return PostView(f"p{n}", platform, NOW - timedelta(hours=age_h), views, read, post_id)


def test_five_read_posts_with_zero_views_trip_the_guard() -> None:
    verdict = evaluate("instagram_main", [post(i, age_h=7 + i) for i in range(5)], NOW, RULES)
    assert verdict.tripped
    assert verdict.median == 0
    assert verdict.counted == ("p0", "p1", "p2", "p3", "p4")


def test_a_median_above_zero_does_not_trip() -> None:
    views = [0, 0, 3, 5, 9]
    verdict = evaluate("x", [post(i, age_h=7 + i, views=v) for i, v in enumerate(views)], NOW, RULES)
    assert not verdict.tripped
    assert verdict.median == 3


def test_an_unread_post_is_unknown_not_zero() -> None:
    posts = [post(i, age_h=7 + i, read=i != 0, views=None) for i in range(5)]
    verdict = evaluate("x", posts, NOW, RULES)
    assert not verdict.tripped  # only 4 readable posts
    assert counted_views(posts[0], NOW, RULES) is None


def test_a_read_post_with_null_views_counts_as_zero() -> None:
    assert counted_views(post(1, views=None, read=True), NOW, RULES) == 0


def test_posts_younger_than_min_age_are_ignored() -> None:
    posts = [post(i, age_h=1 + i) for i in range(5)]  # ages 1..5 h
    verdict = evaluate("x", posts, NOW, RULES)
    assert not verdict.tripped
    assert verdict.median is None


def test_only_the_latest_five_old_enough_posts_count() -> None:
    old_good = [post(10 + i, age_h=30 + i, views=500) for i in range(5)]
    new_zero = [post(i, age_h=7 + i) for i in range(5)]
    verdict = evaluate("x", [*old_good, *new_zero], NOW, RULES)
    assert verdict.tripped
    assert set(verdict.counted) == {"p0", "p1", "p2", "p3", "p4"}


def test_a_tiktok_post_without_id_after_6h_counts_as_zero_even_unread() -> None:
    p = post(1, platform="tiktok", post_id=None, read=False, views=None, age_h=6.5)
    assert counted_views(p, NOW, RULES) == 0


def test_a_tiktok_post_without_id_before_6h_is_unknown() -> None:
    rules = GuardRules(min_age_h=0)
    p = post(1, platform="tiktok", post_id=None, read=False, views=None, age_h=2)
    assert counted_views(p, NOW, rules) is None


def test_rules_from_flag_ignore_bad_values() -> None:
    rules = GuardRules.from_flag(
        {"enabled": True, "last_posts": 0, "min_age_h": "x", "tiktok_missing_id_h": 12}
    )
    assert rules == GuardRules(last_posts=5, min_age_h=6.0, tiktok_missing_id_h=12.0)

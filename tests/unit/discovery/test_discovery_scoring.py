"""Trend score maths with fixed inputs (docs/06 section 3), the title blocklist, the language rules and
the chat-activity adjustments. No network, no database."""

import math

import pytest

from sixstars.discovery import scoring
from sixstars.discovery.config import ScoreConfig

CONFIG = ScoreConfig(
    weights_live={"viewers": 0.35, "growth": 0.25, "chat_rate": 0.2, "global_reach": 0.1, "history": 0.1},
    weights_vod={
        "views_per_hour": 0.45,
        "engagement": 0.15,
        "size_ratio": 0.15,
        "global_reach": 0.1,
        "history": 0.15,
    },
    non_english_penalty=0.85,
    preferred_bonus=15,
    chat_replay_bonus=5,
    chat_mpm_per_viewer=0.01,
    chat_msgs_per_view=0.02,
    chat_min_messages=1500,
    passive_views_engagement=0.005,
)


def live(**overrides: object) -> tuple[float, dict[str, object]]:
    arguments: dict[str, object] = {
        "viewers": 9999,
        "viewers_then": None,
        "measured_chat_mpm": None,
        "language": "en",
        "audience_spread": None,
        "history": 0.5,
        "preferred": False,
        "config": CONFIG,
    }
    arguments.update(overrides)
    return scoring.score_live(**arguments)  # type: ignore[arg-type]


def vod(**overrides: object) -> tuple[float, dict[str, object], float | None]:
    arguments: dict[str, object] = {
        "views": 99_999,
        "age_hours": 10.0,
        "likes": 4000,
        "comments": 0,
        "followers": 199_998,
        "duration_s": 7200,
        "has_chat": False,
        "peak_viewers": None,
        "measured_chat_mpm": None,
        "language": "en",
        "audience_spread": None,
        "history": 0.5,
        "preferred": False,
        "config": CONFIG,
    }
    arguments.update(overrides)
    return scoring.score_vod(**arguments)  # type: ignore[arg-type]


# ── live ─────────────────────────────────────────────────────


def test_live_score_with_fixed_inputs() -> None:
    # viewers 9,999 -> log10(10,000)/5 = 0.8; growth unknown 0.5; chat estimated 99.99 msgs/min ->
    # log10(100.99)/3; reach 1.0; history 0.5
    score, parts = live()
    chat = math.log10(99.99 + 1) / 3
    expected = 100 * (0.35 * 0.8 + 0.25 * 0.5 + 0.2 * chat + 0.1 * 1.0 + 0.1 * 0.5)
    assert score == pytest.approx(expected, abs=0.01)
    assert parts["viewers"] == pytest.approx(0.8)
    assert parts["growth"] == 0.5
    assert parts["chat_mpm"] == pytest.approx(99.99)
    assert parts["chat_measured"] is False


def test_live_viewers_part_is_capped_at_100k() -> None:
    assert live(viewers=5_000_000)[1]["viewers"] == 1.0


@pytest.mark.parametrize(
    ("now", "then", "expected"),
    [(2000, 1000, 1.0), (1500, 1000, 0.5), (1000, 1000, 0.0), (500, 1000, 0.0), (9000, 1000, 1.0)],
)
def test_growth_is_relative_change_clamped(now: int, then: int, expected: float) -> None:
    assert scoring.growth_part(now, then) == pytest.approx(expected)


def test_growth_unknown_is_half() -> None:
    assert scoring.growth_part(1000, None) == 0.5
    assert scoring.growth_part(1000, 0) == 0.5


def test_live_uses_measured_chat_rate_when_known() -> None:
    _, estimated = live(viewers=9999)
    score, measured = live(viewers=9999, measured_chat_mpm=999.0)
    assert measured["chat_rate"] == pytest.approx(1.0)
    assert measured["chat_measured"] is True
    assert measured["chat_rate"] > estimated["chat_rate"]  # type: ignore[operator]
    assert score > live(viewers=9999)[0]


def test_busy_stream_outranks_quiet_stream_on_chat() -> None:
    quiet = live(viewers=200)[1]["chat_rate"]
    busy = live(viewers=20_000)[1]["chat_rate"]
    assert quiet < 0.2 < busy  # type: ignore[operator]


def test_live_with_unknown_audience_scores_zero_on_viewers_and_chat() -> None:
    score, parts = live(viewers=None)
    assert parts["viewers"] == 0.0
    assert parts["chat_rate"] == 0.0
    assert score == pytest.approx(100 * (0.25 * 0.5 + 0.1 + 0.05))


def test_non_english_penalty_and_reach() -> None:
    english, _ = live()
    other, parts = live(language="pt")
    assert parts["global_reach"] == 0.6
    chat = math.log10(99.99 + 1) / 3
    raw = 100 * (0.35 * 0.8 + 0.25 * 0.5 + 0.2 * chat + 0.1 * 0.6 + 0.1 * 0.5)
    assert other == pytest.approx(raw * 0.85, abs=0.01)
    assert other < english


def test_international_audience_raises_reach() -> None:
    assert scoring.reach_part("pt", 0.7) == pytest.approx(0.8)
    assert scoring.reach_part("en", 0.7) == 1.0
    assert scoring.reach_part("pt", 0.5) == pytest.approx(0.6)


def test_preferred_bonus_is_added_and_score_is_capped() -> None:
    base, _ = live()
    assert live(preferred=True)[0] == pytest.approx(base + 15, abs=0.01)
    assert (
        live(viewers=10**7, viewers_then=1, measured_chat_mpm=5000.0, history=1.0, preferred=True)[0] == 100
    )


@pytest.mark.parametrize(
    ("value", "claims", "expected"),
    [(None, 0, 0.5), (None, 1, 0.0), (300.0, 0, 0.75), (300.0, 1, 0.25), (300.0, 2, 0.0)],
)
def test_history_part(value: float | None, claims: int, expected: float) -> None:
    assert scoring.history_part(value, [100.0, 200.0, 300.0, 400.0], claims) == pytest.approx(expected)


def test_percentile_of_empty_population_is_half() -> None:
    assert scoring.percentile(10.0, []) == 0.5


# ── videos ───────────────────────────────────────────────────


def test_vod_score_with_fixed_inputs() -> None:
    # 99,999 views in 10 h -> 9,999.9 per hour -> log10(10,000.9)/5; engagement 4000/99,999/0.08 = 0.5;
    # size ratio 99,999/199,998 = 0.5; reach 1; history 0.5
    score, parts, vph = vod()
    views_part = math.log10(9999.9 + 1) / 5
    expected = 100 * (0.45 * views_part + 0.15 * 0.5 + 0.15 * 0.5 + 0.1 + 0.15 * 0.5)
    assert vph == pytest.approx(9999.9)
    assert parts["engagement"] == pytest.approx(0.5, abs=1e-3)
    assert parts["size_ratio"] == pytest.approx(0.5, abs=1e-3)
    assert score == pytest.approx(expected, abs=0.01)


def test_vod_age_under_one_hour_counts_as_one_hour() -> None:
    assert vod(age_hours=0.1)[2] == pytest.approx(99_999)


def test_vod_small_channel_uses_follower_floor() -> None:
    assert vod(views=500, likes=10, followers=10)[1]["size_ratio"] == pytest.approx(0.5)


def test_vod_unknown_numbers_are_neutral() -> None:
    _, parts, vph = vod(views=None, likes=None, comments=None, followers=None, age_hours=None)
    assert vph is None
    assert (parts["views_per_hour"], parts["engagement"], parts["size_ratio"]) == (0.5, 0.5, 0.5)


def test_chat_replay_bonus_needs_an_active_chat() -> None:
    plain = vod()[0]
    # 99,999 views x 0.02 = 2,000 estimated messages >= 1,500: full bonus
    assert vod(has_chat=True)[0] == pytest.approx(plain + 5, abs=0.01)
    # a past stream we watched live with 20 viewers for 2 hours: 20 x 0.01 x 120 = 24 messages
    thin, parts, _ = vod(has_chat=True, peak_viewers=20)
    assert parts["chat_messages_est"] == 24
    assert thin == pytest.approx(plain + 5 * 24 / 1500, abs=0.01)
    # the creator's measured rate wins over both estimates: 20 msgs/min x 120 min = 2,400
    assert vod(has_chat=True, peak_viewers=20, measured_chat_mpm=20.0)[1]["chat_bonus"] == 5.0


def test_passive_views_are_scaled_down() -> None:
    # 7 million views with 8,000 likes and no comments: 0.11 % reacted, below the 0.5 % threshold
    passive, parts, _ = vod(views=7_000_000, likes=8000, comments=5, has_chat=True, followers=480_000)
    activity = (8000 + 10) / 7_000_000 / 0.005
    assert parts["activity"] == pytest.approx(activity, abs=1e-4)
    assert parts["views_per_hour"] == pytest.approx(1.0 * activity, abs=1e-3)
    engaged, engaged_parts, _ = vod(views=750_000, likes=15_000, comments=600, has_chat=True)
    assert engaged_parts["activity"] == 1.0
    assert engaged > passive + 15


def test_passive_rule_can_be_switched_off() -> None:
    from dataclasses import replace

    off = replace(CONFIG, passive_views_engagement=0.0)
    assert vod(views=7_000_000, likes=8000, config=off)[1]["activity"] == 1.0


def test_vod_non_english_penalty_applies_before_bonuses() -> None:
    english = vod(has_chat=True)[0]
    other = vod(has_chat=True, language="hi")[0]
    assert other < english * 0.9


# ── titles and languages ─────────────────────────────────────

BLOCKLIST = ["sponsored", "#ad", "music only", "lofi", "radio", "casino", "gambling", "slots"]


@pytest.mark.parametrize(
    "title",
    [
        "GTA 6 Casino heist all night",
        "SPONSORED stream | GTA RP",
        "gta 6 LEAK reaction",
        "GTA lofi radio 24/7",
    ],
)
def test_blocked_titles(title: str) -> None:
    assert scoring.title_blocked(title, BLOCKLIST)


def test_leak_is_blocked_even_with_an_empty_blocklist() -> None:
    assert scoring.title_blocked("New GTA 6 leaks!!", [])
    assert not scoring.title_blocked("Police chase in GTA RP", [])


def test_normal_title_passes() -> None:
    assert not scoring.title_blocked("The LAST Ride, Police Chases + MORE in GTA 5", BLOCKLIST)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Playing GTA6 early!", True),
        ("gta-6 first hour", True),
        ("GRAND THEFT AUTO VI | Day 1", True),
        ("Minecraft hardcore day 100", False),
        ("", False),
    ],
)
def test_matches_terms(title: str, expected: bool) -> None:
    assert scoring.matches_terms(title, ["GTA 6", "GTA VI", "Grand Theft Auto VI", "GTA6"]) is expected


def test_title_language_spots_non_latin_scripts() -> None:
    assert scoring.title_language("ГТА 5 прохождение без комментариев") == "und"
    assert scoring.title_language("GTA 5 लाइव स्ट्रीम आज रात") == "und"
    assert scoring.title_language("GTA 5 LIVE – DON’T BLINK! 🔴") is None
    assert scoring.title_language("🔴🔴 123") is None


@pytest.mark.parametrize(
    ("language", "expected"), [(None, True), ("en", True), ("en-GB", True), ("hi", False)]
)
def test_is_english(language: str | None, expected: bool) -> None:
    assert scoring.is_english(language) is expected

"""Learning which design style works on each account (development-plan/M18 Phase D): the post scores, the
style statistics, the director's bandit and a seven-day simulation. No network, no database."""

import math
import random

import pytest

from sixstars.factory.director import (
    DEFAULT_DIRECTOR,
    AccountSlot,
    DirectorRules,
    bandit_pick,
    plan_styles,
)
from sixstars.factory.performance import StyleStats, pooled_sigma, post_scores, summarise
from sixstars.factory.styles import StyleCatalog

THREE = StyleCatalog.from_setting({"good": {}, "plain": {}, "weak": {}})
BANDIT = DirectorRules.from_setting(
    {
        "mode": "bandit",
        "explore_floor": 0.1,
        "min_samples": 10,
        "no_repeat_n": 1,
        "diverse_across_accounts": True,
    }
)


def test_a_score_is_the_log_views_against_the_accounts_median() -> None:
    rows = [("tt", "good", 2000), ("tt", "plain", 1000), ("tt", None, 1000), ("ig", "plain", 10)]
    scores = post_scores(rows)
    assert scores["tt"]["plain"] == [0.0]
    assert scores["tt"]["good"][0] == pytest.approx(math.log1p(2000) - math.log1p(1000))
    assert scores["ig"]["plain"] == [0.0], "every account is its own baseline"
    assert "None" not in scores["tt"] and None not in scores["tt"], "posts without a style get no score"


def test_style_stats_shrink_towards_an_ordinary_post() -> None:
    stats = summarise({"tt": {"good": [1.0, 1.0, 1.0], "weak": [-1.0]}})["tt"]
    assert stats["good"] == StyleStats(3, 1.0, 0.0) and stats["weak"].var == 0.0
    mean, sd = stats["good"].posterior(prior_n=2.0, sigma=1.0)
    assert mean == pytest.approx(0.6) and sd == pytest.approx(1 / math.sqrt(5))
    assert pooled_sigma(stats) == 1.0, "too few posts: one unit of log views"
    spread = {"a": StyleStats(10, 0.0, 0.25), "b": StyleStats(10, 0.0, 0.25)}
    assert pooled_sigma(spread) == pytest.approx(0.5)


def test_the_bandit_settings_are_read_and_bad_values_fall_back() -> None:
    rules = DirectorRules.from_setting(DEFAULT_DIRECTOR)
    assert rules.mode == "bandit" and rules.explore_floor == 0.1 and rules.min_samples == 20
    assert (rules.min_age_h, rules.window_d, rules.prior_n) == (48.0, 14.0, 2.0)
    bad = DirectorRules.from_setting({"mode": "chaos", "explore_floor": 7, "min_samples": -3, "window_d": 0})
    assert (
        bad.mode == "weighted" and bad.explore_floor == 0.1 and bad.min_samples == 20 and bad.window_d == 14.0
    )


def test_the_bandit_learns_explores_and_then_exploits() -> None:
    entries = THREE.entries
    young = {"good": StyleStats(3, 1.0, 0.1)}
    pick, branch = bandit_pick(
        entries, entries, "s", DirectorRules(mode="bandit", explore_floor=0.0), young, None
    )
    assert branch == "learn"
    always = DirectorRules(mode="bandit", explore_floor=1.0)
    assert bandit_pick(entries, entries, "s", always, {}, None)[1] == "explore"
    known = {
        "good": StyleStats(50, 0.8, 1.0),
        "plain": StyleStats(50, 0.0, 1.0),
        "weak": StyleStats(50, -0.8, 1.0),
    }
    exploit = DirectorRules(mode="bandit", explore_floor=0.0, min_samples=20)
    picks = [bandit_pick(entries, entries, f"s{n}", exploit, known, None) for n in range(100)]
    assert all(branch == "exploit" for _, branch in picks)
    assert sum(entry.id == "good" for entry, _ in picks) >= 90


def test_the_affinity_still_nudges_an_exploiting_bandit() -> None:
    entries = THREE.entries
    tie = {name: StyleStats(200, 0.0, 1.0) for name in ("good", "plain", "weak")}
    rules = DirectorRules(
        mode="bandit", explore_floor=0.0, min_samples=20, affinity={"story": {"weak": 20.0}}
    )
    picks = [bandit_pick(entries, entries, f"s{n}", rules, tie, "story")[0].id for n in range(100)]
    assert picks.count("weak") >= 80


def test_the_weighted_mode_ignores_the_stats() -> None:
    accounts = [AccountSlot("tt", "tiktok", ("hourly",))]
    stats = {"tt": {"good": StyleStats(500, 5.0, 0.1)}}
    plan = plan_styles("c", "hourly", accounts, THREE, DirectorRules(), stats=stats)
    assert plan.choices["tt"].branch == "weighted"


def test_seven_days_of_posts_find_the_best_style_and_keep_exploring() -> None:
    """Three accounts post a clip an hour for a week; the true effect of the styles is +0.7, 0 and -0.5
    log views with spread 1. The bandit must end up mostly on the best style while every style keeps
    a share of the picks."""
    truth = {"good": 0.7, "plain": 0.0, "weak": -0.5}
    accounts = [AccountSlot(name, "tiktok", ("hourly",)) for name in ("a1", "a2", "a3")]
    rng = random.Random(7)
    history: dict[str, list[tuple[str, float]]] = {account.id: [] for account in accounts}
    last_day: dict[str, int] = {name: 0 for name in truth}
    totals: dict[str, int] = {name: 0 for name in truth}
    for hour in range(24 * 7):
        stats = {}
        for account, posts in history.items():
            by_style: dict[str, list[float]] = {}
            for style, value in posts:
                by_style.setdefault(style, []).append(value)
            stats[account] = summarise({account: by_style}).get(account, {})
        plan = plan_styles(f"clip-{hour}", "hourly", accounts, THREE, BANDIT, stats=stats)
        for account, style in plan.by_account.items():
            history[account].append((style, truth[style] + rng.gauss(0.0, 1.0)))
            totals[style] += 1
            if hour >= 24 * 6:
                last_day[style] += 1
    picks = sum(totals.values())
    assert last_day["good"] / sum(last_day.values()) > 0.5, last_day
    assert all(count / picks >= 0.1 / 3 * 0.5 for count in totals.values()), totals

"""How each design style performs on each account (development-plan/M18 Phase D). Pure.

The stats collector refreshes `posts.views` every two hours for posts up to `stats.post_lookback_d` days old;
there is no snapshot at a fixed age. So a post is scored once it is at least `min_age_h` old (most of a
short's views come in its first two days) and at most `window_d` days old:

    score = log(1 + views) - median over the account's scored posts of log(1 + views)

A score of 0 is an ordinary post for that account, +0.7 about twice its usual views, -0.7 about half.
Comparing within one account removes its size and its audience; comparing styles of one account over the
same weeks removes most of the rest (the style director picks at random among styles while it learns).

`StyleStats` summarises the scores of one (account, style). The director's bandit (`director.bandit_pick`)
draws from the posterior of each style's mean score (Thompson sampling with a normal approximation).
"""

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# (account id, style id or None, views)
PostViews = tuple[str, str | None, int]


@dataclass(frozen=True, slots=True)
class StyleStats:
    """The scores of one style on one account: how many, their mean and their sample variance."""

    n: int = 0
    mean: float = 0.0
    var: float = 0.0

    def posterior(self, prior_n: float, sigma: float) -> tuple[float, float]:
        """Mean and standard deviation of the style's true mean score: the sample mean shrunk towards 0 (an
        ordinary post) by `prior_n` pseudo-posts, with spread `sigma` per post."""
        weight = self.n + prior_n
        return self.n * self.mean / weight, sigma / math.sqrt(weight)


def post_scores(rows: Iterable[PostViews]) -> dict[str, dict[str, list[float]]]:
    """account -> style -> scores. Posts without a style (rendered before design styles) count in the
    account's median but get no score of their own."""
    by_account: dict[str, list[tuple[str | None, float]]] = {}
    for account, style, views in rows:
        by_account.setdefault(account, []).append((style, math.log1p(max(0, views))))
    scores: dict[str, dict[str, list[float]]] = {}
    for account, posts in by_account.items():
        median = statistics.median(value for _, value in posts)
        for style, value in posts:
            if style:
                scores.setdefault(account, {}).setdefault(style, []).append(value - median)
    return scores


def summarise(scores: Mapping[str, Mapping[str, list[float]]]) -> dict[str, dict[str, StyleStats]]:
    """account -> style -> `StyleStats`."""
    return {
        account: {
            style: StyleStats(
                n=len(values),
                mean=statistics.fmean(values),
                var=statistics.variance(values) if len(values) > 1 else 0.0,
            )
            for style, values in styles.items()
            if values
        }
        for account, styles in scores.items()
    }


def pooled_sigma(stats: Mapping[str, StyleStats], fallback: float = 1.0) -> float:
    """The spread of one post's score on an account: the pooled standard deviation of its styles, or
    `fallback` (one unit of log-views) while there are too few posts to tell."""
    dof = sum(item.n - 1 for item in stats.values() if item.n > 1)
    if dof < 2:
        return fallback
    pooled = sum(item.var * (item.n - 1) for item in stats.values() if item.n > 1) / dof
    return math.sqrt(pooled) if pooled > 0 else fallback

"""Stage 7 of the cover engine (docs/29a section 7): which arm (layout x line style) a cover uses.

The bandit may only choose among the layouts the scorer allows for the clip: the proposed layout and the
split (always possible), plus the TikTok-native box on TikTok. `choose` explores with probability
`cover.exploration` (never under 0.2) and otherwise plays the best arm by Thompson sampling over the
per-platform rewards in `stats` (C8); without stats it plays the scorer's proposal.
"""

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sixstars.covers import hookline, store
from sixstars.covers.compose import CoverLayout

NO_LINE_SHARE = 0.10  # the control arm (a cover without a line)


def arm_name(layout: str, style: str) -> str:
    """`split|quote`, `face_led|label`, `plain|none`... Pure."""
    return f"{layout}|{style}"


def allowed_layouts(platform: str, proposed: CoverLayout) -> tuple[CoverLayout, ...]:
    """The layouts the bandit may pick for this clip on this platform. Pure."""
    layouts: list[CoverLayout] = [proposed]
    if "split" not in layouts:
        layouts.append("split")
    if platform == "tiktok":
        layouts.append("tiktok_native")
    return tuple(layouts)


@dataclass(frozen=True, slots=True)
class ArmStats:
    """Wins and plays of one arm on one platform (wins: views above the account median at 48 h)."""

    wins: int = 0
    plays: int = 0


def thompson(stats: Mapping[str, ArmStats], arms: Sequence[str], rng: random.Random) -> str:
    """The arm with the highest Beta(1 + wins, 1 + losses) draw. Pure given the rng."""
    best, best_draw = arms[0], -1.0
    for arm in arms:
        s = stats.get(arm, ArmStats())
        draw = rng.betavariate(1 + s.wins, 1 + max(0, s.plays - s.wins))
        if draw > best_draw:
            best, best_draw = arm, draw
    return best


def choose(
    platform: str,
    proposed: CoverLayout,
    candidates: Sequence[hookline.Candidate],
    recent: Sequence[store.RecentCover],
    *,
    exploration: float,
    rng: random.Random | None = None,
    stats: Mapping[str, ArmStats] | None = None,
) -> tuple[CoverLayout, hookline.Candidate | None]:
    """The layout and the line for one cover. `candidates` are the valid lines (non-empty); None as the
    line is the no-line control arm."""
    rng = rng or random.Random()
    styles = list(dict.fromkeys(c.style for c in candidates))  # in the model's order
    arms = [arm_name(layout, style) for layout in allowed_layouts(platform, proposed) for style in styles]
    if rng.random() < NO_LINE_SHARE:
        return proposed, None
    if rng.random() < max(exploration, 0.2):
        chosen = rng.choice(arms)
    elif stats:
        chosen = thompson(stats, arms, rng)
    else:
        chosen = arm_name(proposed, candidates[0].style)
    layout_name, style = chosen.split("|")
    layout = next(item for item in allowed_layouts(platform, proposed) if item == layout_name)
    line = next(c for c in candidates if c.style == style)
    return layout, line


# ── Rewards (C8) ──────────────────────────────────────────────

# The learning loop snapshots posts at fixed ages (1/3/6/24/72/168 h, migration 0033); 72 h is the first
# at or after the prompt's 48 h. Only covers made before the current week count: the bandit updates weekly.
REWARD_AGE_H = 72
MEDIAN_WINDOW_D = 60

_STATS_SQL = """
with snap as (
  select p.account_id, s.post_id, s.views
  from post_metrics_snapshots s join posts p on p.id = s.post_id
  where s.target_age_h = %(age)s and s.views is not null
    and s.captured_at >= now() - make_interval(days => %(window)s)
), med as (
  select account_id, percentile_cont(0.5) within group (order by views) as median
  from snap group by account_id
)
select c.arm, count(*) as plays, count(*) filter (where snap.views > med.median) as wins
from covers c
join snap on snap.post_id = c.post_id
join med on med.account_id = snap.account_id
where c.platform = %(platform)s and c.status = 'applied' and c.created_at < date_trunc('week', now())
group by c.arm
"""


def load_stats(platform: str) -> dict[str, ArmStats]:
    """Wins and plays per arm on one platform (views above the account's median at 72 h)."""
    from sixstars import db

    rows = db.fetch_all(_STATS_SQL, {"age": REWARD_AGE_H, "window": MEDIAN_WINDOW_D, "platform": platform})
    return {str(r["arm"]): ArmStats(int(r["wins"]), int(r["plays"])) for r in rows}

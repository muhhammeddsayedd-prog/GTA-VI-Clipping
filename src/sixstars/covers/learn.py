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

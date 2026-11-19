"""The style director (development-plan/M18 Phase A): picks one design style per platform account for a
clip. Pure: no database, no network. `factory.variants` loads the inputs and stores the plan.

For each account, in a fixed order:

1. the styles the catalogue allows for the clip's lane and that account (`StyleEntry.allows`) whose
   needs the clip meets (`StyleEntry.fits`: a facecam, the clip writer's text the style shows);
2. without the styles the account used for its last `no_repeat_n` clips, unless that leaves none;
3. with `diverse_across_accounts`, without the styles already given to another account of this clip,
   unless that leaves none;
4. a weighted pick, seeded with the clip id and the account id, so the same clip always gets the same
   plan. Each weight is multiplied by the `affinity` of the clip writer's `moment_type` for the style
   (development-plan/M18 Phase B: a discovery suits the news card, a reaction the viral edit).

With `mode` "bandit" (development-plan/M18 Phase D) step 4 learns from the posts' views per account
(`performance`, `bandit_pick`): with probability `explore_floor` the pick stays the weighted one above;
while one of the allowed styles has fewer than `min_samples` scored posts on the account, the pick is
weighted among those; otherwise each style's mean score is drawn from its posterior (Thompson sampling),
nudged by `affinity_weight` times the log of its affinity, and the highest wins, without the no-repeat
and diversity steps (it is the account's best style for this kind of moment).

The reference account is the one the first render is made for: the first account (by id) that posts the
clip's lane, else the first account. The other accounts get their own render only when their style
differs (`factory.variants`).
"""

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sixstars.factory.performance import StyleStats, pooled_sigma
from sixstars.factory.styles import StyleCatalog, StyleEntry

Mode = Literal["weighted", "bandit"]
Branch = Literal["weighted", "explore", "learn", "exploit"]

# The setting of migrations 0027 and 0028; `config.OFFLINE_DEFAULTS` and the tests use it.
DEFAULT_DIRECTOR: dict[str, object] = {
    "no_repeat_n": 1,
    "diverse_across_accounts": True,
    "affinity": {
        "reaction": {"cam_top_viral": 3},
        "discovery": {"headline_card": 3, "cam_top_question": 3},
        "action": {"pov_fullframe": 3, "cam_top_viral": 2},
        "story": {"cam_top_title": 3, "cam_top_wordcap": 3},
        "npc_funny": {"cam_top_title": 2, "pov_fullframe": 2},
        "fail": {"cam_top_viral": 2, "pov_fullframe": 2},
    },
    # Phase D (migration 0030).
    "mode": "bandit",
    "explore_floor": 0.1,
    "min_samples": 20,
    "affinity_weight": 0.3,
    "prior_n": 2,
    "min_age_h": 48,
    "window_d": 14,
}


@dataclass(frozen=True, slots=True)
class DirectorRules:
    """The setting `factory.style_director`."""

    no_repeat_n: int = 1
    diverse_across_accounts: bool = True
    # moment_type -> style id -> weight multiplier (a missing pair is 1).
    affinity: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    # Phase D: learn from the posts' views (`bandit`) or keep the weighted pick (`weighted`).
    mode: Mode = "weighted"
    explore_floor: float = 0.1
    min_samples: int = 20
    affinity_weight: float = 0.3
    prior_n: float = 2.0
    min_age_h: float = 48.0
    window_d: float = 14.0

    def multiplier(self, moment_type: str | None, style_id: str) -> float:
        return float(self.affinity.get(moment_type or "", {}).get(style_id, 1.0))

    @classmethod
    def from_setting(cls, raw: object) -> "DirectorRules":
        if not isinstance(raw, Mapping):
            return cls()
        n = raw.get("no_repeat_n", 1)
        diverse = raw.get("diverse_across_accounts", True)
        raw_affinity = raw.get("affinity", {})
        affinity: dict[str, dict[str, float]] = {}
        if isinstance(raw_affinity, Mapping):
            for kind, styles in raw_affinity.items():
                if not isinstance(styles, Mapping):
                    continue
                affinity[str(kind)] = {
                    str(style): float(value)
                    for style, value in styles.items()
                    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
                }
        base = cls()

        def number(key: str, fallback: float, low: float, high: float) -> float:
            value = raw.get(key)
            ok = isinstance(value, int | float) and not isinstance(value, bool) and low <= value <= high
            return float(value) if ok else fallback  # type: ignore[arg-type]

        mode = raw.get("mode")
        return cls(
            no_repeat_n=n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 1,
            diverse_across_accounts=diverse if isinstance(diverse, bool) else True,
            affinity=affinity,
            mode="bandit" if mode == "bandit" else "weighted",
            explore_floor=number("explore_floor", base.explore_floor, 0.0, 1.0),
            min_samples=int(number("min_samples", base.min_samples, 0, 10_000)),
            affinity_weight=number("affinity_weight", base.affinity_weight, 0.0, 10.0),
            prior_n=number("prior_n", base.prior_n, 0.0, 1_000.0),
            min_age_h=number("min_age_h", base.min_age_h, 0.0, 24 * 30),
            window_d=number("window_d", base.window_d, 1.0, 365.0),
        )


@dataclass(frozen=True, slots=True)
class AccountSlot:
    """A platform account a clip may be posted on."""

    id: str
    platform: str
    lanes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Choice:
    style_id: str
    candidates: tuple[str, ...]
    dropped_repeat: tuple[str, ...] = ()
    dropped_taken: tuple[str, ...] = ()
    unmet: tuple[str, ...] = ()
    branch: Branch = "weighted"


@dataclass(frozen=True, slots=True)
class StylePlan:
    """The style of every account and the account of the first render."""

    reference_account: str | None
    reference_style: str
    by_account: dict[str, str] = field(default_factory=dict)
    choices: dict[str, Choice] = field(default_factory=dict)
    facts: tuple[str, ...] = ()
    moment_type: str | None = None

    @property
    def styles(self) -> tuple[str, ...]:
        """Every style the plan needs a render of, the reference style first."""
        rest = sorted({style for style in self.by_account.values() if style != self.reference_style})
        return (self.reference_style, *rest)

    def as_json(self) -> dict[str, object]:
        return {
            "reference_account": self.reference_account,
            "reference_style": self.reference_style,
            "by_account": dict(self.by_account),
            "facts": list(self.facts),
            "moment_type": self.moment_type,
            "choices": {
                account: {
                    "style_id": choice.style_id,
                    "candidates": list(choice.candidates),
                    "dropped_repeat": list(choice.dropped_repeat),
                    "dropped_taken": list(choice.dropped_taken),
                    "unmet": list(choice.unmet),
                    "branch": choice.branch,
                }
                for account, choice in self.choices.items()
            },
        }


def _weighted(entries: Sequence[StyleEntry], seed: str, weights: Sequence[float] | None = None) -> StyleEntry:
    rng = random.Random(seed)  # noqa: S311 - a reproducible pick, not security
    shares = list(weights) if weights is not None else [entry.weight for entry in entries]
    total = sum(shares)
    if total <= 0:
        return entries[0]
    point = rng.uniform(0, total)
    running = 0.0
    for entry, share in zip(entries, shares, strict=True):
        running += share
        if point <= running:
            return entry
    return entries[-1]


def _without(entries: Sequence[StyleEntry], drop: set[str]) -> tuple[tuple[StyleEntry, ...], tuple[str, ...]]:
    """The entries not in `drop` and the ids actually dropped; all of them when nothing would be left."""
    kept = tuple(entry for entry in entries if entry.id not in drop)
    if not kept:
        return tuple(entries), ()
    return kept, tuple(entry.id for entry in entries if entry.id in drop)


def bandit_pick(
    usable: Sequence[StyleEntry],
    kept: Sequence[StyleEntry],
    seed: str,
    rules: DirectorRules,
    stats: Mapping[str, StyleStats],
    moment_type: str | None,
) -> tuple[StyleEntry, Branch]:
    """Phase D: the style for one account. `usable` is every style the clip and the account allow, `kept`
    the same after the no-repeat and diversity steps; `stats` the account's scores per style. Pure and
    reproducible for one `seed`."""
    rng = random.Random(f"{seed}:bandit")  # noqa: S311 - a reproducible pick, not security
    weights = [entry.weight * rules.multiplier(moment_type, entry.id) for entry in kept]
    if rng.random() < rules.explore_floor:
        return _weighted(kept, seed, weights), "explore"
    young = [entry for entry in kept if stats.get(entry.id, StyleStats()).n < rules.min_samples]
    if not young:
        young = [entry for entry in usable if stats.get(entry.id, StyleStats()).n < rules.min_samples]
    if young:
        young_weights = [entry.weight * rules.multiplier(moment_type, entry.id) for entry in young]
        return _weighted(young, seed, young_weights), "learn"
    sigma = pooled_sigma({entry.id: stats[entry.id] for entry in usable if entry.id in stats})

    def draw(entry: StyleEntry) -> float:
        mean, sd = stats.get(entry.id, StyleStats()).posterior(rules.prior_n, sigma)
        nudge = rules.affinity_weight * math.log(max(rules.multiplier(moment_type, entry.id), 1e-6))
        return mean + sd * rng.gauss(0.0, 1.0) + nudge

    draws = {entry.id: draw(entry) for entry in usable}
    return max(usable, key=lambda entry: draws[entry.id]), "exploit"


def clip_facts(texts: Mapping[str, str], *, has_facecam: bool) -> frozenset[str]:
    """What a clip offers the styles (`StyleEntry.requires`): `facecam` and the names of the writer's
    texts it has. Pure."""
    found = {name for name, text in texts.items() if text}
    return frozenset(found | ({"facecam"} if has_facecam else set()))


def reference_account(accounts: Sequence[AccountSlot], lane: str) -> str | None:
    ordered = sorted(accounts, key=lambda account: account.id)
    for account in ordered:
        if lane in account.lanes:
            return account.id
    return ordered[0].id if ordered else None


def plan_styles(
    clip_id: str,
    lane: str,
    accounts: Sequence[AccountSlot],
    catalog: StyleCatalog,
    rules: DirectorRules,
    recent: Mapping[str, Sequence[str]] | None = None,
    facts: frozenset[str] = frozenset(),
    moment_type: str | None = None,
    stats: Mapping[str, Mapping[str, StyleStats]] | None = None,
) -> StylePlan:
    """The style of every account for one clip. `recent` maps an account id to the styles of its latest
        clips, newest first; `facts` is what the clip has (`facecam` and the names of the writer's texts) and
        `moment_type` the writer's kind of moment; `stats` (bandit mode) maps an account to its scores per
    style. Without accounts (or without a usable style for one) the
        first usable style of the catalogue that fits the clip is used, so a clip always gets a style."""
    recent = recent or {}
    lane_styles = catalog.usable(lane, None)
    fallback = [entry for entry in lane_styles if entry.fits(facts)] or lane_styles or catalog.entries
    default_style = fallback[0].id if fallback else ""
    reference = reference_account(accounts, lane)
    ordered = sorted(accounts, key=lambda account: (account.id != reference, account.id))
    by_account: dict[str, str] = {}
    choices: dict[str, Choice] = {}
    for account in ordered:
        allowed = catalog.usable(lane, account.id)
        usable = tuple(entry for entry in allowed if entry.fits(facts))
        unmet = tuple(entry.id for entry in allowed if not entry.fits(facts))
        if not usable:
            by_account[account.id] = default_style
            choices[account.id] = Choice(default_style, (), unmet=unmet)
            continue
        last = set(list(recent.get(account.id, ()))[: rules.no_repeat_n])
        kept, dropped_repeat = _without(usable, last)
        dropped_taken: tuple[str, ...] = ()
        if rules.diverse_across_accounts:
            kept, dropped_taken = _without(kept, set(by_account.values()))
        seed = f"{clip_id}:{account.id}"
        branch: Branch = "weighted"
        if rules.mode == "bandit":
            picked, branch = bandit_pick(
                usable, kept, seed, rules, (stats or {}).get(account.id, {}), moment_type
            )
        else:
            weights = [entry.weight * rules.multiplier(moment_type, entry.id) for entry in kept]
            picked = _weighted(kept, seed, weights)
        by_account[account.id] = picked.id
        choices[account.id] = Choice(
            picked.id, tuple(entry.id for entry in usable), dropped_repeat, dropped_taken, unmet, branch
        )
    reference_style = by_account.get(reference, default_style) if reference is not None else default_style
    return StylePlan(reference, reference_style, by_account, choices, tuple(sorted(facts)), moment_type)

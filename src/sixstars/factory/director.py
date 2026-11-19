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

The reference account is the one the first render is made for: the first account (by id) that posts the
clip's lane, else the first account. The other accounts get their own render only when their style
differs (`factory.variants`).
"""

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sixstars.factory.styles import StyleCatalog, StyleEntry

# The setting of migrations 0027 and 0028; `config.OFFLINE_DEFAULTS` and the tests use it.
DEFAULT_DIRECTOR: dict[str, object] = {
    "no_repeat_n": 1,
    "diverse_across_accounts": True,
    "affinity": {
        "reaction": {"cam_top_viral": 3},
        "discovery": {"headline_card": 3, "cam_top_question": 3},
        "action": {"pov_fullframe": 3, "cam_top_viral": 2},
        "story": {"cam_top_title": 3},
        "npc_funny": {"cam_top_title": 2, "pov_fullframe": 2},
        "fail": {"cam_top_viral": 2, "pov_fullframe": 2},
    },
}


@dataclass(frozen=True, slots=True)
class DirectorRules:
    """The setting `factory.style_director`."""

    no_repeat_n: int = 1
    diverse_across_accounts: bool = True
    # moment_type -> style id -> weight multiplier (a missing pair is 1).
    affinity: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

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
        return cls(
            no_repeat_n=n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 1,
            diverse_across_accounts=diverse if isinstance(diverse, bool) else True,
            affinity=affinity,
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
) -> StylePlan:
    """The style of every account for one clip. `recent` maps an account id to the styles of its latest
    clips, newest first; `facts` is what the clip has (`facecam` and the names of the writer's texts) and
    `moment_type` the writer's kind of moment. Without accounts (or without a usable style for one) the
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
        weights = [entry.weight * rules.multiplier(moment_type, entry.id) for entry in kept]
        picked = _weighted(kept, f"{clip_id}:{account.id}", weights)
        by_account[account.id] = picked.id
        choices[account.id] = Choice(
            picked.id, tuple(entry.id for entry in usable), dropped_repeat, dropped_taken, unmet
        )
    reference_style = by_account.get(reference, default_style) if reference is not None else default_style
    return StylePlan(reference, reference_style, by_account, choices, tuple(sorted(facts)), moment_type)

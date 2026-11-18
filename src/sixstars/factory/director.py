"""The style director (development-plan/M18 Phase A): picks one design style per platform account for a
clip. Pure: no database, no network. `factory.variants` loads the inputs and stores the plan.

For each account, in a fixed order:

1. the styles the catalogue allows for the clip's lane and that account (`StyleEntry.allows`);
2. without the styles the account used for its last `no_repeat_n` clips, unless that leaves none;
3. with `diverse_across_accounts`, without the styles already given to another account of this clip,
   unless that leaves none;
4. a weighted pick, seeded with the clip id and the account id, so the same clip always gets the same
   plan.

The reference account is the one the first render is made for: the first account (by id) that posts the
clip's lane, else the first account. The other accounts get their own render only when their style
differs (`factory.variants`).
"""

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sixstars.factory.styles import StyleCatalog, StyleEntry


@dataclass(frozen=True, slots=True)
class DirectorRules:
    """The setting `factory.style_director`."""

    no_repeat_n: int = 1
    diverse_across_accounts: bool = True

    @classmethod
    def from_setting(cls, raw: object) -> "DirectorRules":
        if not isinstance(raw, Mapping):
            return cls()
        n = raw.get("no_repeat_n", 1)
        diverse = raw.get("diverse_across_accounts", True)
        return cls(
            no_repeat_n=n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 1,
            diverse_across_accounts=diverse if isinstance(diverse, bool) else True,
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


@dataclass(frozen=True, slots=True)
class StylePlan:
    """The style of every account and the account of the first render."""

    reference_account: str | None
    reference_style: str
    by_account: dict[str, str] = field(default_factory=dict)
    choices: dict[str, Choice] = field(default_factory=dict)

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
            "choices": {
                account: {
                    "style_id": choice.style_id,
                    "candidates": list(choice.candidates),
                    "dropped_repeat": list(choice.dropped_repeat),
                    "dropped_taken": list(choice.dropped_taken),
                }
                for account, choice in self.choices.items()
            },
        }


def _weighted(entries: Sequence[StyleEntry], seed: str) -> StyleEntry:
    rng = random.Random(seed)  # noqa: S311 - a reproducible pick, not security
    total = sum(entry.weight for entry in entries)
    point = rng.uniform(0, total)
    running = 0.0
    for entry in entries:
        running += entry.weight
        if point <= running:
            return entry
    return entries[-1]


def _without(entries: Sequence[StyleEntry], drop: set[str]) -> tuple[tuple[StyleEntry, ...], tuple[str, ...]]:
    """The entries not in `drop` and the ids actually dropped; all of them when nothing would be left."""
    kept = tuple(entry for entry in entries if entry.id not in drop)
    if not kept:
        return tuple(entries), ()
    return kept, tuple(entry.id for entry in entries if entry.id in drop)


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
) -> StylePlan:
    """The style of every account for one clip. `recent` maps an account id to the styles of its latest
    clips, newest first. Without accounts (or without a usable style for one) the first usable style of the
    catalogue is used, so a clip always gets a style."""
    recent = recent or {}
    fallback = catalog.usable(lane, None) or catalog.entries
    default_style = fallback[0].id if fallback else ""
    reference = reference_account(accounts, lane)
    ordered = sorted(accounts, key=lambda account: (account.id != reference, account.id))
    by_account: dict[str, str] = {}
    choices: dict[str, Choice] = {}
    for account in ordered:
        usable = catalog.usable(lane, account.id)
        if not usable:
            by_account[account.id] = default_style
            choices[account.id] = Choice(default_style, ())
            continue
        last = set(list(recent.get(account.id, ()))[: rules.no_repeat_n])
        kept, dropped_repeat = _without(usable, last)
        dropped_taken: tuple[str, ...] = ()
        if rules.diverse_across_accounts:
            kept, dropped_taken = _without(kept, set(by_account.values()))
        picked = _weighted(kept, f"{clip_id}:{account.id}")
        by_account[account.id] = picked.id
        choices[account.id] = Choice(
            picked.id, tuple(entry.id for entry in usable), dropped_repeat, dropped_taken
        )
    reference_style = by_account.get(reference, default_style) if reference is not None else default_style
    return StylePlan(reference, reference_style, by_account, choices)

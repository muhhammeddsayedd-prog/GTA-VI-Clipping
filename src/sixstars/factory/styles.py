"""The catalogue of design styles (development-plan/M18, docs/27 section 5): the setting `factory.styles`.

Each entry names a style and holds its `StyleRules` plus when it may be used:

    {"cam_top_viral": {"enabled": true, "weight": 1, "lanes": ["spike", "hourly"], "accounts": "*",
                       "rules": {...StyleRules keys...}}, ...}

`accounts` is `"*"` (every account) or a list of `platform_accounts.id`. `rules` keys missing from an entry
keep their classic value. An entry that is not an object, or whose rules fail validation, is left out.

Without `factory.styles` (a database before migration 0027) the catalogue holds one style, `legacy`, built
from `factory.style`: every clip then looks exactly as before.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sixstars.factory.style import (
    CAM_TOP_QUESTION,
    CAM_TOP_TITLE,
    CAM_TOP_VIRAL,
    CAM_TOP_WORDCAP,
    HEADLINE_CARD,
    POV_FULLFRAME,
    StyleRules,
)

LEGACY_STYLE_ID = "legacy"
# What a style may require of a clip (`StyleEntry.requires`): a facecam, or one of the writer's texts.
REQUIREMENTS = ("facecam", "title_line", "headline", "question", "pov_line")
ALL_ACCOUNTS = "*"
LANES = ("spike", "hourly")

# The catalogue of migration 0027; `config.OFFLINE_DEFAULTS` and the tests use it.
DEFAULT_STYLES: dict[str, object] = {
    "cam_top_viral": {
        "enabled": True,
        "weight": 1,
        "lanes": list(LANES),
        "accounts": ALL_ACCOUNTS,
        "rules": CAM_TOP_VIRAL,
    },
    "cam_top_title": {
        "enabled": True,
        "weight": 1,
        "lanes": list(LANES),
        "accounts": ALL_ACCOUNTS,
        "rules": CAM_TOP_TITLE,
    },
    # Phase B (migration 0028).
    "cam_top_question": {
        "enabled": True,
        "weight": 1,
        "lanes": list(LANES),
        "accounts": ALL_ACCOUNTS,
        "requires": ["question"],
        "rules": CAM_TOP_QUESTION,
    },
    "headline_card": {
        "enabled": True,
        "weight": 1,
        "lanes": list(LANES),
        "accounts": ALL_ACCOUNTS,
        "requires": ["headline"],
        "rules": HEADLINE_CARD,
    },
    "pov_fullframe": {
        "enabled": True,
        "weight": 1,
        "lanes": list(LANES),
        "accounts": ALL_ACCOUNTS,
        "requires": ["pov_line"],
        "rules": POV_FULLFRAME,
    },
    # Phase C (migration 0029).
    "cam_top_wordcap": {
        "enabled": True,
        "weight": 1,
        "lanes": list(LANES),
        "accounts": ALL_ACCOUNTS,
        "rules": CAM_TOP_WORDCAP,
    },
}


@dataclass(frozen=True, slots=True)
class StyleEntry:
    """One style of the catalogue. `accounts` None means every account."""

    id: str
    rules: StyleRules
    enabled: bool = True
    weight: float = 1.0
    lanes: tuple[str, ...] = LANES
    accounts: tuple[str, ...] | None = None
    requires: tuple[str, ...] = ()

    def fits(self, facts: frozenset[str]) -> bool:
        """Does the clip have everything this style needs (a facecam, the writer's text it shows)?"""
        return all(need in facts for need in self.requires)

    def allows(self, lane: str, account_id: str | None) -> bool:
        """May this style be used for a clip of `lane` posted on `account_id` (None: any account)?"""
        if not self.enabled or self.weight <= 0 or lane not in self.lanes:
            return False
        return self.accounts is None or account_id is None or account_id in self.accounts


def _entry(style_id: str, raw: object) -> StyleEntry | None:
    if not isinstance(raw, Mapping) or not style_id.strip():
        return None
    rules_raw = raw.get("rules", {})
    if not isinstance(rules_raw, Mapping):
        return None
    rules = StyleRules.from_setting(rules_raw)
    enabled = raw.get("enabled", True)
    weight = raw.get("weight", 1)
    lanes = raw.get("lanes", list(LANES))
    accounts = raw.get("accounts", ALL_ACCOUNTS)
    requires = raw.get("requires", [])
    if not isinstance(requires, list) or not all(need in REQUIREMENTS for need in requires):
        return None
    if not isinstance(enabled, bool) or isinstance(weight, bool) or not isinstance(weight, int | float):
        return None
    if not isinstance(lanes, list) or not all(lane in LANES for lane in lanes):
        return None
    if accounts == ALL_ACCOUNTS:
        allowed: tuple[str, ...] | None = None
    elif isinstance(accounts, list) and all(isinstance(item, str) for item in accounts):
        allowed = tuple(accounts)
    else:
        return None
    return StyleEntry(
        id=style_id.strip(),
        rules=rules,
        enabled=enabled,
        weight=float(weight),
        lanes=tuple(lanes),
        accounts=allowed,
        requires=tuple(requires),
    )


@dataclass(frozen=True, slots=True)
class StyleCatalog:
    """Every known style, sorted by id (a `jsonb` setting does not keep the order its keys were written
    in, so the order is made explicit: the first usable style is the fallback)."""

    entries: tuple[StyleEntry, ...]

    def get(self, style_id: str | None) -> StyleEntry | None:
        return next((entry for entry in self.entries if entry.id == style_id), None)

    def rules_for(self, style_id: str | None) -> StyleRules:
        """The rules of `style_id`; an unknown id (a style removed from the setting since) gives the first
        entry's rules, never an error."""
        entry = self.get(style_id) or (self.entries[0] if self.entries else None)
        return entry.rules if entry is not None else StyleRules.classic()

    def usable(self, lane: str, account_id: str | None) -> tuple[StyleEntry, ...]:
        return tuple(entry for entry in self.entries if entry.allows(lane, account_id))

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.entries)

    @classmethod
    def legacy(cls, style_setting: object) -> "StyleCatalog":
        """One style from the old single setting `factory.style` (the look before migration 0027)."""
        return cls((StyleEntry(LEGACY_STYLE_ID, StyleRules.from_setting(style_setting)),))

    @classmethod
    def from_setting(cls, raw: object, style_setting: object = None) -> "StyleCatalog":
        """Build the catalogue from `factory.styles`. A missing or unusable setting (or one without a
        single usable entry) gives the legacy catalogue from `factory.style`."""
        if not isinstance(raw, Mapping):
            return cls.legacy(style_setting)
        entries = [
            entry for key, value in sorted(raw.items()) if (entry := _entry(str(key), value)) is not None
        ]
        if not any(entry.enabled for entry in entries):
            return cls.legacy(style_setting)
        return cls(tuple(entries))


def style_ids(entries: Sequence[StyleEntry]) -> list[str]:
    return [entry.id for entry in entries]

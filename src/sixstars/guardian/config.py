"""Guardian thresholds, all read from the `settings` table (docs/16). Nothing here has a default in
code: a missing key raises, so a typo or a missing migration is found at once."""

from dataclasses import dataclass
from typing import Any

from sixstars import db, settings

TRIGGERS: tuple[str, ...] = ("discovery", "hourly", "guardian", "stats")
TRIGGER_PREFIX = "trigger:"


@dataclass(frozen=True, slots=True)
class GuardianConfig:
    check_lookback_h: float
    check_min_age_s: float
    check_max_posts: int
    check_budget_s: float
    check_private_posts: bool
    mass_missing_min: int
    mass_missing_ratio: float
    strike_limit: int
    strike_window_d: float
    takedown_body_max_chars: int
    takedown_max_per_hour: int
    handle_match_min_chars: int


def load() -> GuardianConfig:
    return GuardianConfig(
        check_lookback_h=settings.get_float("guardian.check_lookback_h"),
        check_min_age_s=settings.get_float("guardian.check_min_age_s"),
        check_max_posts=settings.get_int("guardian.check_max_posts_per_run"),
        check_budget_s=settings.get_float("guardian.check_budget_s"),
        check_private_posts=settings.get_bool("guardian.check_private_posts"),
        mass_missing_min=settings.get_int("guardian.mass_missing_min"),
        mass_missing_ratio=settings.get_float("guardian.mass_missing_ratio"),
        strike_limit=settings.get_int("guardian.strike_limit"),
        strike_window_d=settings.get_float("guardian.strike_window_d"),
        takedown_body_max_chars=settings.get_int("guardian.takedown_body_max_chars"),
        takedown_max_per_hour=settings.get_int("guardian.takedown_max_per_hour"),
        handle_match_min_chars=settings.get_int("guardian.handle_match_min_chars"),
    )


def fresh_setting(key: str) -> Any:
    """One setting read from the table now, not from the 30 s cache."""
    row = db.fetch_one("select value from settings where key = %s", (key,))
    return None if row is None else row["value"]


def seconds_map(key: str) -> dict[str, float]:
    """An object setting of seconds per trigger name, for example `guardian.failover_grace_s`."""
    out: dict[str, float] = {}
    for name, value in settings.get_dict(key).items():
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            out[str(name)] = float(value)
    return out

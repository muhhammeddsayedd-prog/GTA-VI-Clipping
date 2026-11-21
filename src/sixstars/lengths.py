"""Hourly clip length per platform (`gate.hourly_len_by_platform`, YouTube lane tuning C, migration 0036).

`gate.hourly` `min_len` / `max_len` stay the length for every platform; an entry here replaces them for one
platform, e.g. `{"youtube": {"min_len": 8, "max_len": 30}}`. Three places read it:

- the clip writer and the gate take the union (the shortest minimum, the longest maximum), so a clip that
  fits any platform can be cut and approved;
- the posting engine sends a short hourly clip only to the platforms whose own window it fits.

Spike clips (gate profile `express`) and story cuts are not affected. Pure functions, no database.
"""

from collections.abc import Mapping
from dataclasses import dataclass

SETTING = "gate.hourly_len_by_platform"


@dataclass(frozen=True, slots=True)
class LengthWindow:
    min_s: float
    max_s: float

    def fits(self, duration_s: float) -> bool:
        return self.min_s <= duration_s <= self.max_s


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def overrides(raw: object) -> dict[str, LengthWindow]:
    """The valid entries of the setting, by platform name. A malformed entry is left out (that platform then
    uses `gate.hourly`); a malformed setting gives no overrides at all."""
    if not isinstance(raw, Mapping):
        return {}
    windows: dict[str, LengthWindow] = {}
    for platform, entry in raw.items():
        if not isinstance(platform, str) or not isinstance(entry, Mapping):
            continue
        low, high = _number(entry.get("min_len")), _number(entry.get("max_len"))
        if low is None or high is None or high < low or high <= 0:
            continue
        windows[platform] = LengthWindow(low, high)
    return windows


def window_for(platform: str, default: LengthWindow, raw: object) -> LengthWindow:
    """The hourly length window of `platform`: its override, or `default` (`gate.hourly`)."""
    return overrides(raw).get(platform, default)


def union(default: LengthWindow, raw: object) -> LengthWindow:
    """The widest window: a clip inside it fits at least one platform's window."""
    windows = [default, *overrides(raw).values()]
    return LengthWindow(min(w.min_s for w in windows), max(w.max_s for w in windows))

"""Story cuts (development-plan/M18 Phase C, decision D113): the setting `factory.story`.

Besides the short clip, the clip writer may propose a longer cut of the same moment (60-150 s) when the
window holds a story with a setup and an outcome. That needs footage around the moment, so for the lanes
that may get a story the source window is widened by `pad_before_s` / `pad_after_s` (the VOD section and
the live cut). Express (spike-lane) renders never get one: every extra second costs latency.

A story is rendered right after its short clip in the same job, as a second `clips` row
(`version = 'story'`, `parent_clip_id` the short one), and goes through the gate and the hourly lane like
any other clip.
"""

from collections.abc import Mapping
from dataclasses import dataclass

SOURCES = ("vod", "live")


@dataclass(frozen=True, slots=True)
class StoryRules:
    """The setting `factory.story`. `enabled` False (or a missing setting) means no story cuts and no wider
    windows: everything as before migration 0029."""

    enabled: bool = False
    min_s: float = 60.0
    max_s: float = 150.0
    pad_before_s: float = 90.0
    pad_after_s: float = 30.0
    sources: tuple[str, ...] = SOURCES

    def applies(self, *, lane: str, mode: str, source_type: str) -> bool:
        """Does a render of this kind get a story cut (and a wider window)? Hourly, normal-mode renders of
        an allowed source only. Pure."""
        return self.enabled and lane == "hourly" and mode == "normal" and source_type in self.sources

    def widen(self, pad_start: float, pad_end: float) -> tuple[float, float]:
        """The padded range with room for a story: `pad_before_s` before and `pad_after_s` after (never
        narrower than given; the caller clamps to its source). Pure."""
        if not self.enabled:
            return pad_start, pad_end
        return pad_start - self.pad_before_s, pad_end + self.pad_after_s

    @classmethod
    def from_setting(cls, raw: object) -> "StoryRules":
        """Build the rules from the JSON object of the setting; anything malformed turns stories off."""
        if not isinstance(raw, Mapping):
            return cls()

        def number(key: str, fallback: float) -> float:
            value = raw.get(key)
            return (
                float(value) if isinstance(value, int | float) and not isinstance(value, bool) else fallback
            )

        base = cls()
        enabled = raw.get("enabled")
        sources = raw.get("sources", list(SOURCES))
        rules = cls(
            enabled=enabled if isinstance(enabled, bool) else False,
            min_s=number("min_s", base.min_s),
            max_s=number("max_s", base.max_s),
            pad_before_s=max(0.0, number("pad_before_s", base.pad_before_s)),
            pad_after_s=max(0.0, number("pad_after_s", base.pad_after_s)),
            sources=tuple(s for s in sources if s in SOURCES) if isinstance(sources, list) else SOURCES,
        )
        if not 0 < rules.min_s <= rules.max_s:
            return cls()
        return rules


# The setting of migration 0029; `config.OFFLINE_DEFAULTS` and the tests use it.
DEFAULT_STORY: dict[str, object] = {
    "enabled": True,
    "min_s": 60,
    "max_s": 150,
    "pad_before_s": 90,
    "pad_after_s": 30,
    "sources": ["vod", "live"],
}

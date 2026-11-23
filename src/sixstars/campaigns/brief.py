"""The `brief_match` check of a campaign clip (D118, PR 3). Run by the factory once the writer's window
and the facecam are known, before the clip is drawn: a clip that does not fit its campaign's brief is not
dropped but made as a Normal clip in the `brand` look (no re-render needed). Pure.

Brief rules (`clip_campaigns.brief_rules`, all optional):
- `streamer_on_screen` (default true): the streamer's face cam must be in the frame for the whole clip;
- `min_len` / `max_len`: clip length in seconds;
- `overlay_required`: the streamer's own stream overlay kept in the crop (needs a full-frame layout,
  which today's renderer does not have: such a clip fails until Render v2 provides one).
Required tags and mentions are checked at caption time (PR 4).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BriefResult:
    passed: bool
    failures: tuple[str, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)

    def as_json(self, campaign_slug: str) -> dict[str, Any]:
        return {
            "campaign": campaign_slug,
            "passed": self.passed,
            "failures": list(self.failures),
            "facts": dict(self.facts),
        }


def check(
    brief: Mapping[str, Any],
    *,
    has_facecam: bool,
    duration_s: float | None,
    creator_in_campaign: bool,
) -> BriefResult:
    failures: list[str] = []
    if not creator_in_campaign:
        failures.append("the clip's streamer is not one of the campaign's streamers")
    if bool(brief.get("streamer_on_screen", True)) and not has_facecam:
        failures.append("no face cam found: the streamer would not be on screen")
    if duration_s is not None:
        low, high = brief.get("min_len"), brief.get("max_len")
        if low is not None and duration_s < float(low):
            failures.append(f"clip is {duration_s:.1f} s, the brief wants at least {float(low):g} s")
        if high is not None and duration_s > float(high):
            failures.append(f"clip is {duration_s:.1f} s, the brief allows at most {float(high):g} s")
    if bool(brief.get("overlay_required", False)):
        failures.append("the brief needs the stream overlay kept, which the renderer cannot do yet")
    facts = {"has_facecam": has_facecam, "duration_s": duration_s, "creator_in_campaign": creator_in_campaign}
    return BriefResult(not failures, tuple(failures), facts)


def summary(failures: Sequence[str]) -> str:
    return "; ".join(failures) if failures else "fits the brief"

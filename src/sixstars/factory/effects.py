"""Pure timing of the sound effects (docs/27) and the reader of ffmpeg's `ebur128` momentary loudness
(a reading every 100 ms), used by the adaptive cut (`anchor`).

The punch-ins that used to follow the loudness envelope were removed by Render v2 (D119).
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sixstars.factory.bleep import Span
from sixstars.factory.style import StyleRules

SfxKind = Literal["pop", "whoosh", "impact", "boom"]

# Momentary loudness integrates the last 400 ms and is reported at the end of that window, so a reading
# describes the audio about 200 ms earlier.
MOMENTARY_LAG_S = 0.2
# The whoosh is a rising swoosh about 0.35 s long that peaks at its end: it starts this early.
WHOOSH_LEAD_S = 0.3
# A keyword pop this close to another effect is dropped, so effects never pile up.
POP_CLEAR_S = 0.4

_MOMENTARY = re.compile(r"t:\s*(-?[\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+|-?inf|nan)", re.IGNORECASE)


def parse_momentary(stderr: str) -> list[tuple[float, float]]:
    """The `(time_s, momentary LUFS)` readings ebur128 prints, shifted back by the filter's lag.

    Unreadable or non-finite readings are skipped.
    """
    readings: list[tuple[float, float]] = []
    for match in _MOMENTARY.finditer(stderr):
        try:
            time_s, loudness = float(match.group(1)), float(match.group(2))
        except ValueError:
            continue
        if math.isfinite(time_s) and math.isfinite(loudness):
            readings.append((max(0.0, time_s - MOMENTARY_LAG_S), loudness))
    return readings


@dataclass(frozen=True, slots=True)
class SfxCue:
    """One sound effect at a time in the clip."""

    kind: SfxKind
    time_s: float


def plan_sfx(
    *,
    duration_s: float,
    keyword_times: Sequence[float] = (),
    cuts: Sequence[Span] = (),
    payoff_s: float | None = None,
    rules: StyleRules,
) -> tuple[SfxCue, ...]:
    """Every sound effect of the clip, in time order.

    A whoosh leads into each B-roll cut of the montage (`cuts`) and an impact lands on its first frame; a
    bass hit lands on the payoff (`rules.payoff_fx`); keyword pops fill the rest, except within
    `POP_CLEAR_S` of another effect. Nothing plays outside the clip.
    """
    cues: list[SfxCue] = []
    for span in cuts:
        cues.append(SfxCue("whoosh", max(0.0, span.start_s - WHOOSH_LEAD_S)))
        cues.append(SfxCue("impact", span.start_s))
    if rules.payoff_fx and payoff_s is not None:
        cues.append(SfxCue("boom", payoff_s))
    taken = [cue.time_s for cue in cues]
    for time_s in keyword_times:
        if all(abs(time_s - other) >= POP_CLEAR_S for other in taken):
            cues.append(SfxCue("pop", time_s))
    kept = [cue for cue in cues if 0.0 <= cue.time_s < duration_s]
    return tuple(sorted(kept, key=lambda cue: (cue.time_s, cue.kind)))

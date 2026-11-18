"""Pure timing of the edit effects (docs/27): where the punch-ins go and when each sound effect plays.

The punch-ins follow the audio: the clips the style is modelled on cut to a full-frame close-up of the
streamer for about a second exactly when they scream. The loudness envelope comes from ffmpeg's `ebur128`
filter (momentary loudness, a reading every 100 ms) over the source audio of the final window.
"""

import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sixstars.factory.bleep import Span
from sixstars.factory.style import StyleRules

SfxKind = Literal["pop", "whoosh", "impact", "boom"]

# Momentary loudness integrates the last 400 ms and is reported at the end of that window, so a reading
# describes the audio about 200 ms earlier.
MOMENTARY_LAG_S = 0.2
# Readings below this are silence (ebur128 reports -120 LUFS and lower for digital silence).
SILENCE_LUFS = -70.0
# No punch-in during the first half second (the hook frame must be the clip itself) or the last 0.3 s.
PUNCHIN_HEAD_S = 0.5
PUNCHIN_TAIL_S = 0.3
# The cut lands this long before the loudest reading, so the scream starts on the close-up.
PUNCHIN_LEAD_S = 0.15
# A punch-in keeps this far from the payoff, which has its own effect.
PAYOFF_CLEAR_S = 1.0
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


def select_punchins(
    envelope: Sequence[tuple[float, float]],
    duration_s: float,
    rules: StyleRules,
    *,
    avoid_s: Sequence[float] = (),
) -> tuple[Span, ...]:
    """Up to `rules.punchin_max` punch-in spans (clip time), loudest moments first, returned in time order.

    A reading qualifies when it is at least `rules.punchin_db` above the median of the non-silent readings.
    Spans are `rules.punchin_len_s` long, start `PUNCHIN_LEAD_S` before their reading, stay inside the clip
    (clear of its first half second and last 0.3 s), keep `rules.punchin_min_gap_s` between each other and
    `PAYOFF_CLEAR_S` from every time in `avoid_s`.
    """
    if not rules.punchins or duration_s <= 0:
        return ()
    loud = [(t, m) for t, m in envelope if m > SILENCE_LUFS]
    if len(loud) < 3:
        return ()
    threshold = statistics.median(m for _, m in loud) + rules.punchin_db
    length = rules.punchin_len_s
    latest_start = duration_s - PUNCHIN_TAIL_S - length
    chosen: list[Span] = []
    for time_s, loudness in sorted(loud, key=lambda reading: (-reading[1], reading[0])):
        if loudness < threshold or len(chosen) >= rules.punchin_max:
            break
        start = time_s - PUNCHIN_LEAD_S
        if not PUNCHIN_HEAD_S <= start <= latest_start:
            continue
        span = Span(round(start, 3), round(start + length, 3))
        if any(span.start_s - PAYOFF_CLEAR_S < t < span.end_s + PAYOFF_CLEAR_S for t in avoid_s):
            continue
        gap = rules.punchin_min_gap_s
        if any(span.start_s < other.end_s + gap and other.start_s < span.end_s + gap for other in chosen):
            continue
        chosen.append(span)
    return tuple(sorted(chosen, key=lambda span: span.start_s))


@dataclass(frozen=True, slots=True)
class SfxCue:
    """One sound effect at a time in the clip."""

    kind: SfxKind
    time_s: float


def plan_sfx(
    *,
    duration_s: float,
    keyword_times: Sequence[float] = (),
    punchins: Sequence[Span] = (),
    payoff_s: float | None = None,
    rules: StyleRules,
) -> tuple[SfxCue, ...]:
    """Every sound effect of the clip, in time order.

    A whoosh leads into each punch-in and an impact lands on its first frame (`rules.cut_sfx`); a bass hit
    lands on the payoff (`rules.payoff_fx`); keyword pops fill the rest, except within `POP_CLEAR_S` of
    another effect. Nothing plays outside the clip.
    """
    cues: list[SfxCue] = []
    if rules.cut_sfx:
        for span in punchins:
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

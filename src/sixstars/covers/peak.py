"""The clip's peak in clip time: where the cover engine looks for its frames (docs/29a stage 1).

The cut anchor (loudness, chat and speech together, `factory.anchor`, only with `cut.adaptive`) when it is
inside the clip, else the clip writer's payoff, else the busiest chat second moved back by the chat lag.
Pure.
"""

from collections.abc import Sequence

from sixstars.factory.anchor import CHAT_LAG_S

# A peak needs at least this many chat lines in its best second to count as a chat peak.
CHAT_PEAK_MIN_LINES = 3


def _inside(t: float | None, duration_s: float) -> bool:
    return t is not None and 0.0 <= t < duration_s


def chat_peak_s(chat_times: Sequence[float], duration_s: float) -> float | None:
    """The clip-time second with the most chat lines, moved back by the chat lag (people type after the
    moment); None when no second has `CHAT_PEAK_MIN_LINES` lines."""
    counts: dict[int, int] = {}
    for t in chat_times:
        second = int(t - CHAT_LAG_S)
        if 0 <= second < duration_s:
            counts[second] = counts.get(second, 0) + 1
    if not counts:
        return None
    second, count = max(counts.items(), key=lambda item: (item[1], -item[0]))
    return float(second) + 0.5 if count >= CHAT_PEAK_MIN_LINES else None


def choose_peak(
    duration_s: float,
    *,
    anchor_s: float | None = None,
    payoff_s: float | None = None,
    chat_times: Sequence[float] = (),
) -> tuple[float | None, str]:
    """The peak (clip time) and where it came from: `anchor`, `payoff`, `chat`, or (None, "")."""
    if anchor_s is not None and _inside(anchor_s, duration_s):
        return round(anchor_s, 2), "anchor"
    if payoff_s is not None and _inside(payoff_s, duration_s):
        return round(payoff_s, 2), "payoff"
    chat = chat_peak_s(chat_times, duration_s)
    if chat is not None:
        return chat, "chat"
    return None, ""

"""`cut.adaptive` (scoring_config): find the loudest point of voice, chat and transcript together and tell
the clip writer to cut a short clip around it (docs/taste-profile.md sections 1 and 2).

Everything here is pure except `measure_loudness`, which runs ffmpeg's `ebur128` over the padded window.
All times are seconds from the start of the padded (window) file.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any, Final

from sixstars.factory import effects, tools
from sixstars.factory.lines import Word
from sixstars.logging import get_logger
from sixstars.scoring import config as scoring_config

# Weights of the three signals in the combined curve, and how far chat lags the moment it reacts to.
LOUD_WEIGHT: Final = 0.5
CHAT_WEIGHT: Final = 0.3
VOICE_WEIGHT: Final = 0.2
CHAT_LAG_S: Final = 3
SILENCE_LUFS: Final = -70.0
SEARCH_BEFORE_FLOOR_S: Final = 20.0
SEARCH_AFTER_FLOOR_S: Final = 15.0
ENVELOPE_TIMEOUT_S: Final = 60.0


@dataclass(frozen=True, slots=True)
class CutRules:
    """The `cut.adaptive` numbers."""

    min_s: float
    max_s: float
    target_min_s: float
    target_max_s: float
    lead_s: tuple[float, float]
    tail_s: tuple[float, float]
    anchor: bool

    @classmethod
    def from_flag(cls, flag: Mapping[str, Any]) -> "CutRules":
        min_s = scoring_config.number(flag, "min_s", 6.0)
        max_s = max(min_s, scoring_config.number(flag, "max_s", 25.0))
        target_min = min(max(scoring_config.number(flag, "target_min_s", 12.0), min_s), max_s)
        target_max = min(max(scoring_config.number(flag, "target_max_s", 18.0), target_min), max_s)
        return cls(
            min_s=min_s,
            max_s=max_s,
            target_min_s=target_min,
            target_max_s=target_max,
            lead_s=scoring_config.pair(flag, "lead_s", (1.0, 3.0)),
            tail_s=scoring_config.pair(flag, "tail_s", (1.0, 3.0)),
            anchor=flag.get("anchor", True) is True,
        )


@dataclass(frozen=True, slots=True)
class Anchor:
    """The anchor second and the per-signal values there, for the log."""

    t_s: float
    loud_lufs: float | None
    chat_per_s: float
    words_per_s: float
    combined: float


def per_second_loudness(readings: Sequence[tuple[float, float]], seconds: int) -> list[float | None]:
    """Mean momentary loudness of every whole second (None for a second without a reading)."""
    sums = [0.0] * seconds
    counts = [0] * seconds
    for t, lufs in readings:
        index = int(t)
        if 0 <= index < seconds and lufs > SILENCE_LUFS:
            sums[index] += lufs
            counts[index] += 1
    return [sums[i] / counts[i] if counts[i] else None for i in range(seconds)]


def per_second_counts(times: Sequence[float], seconds: int, shift_s: int = 0) -> list[float]:
    """How many of `times` fall in each second, moved `shift_s` seconds earlier."""
    counts = [0.0] * seconds
    for t in times:
        index = int(math.floor(t)) - shift_s
        if 0 <= index < seconds:
            counts[index] += 1
    return counts


def _z(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    mean, std = fmean(values), pstdev(values)
    return [0.0 if std <= 1e-9 else (value - mean) / std for value in values]


def _smooth(values: Sequence[float], radius: int = 1) -> list[float]:
    out: list[float] = []
    for i in range(len(values)):
        window = values[max(0, i - radius) : i + radius + 1]
        out.append(fmean(window) if window else 0.0)
    return out


def find_anchor(
    window_s: float,
    floor_s: float,
    *,
    loudness: Sequence[tuple[float, float]],
    chat_times: Sequence[float],
    words: Sequence[Word],
) -> Anchor | None:
    """The second where loudness, chat (moved back by `CHAT_LAG_S`) and speech together peak, searched
    from `SEARCH_BEFORE_FLOOR_S` before the spike floor to `SEARCH_AFTER_FLOOR_S` after it. None when there
    is no signal at all."""
    seconds = int(math.ceil(window_s))
    if seconds <= 0:
        return None
    loud = per_second_loudness(loudness, seconds)
    known = [value for value in loud if value is not None]
    floor_lufs = min(known) if known else SILENCE_LUFS
    loud_filled = [floor_lufs if value is None else value for value in loud]
    chat = _smooth(per_second_counts(chat_times, seconds, CHAT_LAG_S), 2)
    voice = _smooth(per_second_counts([word.start_s for word in words], seconds), 1)
    if not known and not any(chat) and not any(voice):
        return None
    combined = [
        LOUD_WEIGHT * a + CHAT_WEIGHT * b + VOICE_WEIGHT * c
        for a, b, c in zip(_z(loud_filled), _z(chat), _z(voice), strict=True)
    ]
    lo = max(0, int(floor_s - SEARCH_BEFORE_FLOOR_S))
    hi = min(seconds, int(floor_s + SEARCH_AFTER_FLOOR_S) + 1)
    if hi <= lo:
        lo, hi = 0, seconds
    best = max(range(lo, hi), key=lambda i: (combined[i], -abs(i - floor_s)))
    return Anchor(
        t_s=float(best) + 0.5,
        loud_lufs=None if loud[best] is None else round(float(loud[best] or 0.0), 1),
        chat_per_s=round(chat[best], 2),
        words_per_s=round(voice[best], 2),
        combined=round(combined[best], 3),
    )


def guidance(rules: CutRules, anchor: Anchor | None) -> str:
    """The length guidance line for prompt P2."""
    parts = [
        f"Length guidance: winning clips are short. Aim for {rules.target_min_s:g}-{rules.target_max_s:g} s.",
        f"Use as little as {rules.min_s:g} s when the moment is a single reaction, and go up to "
        f"{rules.max_s:g} s only for a story with a clear setup and payoff.",
        f"Start {rules.lead_s[0]:g}-{rules.lead_s[1]:g} s before the setup line and end "
        f"{rules.tail_s[0]:g}-{rules.tail_s[1]:g} s after the reaction fades.",
        "A quotable line said loudly is a payoff even when nothing happens on screen.",
    ]
    if anchor is not None:
        parts.append(
            f"The loudest point of voice, chat and transcript together is at about {anchor.t_s:.1f} s: "
            "the cut should contain it, usually as the payoff."
        )
    return " ".join(parts)


def measure_loudness(
    source: Path, start_s: float, duration_s: float, *, ffmpeg: str = "ffmpeg"
) -> list[tuple[float, float]]:
    """Momentary loudness (`ebur128`, every 100 ms) of `duration_s` seconds of `source` from `start_s`, on
    the window clock. Empty when it cannot be measured (never fails the render)."""
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-ss",
        f"{max(0.0, start_s):.3f}",
        "-t",
        f"{max(0.1, duration_s):.3f}",
        "-i",
        str(source),
        "-vn",
        "-af",
        "ebur128",
        "-f",
        "null",
        "-",
    ]
    try:
        result = tools.run(command, timeout_s=ENVELOPE_TIMEOUT_S, what="anchor envelope")
    except tools.FfmpegError as exc:
        get_logger("factory").warning("anchor_envelope_failed", error=str(exc)[:200])
        return []
    return effects.parse_momentary(result.stderr.decode("utf-8", errors="replace"))

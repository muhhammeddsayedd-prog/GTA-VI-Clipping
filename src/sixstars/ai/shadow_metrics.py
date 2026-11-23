"""Comparison of shadow answers with the production ones (AI cost plan B1, D117). Pure functions; the
read-only `scripts/shadow_report.py` feeds them rows of `ai_shadow`.

Switch criteria agreed with the owner (2026-11-23):

- P9V: pass/fail agreement at each clip's pass line of at least 90%, Spearman of at least 0.8, and the
  number of clips the shadow model passes that production failed, reported on its own.
- P2: trim points within 1 s on at least 90% of clips, every safety and spoiler flag production raised also
  raised (recall 100%), valid JSON on at least 99% of calls; copy quality by a blind review (separate step).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from sixstars.ai.calls import bounded_score
from sixstars.ai.schemas import VisualClipScore

TRIM_TOLERANCE_S = 1.0
P9V_AGREEMENT_MIN = 0.9
P9V_SPEARMAN_MIN = 0.8
P2_TRIM_MIN = 0.9
P2_VALID_MIN = 0.99


def ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks, ties sharing the mean rank. Pure."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        mean = (position + end) / 2 + 1
        for index in order[position : end + 1]:
            result[index] = mean
        position = end + 1
    return result


def spearman(first: Sequence[float], second: Sequence[float]) -> float | None:
    """Spearman's rank correlation (Pearson on the ranks); None for fewer than 3 pairs or no spread."""
    if len(first) != len(second) or len(first) < 3:
        return None
    a, b = ranks(first), ranks(second)
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a == 0 or var_b == 0:
        return None
    return float(cov / (var_a * var_b) ** 0.5)


def shadow_visual_score(output: Any) -> int | None:
    """The shadow model's P9V score held to the rubric (`bounded_score`, as production does), or None."""
    if not isinstance(output, Mapping):
        return None
    try:
        return bounded_score(VisualClipScore.model_validate(output))
    except ValidationError:
        return None


@dataclass(frozen=True, slots=True)
class VisualPair:
    primary: int
    shadow: int | None  # None: no usable answer
    pass_line: float


@dataclass(frozen=True, slots=True)
class VisualReport:
    pairs: int
    usable: int
    agreement: float | None
    spearman: float | None
    shadow_passes_primary_fails: int
    shadow_fails_primary_passes: int

    @property
    def meets_criteria(self) -> bool:
        return (
            self.agreement is not None
            and self.agreement >= P9V_AGREEMENT_MIN
            and self.spearman is not None
            and self.spearman >= P9V_SPEARMAN_MIN
        )


def visual_report(pairs: Sequence[VisualPair]) -> VisualReport:
    usable = [pair for pair in pairs if pair.shadow is not None]
    agree = sum(1 for p in usable if (p.primary >= p.pass_line) == ((p.shadow or 0) >= p.pass_line))
    return VisualReport(
        pairs=len(pairs),
        usable=len(usable),
        agreement=agree / len(usable) if usable else None,
        spearman=spearman([float(p.primary) for p in usable], [float(p.shadow or 0) for p in usable]),
        shadow_passes_primary_fails=sum(1 for p in usable if (p.shadow or 0) >= p.pass_line > p.primary),
        shadow_fails_primary_passes=sum(1 for p in usable if p.primary >= p.pass_line > (p.shadow or 0)),
    )


def _flags(output: Mapping[str, Any]) -> set[str]:
    safety = output.get("safety")
    flags = (
        {name for name, value in safety.items() if value is True} if isinstance(safety, Mapping) else set()
    )
    if output.get("spoiler") is True:
        flags.add("spoiler")
    return flags


def _number(output: Mapping[str, Any], key: str) -> float | None:
    value = output.get(key)
    return float(value) if isinstance(value, int | float) else None


@dataclass(frozen=True, slots=True)
class WriterReport:
    pairs: int
    valid_rate: float | None
    trim_within_1s: float | None
    flag_recall: float | None  # share of production's safety/spoiler flags the shadow also raised
    missed_flags: int
    reject_agreement: float | None

    @property
    def meets_criteria(self) -> bool:
        return (
            self.valid_rate is not None
            and self.valid_rate >= P2_VALID_MIN
            and self.trim_within_1s is not None
            and self.trim_within_1s >= P2_TRIM_MIN
            and self.missed_flags == 0
        )


def writer_report(pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any] | None]]) -> WriterReport:
    """`pairs` of (production reply, shadow reply or None)."""
    valid = [(primary, shadow) for primary, shadow in pairs if isinstance(shadow, Mapping)]
    trims: list[bool] = []
    flagged = caught = 0
    rejects_agree = 0
    for primary, shadow in valid:
        starts = (_number(primary, "start_s"), _number(shadow, "start_s"))
        ends = (_number(primary, "end_s"), _number(shadow, "end_s"))
        if None not in starts and None not in ends:
            trims.append(
                abs((starts[0] or 0) - (starts[1] or 0)) <= TRIM_TOLERANCE_S
                and abs((ends[0] or 0) - (ends[1] or 0)) <= TRIM_TOLERANCE_S
            )
        wanted = _flags(primary)
        flagged += len(wanted)
        caught += len(wanted & _flags(shadow))
        rejects_agree += bool(primary.get("reject")) == bool(shadow.get("reject"))
    return WriterReport(
        pairs=len(pairs),
        valid_rate=len(valid) / len(pairs) if pairs else None,
        trim_within_1s=sum(trims) / len(trims) if trims else None,
        flag_recall=caught / flagged if flagged else None,
        missed_flags=flagged - caught,
        reject_agreement=rejects_agree / len(valid) if valid else None,
    )

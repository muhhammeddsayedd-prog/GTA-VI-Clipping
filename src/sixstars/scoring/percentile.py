"""How strong a spike is for its own channel: the percentile of its `score_z` among the creator's recent
spikes (`scoring_config.percentile.history`). A creator with too little history is compared with every
creator of the same platform instead.

`percentile_of` is pure; `spike_percentile` reads the history.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sixstars import db
from sixstars.logging import get_logger
from sixstars.scoring import config

Basis = Literal["channel", "platform", "none"]
_log = get_logger("scoring")


@dataclass(frozen=True, slots=True)
class Strength:
    """The percentile (0-100) of a spike and what it was measured against."""

    percentile: float | None
    basis: Basis
    history: int

    def at_least(self, percentile: float) -> bool:
        return self.percentile is not None and self.percentile >= percentile


def percentile_of(value: float, history: Sequence[float]) -> float | None:
    """The share of `history` below `value` (ties count half), as 0-100. None for an empty history."""
    if not history:
        return None
    ordered = sorted(history)
    below = bisect_left(ordered, value)
    ties = bisect_right(ordered, value) - below
    return round(100.0 * (below + 0.5 * ties) / len(ordered), 2)


def _history(sql: str, params: tuple[object, ...]) -> list[float]:
    return [float(row["score_z"]) for row in db.fetch_all(sql, params) if row["score_z"] is not None]


def spike_percentile(creator_id: str | None, score_z: float, exclude_spike_id: str | None = None) -> Strength:
    """The strength of a spike with `score_z` for `creator_id`. Never raises: a failed read gives
    `Strength(None, "none", 0)`, which every rule treats as "not strong"."""
    params = config.section("percentile.history")
    days = config.number(params, "lookback_days", 14.0)
    min_history = int(config.number(params, "min_history", 20.0))
    max_rows = int(config.number(params, "max_rows", 500.0))
    exclude = exclude_spike_id or "00000000-0000-0000-0000-000000000000"
    try:
        if creator_id:
            own = _history(
                """
                select score_z from spikes
                where creator_id = %s and id <> %s and detected_at > now() - make_interval(days => %s)
                order by detected_at desc limit %s
                """,
                (creator_id, exclude, int(days), max_rows),
            )
            if len(own) >= min_history:
                return Strength(percentile_of(score_z, own), "channel", len(own))
        pool = _history(
            """
            select s.score_z from spikes s
            where s.id <> %s and s.detected_at > now() - make_interval(days => %s)
              and (%s::uuid is null or exists (
                    select 1 from creators c, creators me
                    where c.id = s.creator_id and me.id = %s::uuid and c.platform = me.platform))
            order by s.detected_at desc limit %s
            """,
            (exclude, int(days), creator_id, creator_id, max_rows),
        )
        if len(pool) >= min_history:
            return Strength(percentile_of(score_z, pool), "platform", len(pool))
        return Strength(None, "none", len(pool))
    except Exception as exc:  # noqa: BLE001 - a strength we cannot measure is simply "not strong"
        _log.warning("spike_percentile_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return Strength(None, "none", 0)


def strength_of_spike(spike_id: str | None) -> Strength:
    """The strength of a stored spike (by id)."""
    if not spike_id:
        return Strength(None, "none", 0)
    try:
        row = db.fetch_one("select creator_id::text as c, score_z from spikes where id = %s", (spike_id,))
    except Exception as exc:  # noqa: BLE001
        _log.warning("spike_percentile_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return Strength(None, "none", 0)
    if row is None or row["score_z"] is None:
        return Strength(None, "none", 0)
    return spike_percentile(row["c"], float(row["score_z"]), spike_id)

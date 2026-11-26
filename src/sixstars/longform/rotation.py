"""Day format rotation (M20 go-live): mix day, creator day, mix day, ...

Pure. The next episode takes the format after the last *built* episode's format in `longform.format_cycle`,
so a skipped day (thin pool) or a creator day that fell back to a mix day does not break the alternation.
"""

from collections.abc import Sequence

FORMATS = ("mix", "creator")


def format_for(last_format: str | None, cycle: Sequence[str]) -> str:
    """The format of the next episode. No previous episode (or an unknown one): the cycle's first entry."""
    known = [f for f in cycle if f in FORMATS] or ["mix"]
    if last_format not in known:
        return known[0]
    return known[(known.index(last_format) + 1) % len(known)]

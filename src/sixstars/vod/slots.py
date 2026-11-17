"""Arithmetic of the VOD lane's concurrency slots: how many videos may be in work for a given number of
live workers, and how long a job that found no free slot waits. Pure, no database. Added by
fix-stale-processing.
"""

from typing import Final

MAX_DOUBLINGS: Final = 30  # keeps 2**n finite for a job deferred for days; the cap applies long before


def effective_concurrency(max_concurrent: int, live_workers: int) -> int:
    """Videos the lane may process at once: `vod.max_concurrent`, but never every live worker of the
    `normal` queue (one stays free for `clip.render`), and never fewer than one."""
    return max(1, min(max_concurrent, live_workers - 1))


def deferral_delay(deferrals: int, base_s: float, cap_s: float, jitter: float, rand: float) -> float:
    """Seconds a job waits after finding no free slot for the `deferrals + 1`-th time in a row.

    The wait doubles from `base_s` up to `cap_s`; `jitter` (0..1) shortens it by up to that fraction,
    scaled by `rand` (0..1), so jobs deferred together do not wake together. Never above the cap.
    """
    base = max(0.0, base_s)
    grown = min(max(base, cap_s), base * 2.0 ** min(max(0, deferrals), MAX_DOUBLINGS))
    spread = min(1.0, max(0.0, jitter)) * min(1.0, max(0.0, rand))
    return grown * (1.0 - spread)

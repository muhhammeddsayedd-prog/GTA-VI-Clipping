"""Hourly slot time maths: the slot a moment belongs to, the next slot and the jitter inside a slot. Pure."""

import hashlib
from datetime import datetime, timedelta

from sixstars.posting.models import ensure_utc

SLOT_LENGTH = timedelta(hours=1)


def slot_start(now: datetime) -> datetime:
    """The hourly slot `now` belongs to: the top of the current UTC hour."""
    return ensure_utc(now, "now").replace(minute=0, second=0, microsecond=0)


def next_slot(now: datetime) -> datetime:
    """The first slot strictly after `now` (rolls over midnight and month ends)."""
    return slot_start(now) + SLOT_LENGTH


def check_slot(slot_at: datetime) -> datetime:
    """Returns `slot_at` in UTC, refusing a time that is not exactly on the hour."""
    slot = ensure_utc(slot_at, "slot_at")
    if slot != slot_start(slot):
        raise ValueError(f"slot_at must be exactly on the hour, got {slot.isoformat()}")
    return slot


def jitter_offset_s(slot_at: datetime, jitter_max_s: int, seed: str = "") -> int:
    """Seconds to wait after `slot_at` before posting: a whole number in 0..`jitter_max_s`, both included.

    The offset is derived from the slot time and `seed` (SHA-256), so it looks random from slot to slot but
    is the same every time it is asked for one slot. Two triggers for one slot (n8n and the scheduler
    failover) therefore agree on the posting time.
    """
    slot = check_slot(slot_at)
    if jitter_max_s <= 0:
        return 0
    digest = hashlib.sha256(f"{slot.isoformat()}|{seed}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (jitter_max_s + 1)


def jitter_window(slot_at: datetime, jitter_max_s: int) -> tuple[datetime, datetime]:
    """Earliest and latest planned posting time of a slot."""
    slot = check_slot(slot_at)
    return slot, slot + timedelta(seconds=max(0, jitter_max_s))


def fire_at(slot_at: datetime, jitter_max_s: int, seed: str = "") -> datetime:
    """The planned posting time of a slot: `slot_at` plus its jitter offset."""
    return check_slot(slot_at) + timedelta(seconds=jitter_offset_s(slot_at, jitter_max_s, seed))


def slot_is_open(slot_at: datetime, now: datetime) -> bool:
    """True while `now` is inside the slot's own hour: `slot_at <= now < slot_at + 1 h`."""
    slot = check_slot(slot_at)
    return slot <= ensure_utc(now, "now") < slot + SLOT_LENGTH

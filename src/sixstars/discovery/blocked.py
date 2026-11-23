"""What discovery does about a platform that cannot be downloaded or recorded from this machine right now
(fix-blocked-platform-queue). Pure decisions; `run.py` loads the state and applies them.

Seen on the staging server: YouTube refused every download, the blocked videos stayed in status `new`,
filled `discovery.max_vod_backlog` and so starved Twitch, which worked. The rules:

- **VOD.** A platform is blocked while the VOD lane's record (`vod.source_health`, see `vod/health.py`)
  has a refusal younger than `discovery.blocked_platform_stale_s`. Its candidates are still scored and
  stored, but none of its videos is queued while one of its videos is open (`new` or `processing`): the
  lane tries the waiting ones again every `vod.blocked_backoff_s`, and that retry is the probe which
  notices the end of the block. Only when no video of the platform is open is one queued, as the probe.
- **Backlog.** Videos waiting out a block (`skip_reason` starts with `download_blocked`) do not count
  towards `discovery.max_vod_backlog`. At most `discovery.max_blocked_waiting` of them are kept per
  platform; the oldest beyond that are closed as `skipped`.
- **Live.** A platform the live lane is backing off from (`live.source_health`, `skip_until` in the
  future) gets no new slot. After the back-off it gets at most one, the live lane's probe.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sixstars.clock import DATE_TIME, fmt_local
from sixstars.live import health as live_health

BLOCKED_REASON_PREFIX: Final = "download_blocked"  # how `vod.process` marks a video waiting out a block
SKIP_VOD: Final = "platform_blocked"
SKIP_LIVE: Final = "live_platform_blocked"


@dataclass(frozen=True)
class Queue:
    """The open videos of one platform."""

    waiting: int = 0  # status `new`, not waiting out a block: what the backlog cap counts
    blocked: int = 0  # status `new`, waiting out a download block
    processing: int = 0

    @property
    def open(self) -> int:
        return self.waiting + self.blocked + self.processing


@dataclass(frozen=True)
class BlockedVideo:
    """A video in status `new` that waits out a download block."""

    id: str
    platform: str
    created_at: datetime


def vod_room(max_per_run: int, max_backlog: int, queues: Mapping[str, Queue]) -> int:
    """How many videos this pass may queue: the per-run maximum, less when the backlog is nearly full.
    Videos waiting out a block are not part of the backlog, so they cannot stop another platform."""
    backlog = sum(queue.waiting for queue in queues.values())
    return max(0, min(max_per_run, max_backlog - backlog))


def vod_limits(blocked: Collection[str], queues: Mapping[str, Queue]) -> dict[str, int]:
    """Videos each blocked platform may get in this pass: none while one of its videos is open (the VOD
    lane's retry of it is the probe), else one (the probe)."""
    return {platform: 0 if queues.get(platform, Queue()).open else 1 for platform in sorted(blocked)}


def overflow(waiting: Sequence[BlockedVideo], blocked: Collection[str], keep: int) -> list[BlockedVideo]:
    """The videos to give up on: of every blocked platform, all but the `keep` newest that wait out the
    block. Videos of a platform that is no longer blocked are left alone (they are draining)."""
    kept: dict[str, int] = {}
    dropped: list[BlockedVideo] = []
    for video in sorted(waiting, key=lambda item: (item.created_at, item.id), reverse=True):
        if video.platform not in blocked:
            continue
        kept[video.platform] = kept.get(video.platform, 0) + 1
        if kept[video.platform] > keep:
            dropped.append(video)
    return dropped[::-1]


def live_limits(
    state: Mapping[str, Mapping[str, Any]],
    now: datetime,
    access: Callable[[str], str | None] | None = None,
) -> dict[str, int]:
    """New live slots each platform in the live lane's health state may get: none while it backs off,
    one once the back-off is over (the live lane probes with it). `access` says how a platform is
    reached now, as in `live.health.backing_off`."""
    limits: dict[str, int] = {}
    for platform, entry in sorted(state.items()):
        if not entry.get("blocks"):
            continue
        described = access(platform) if access is not None else None
        limits[platform] = 0 if live_health.backing_off(state, platform, now, described) else 1
    return limits


def vod_note(platform: str, since: datetime, held: int, queued: int, queue: Queue) -> str:
    """The one line of the pass summary about a platform whose downloads are blocked."""
    probe = f", {queued} queued as a probe" if queued else ""
    return (
        f"{platform} downloads are blocked since {fmt_local(since, DATE_TIME)}: {held} video(s) not "
        f"queued{probe}, {queue.blocked} waiting for the VOD lane's retry"
    )


def live_notes(limits: Mapping[str, int], held: Mapping[str, int], seen: Collection[str]) -> list[str]:
    """The lines of the pass summary about platforms the live lane cannot record from. `seen` holds the
    platforms that had a live stream in this pass."""
    closed = sorted(platform for platform, limit in limits.items() if limit <= 0)
    if seen and all(platform in closed for platform in seen):
        return [f"no live slot assigned: every platform with streams is blocked ({', '.join(closed)})"]
    return [
        f"{platform} live recording is blocked: {count} stream(s) got no slot"
        for platform, count in sorted(held.items())
    ]

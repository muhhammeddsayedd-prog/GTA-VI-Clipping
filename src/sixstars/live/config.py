"""Every setting the live lane reads, loaded into `LiveConfig`.

Keys seeded by migration `0002` are required (`live.max_slots`, `live.segment_s`, `live.buffer_s`,
`live.max_reconnects`). Keys added by `0012_live.sql` fall back to `DEFAULTS` (the values that migration
seeds), so the lane runs the same on a database where the migration has not been applied yet.
"""

from dataclasses import dataclass
from typing import Any, Final

from sixstars import settings

# Values of migration 0012 (see docs/16-settings-reference.md). Only used when a key is missing.
DEFAULTS: Final[dict[str, Any]] = {
    "live.poll_s": 15,
    "live.max_height": 1080,
    "live.reconnect_backoff_s": 10,
    "live.reconnect_reset_s": 300,
    "live.start_timeout_s": 60,
    "live.stall_timeout_s": 45,
    "live.stop_timeout_s": 8,
    "live.blocked_backoff_s": 1800,
    "live.blocked_notify_after": 2,
    "live.max_buffer_bytes": 3_000_000_000,
    "live.min_free_bytes": 8_000_000_000,
    "live.cut_settle_s": 4,
    "live.cut_wait_max_s": 70,
    "live.express_pad_wait_s": 6,
    "live.cut_keep_s": 2700,
    "live.anchor_span_s": 300,
    "live.replay_delay_s": 900,
    "live.replay_find_interval_s": 600,
    "live.replay_find_max_s": 7200,
    "live.priority_express": 100,
    "live.priority_a": 60,
    "live.priority_b": 40,
    "live.ab_clips": True,
}


def _number(key: str) -> float:
    value = settings.get(key, DEFAULTS[key]) if key in DEFAULTS else settings.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"setting {key} must be a number, got {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class LiveConfig:
    """The live lane's settings at one moment. Times are seconds, sizes bytes."""

    max_slots: int
    segment_s: float
    buffer_s: float
    max_reconnects: int
    poll_s: float
    max_height: int
    reconnect_backoff_s: float
    reconnect_reset_s: float
    start_timeout_s: float
    stall_timeout_s: float
    stop_timeout_s: float
    blocked_backoff_s: float
    blocked_notify_after: int
    max_buffer_bytes: int
    min_free_bytes: int
    cut_settle_s: float
    cut_wait_max_s: float
    express_pad_wait_s: float
    cut_keep_s: float
    anchor_span_s: float
    replay_delay_s: float
    replay_find_interval_s: float
    replay_find_max_s: float
    priority_express: int
    priority_a: int
    priority_b: int
    ab_clips: bool

    @classmethod
    def load(cls) -> "LiveConfig":
        """Read the settings (through the 30-second cache of `sixstars.settings`)."""
        return cls(
            max_slots=max(0, int(_number("live.max_slots"))),
            segment_s=max(2.0, _number("live.segment_s")),
            buffer_s=max(60.0, _number("live.buffer_s")),
            max_reconnects=max(0, int(_number("live.max_reconnects"))),
            poll_s=max(1.0, _number("live.poll_s")),
            max_height=int(_number("live.max_height")),
            reconnect_backoff_s=_number("live.reconnect_backoff_s"),
            reconnect_reset_s=_number("live.reconnect_reset_s"),
            start_timeout_s=_number("live.start_timeout_s"),
            stall_timeout_s=_number("live.stall_timeout_s"),
            stop_timeout_s=_number("live.stop_timeout_s"),
            blocked_backoff_s=_number("live.blocked_backoff_s"),
            blocked_notify_after=max(1, int(_number("live.blocked_notify_after"))),
            max_buffer_bytes=int(_number("live.max_buffer_bytes")),
            min_free_bytes=int(_number("live.min_free_bytes")),
            cut_settle_s=_number("live.cut_settle_s"),
            cut_wait_max_s=_number("live.cut_wait_max_s"),
            express_pad_wait_s=_number("live.express_pad_wait_s"),
            cut_keep_s=_number("live.cut_keep_s"),
            anchor_span_s=_number("live.anchor_span_s"),
            replay_delay_s=_number("live.replay_delay_s"),
            replay_find_interval_s=_number("live.replay_find_interval_s"),
            replay_find_max_s=_number("live.replay_find_max_s"),
            priority_express=int(_number("live.priority_express")),
            priority_a=int(_number("live.priority_a")),
            priority_b=int(_number("live.priority_b")),
            ab_clips=bool(settings.get("live.ab_clips", DEFAULTS["live.ab_clips"])),
        )

    @classmethod
    def offline(cls, **overrides: Any) -> "LiveConfig":
        """The documented defaults without a database (the `live probe` command and unit tests)."""
        values: dict[str, Any] = {
            "max_slots": 3,
            "segment_s": 60.0,
            "buffer_s": 1800.0,
            "max_reconnects": 3,
            **{key.removeprefix("live."): value for key, value in DEFAULTS.items()},
        }
        values.update(overrides)
        return cls(**values)

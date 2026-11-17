"""Chat spike engine: bucketing, baselines, weighted z-score scoring, live and replay detection, clip window
rules and Claude confirmation.

Public API (M04): `scan_replay` and `analyse_replay` for the VOD lane, `LiveSpikeDetector` for the live
lane, `persist_spike` to store a result, `SpikeConfig.from_settings` for the parameters.
"""

from sixstars.spikes.config import SpikeConfig, SpikeConfigError, from_settings
from sixstars.spikes.detector import (
    DetectedSpike,
    LiveSpikeDetector,
    ReplayScan,
    analyse_replay,
    learn_bots,
    scan_replay,
)
from sixstars.spikes.store import persist_spike

__all__ = [
    "DetectedSpike",
    "LiveSpikeDetector",
    "ReplayScan",
    "SpikeConfig",
    "SpikeConfigError",
    "analyse_replay",
    "from_settings",
    "learn_bots",
    "persist_spike",
    "scan_replay",
]

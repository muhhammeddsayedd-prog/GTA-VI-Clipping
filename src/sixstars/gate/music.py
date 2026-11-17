"""Music detection for the gate.

The detector itself (PANNs Cnn14 on 1-second frames, loaded once per process, CPU) lives in
`sixstars.factory.music`, because the render needs the per-second flags for ducking before the gate runs.
The factory stores the result in `clips.music_ratio` (NULL when the detector could not run) and the flags
in the `clip.rendered` event. The gate only reads them: this module re-exports the detector types and the
rule that turns ratio and flags into the number the limits are compared with.
"""

from sixstars.factory.music import MusicAnalysis, MusicDetector, MusicDetectorUnavailableError
from sixstars.gate.checks import effective_music_ratio

__all__ = ["MusicAnalysis", "MusicDetector", "MusicDetectorUnavailableError", "effective_music_ratio"]

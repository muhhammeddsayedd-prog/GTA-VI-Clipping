"""Named render presets (encoder options, resolution, bitrate) for the express and normal paths. Implemented
in M06.
"""

from typing import Literal

from sixstars.factory.audio import SAMPLE_RATE
from sixstars.factory.params import FactoryParams

RenderMode = Literal["normal", "express"]

# docs/09 section 7 (fixed by the spec, not settings).
X264_PROFILE = "high"
PIXEL_FORMAT = "yuv420p"
NVENC_PRESET = "p4"
NVENC_CQ = 23
AUDIO_BITRATE = "128k"
# Bitrate ceiling for the video stream. CRF alone gave 12 Mbit/s on fast gameplay with the express preset
# (48 MB for 31 s); the ceiling keeps a 60 s clip under 50 MB without touching ordinary scenes.
MAX_VIDEO_BITRATE = "6M"
VIDEO_BUFFER_SIZE = "12M"


def uses_nvenc(params: FactoryParams, mode: RenderMode, *, nvenc_available: bool) -> bool:
    """NVENC is used for express renders only, when `factory.use_nvenc` is on and a GPU encoder exists."""
    return mode == "express" and params.use_nvenc and nvenc_available


def video_encoder_args(
    params: FactoryParams, mode: RenderMode, *, nvenc_available: bool = False
) -> list[str]:
    """ffmpeg output options for the video stream."""
    if mode not in ("normal", "express"):
        raise ValueError(f"unknown render mode {mode!r}")
    if uses_nvenc(params, mode, nvenc_available=nvenc_available):
        codec = ["-c:v", "h264_nvenc", "-preset", NVENC_PRESET, "-cq", str(NVENC_CQ)]
    else:
        preset = params.preset_express if mode == "express" else params.preset_normal
        codec = ["-c:v", "libx264", "-preset", preset, "-crf", str(params.crf)]
    rate = ["-maxrate", MAX_VIDEO_BITRATE, "-bufsize", VIDEO_BUFFER_SIZE]
    return [*codec, *rate, "-profile:v", X264_PROFILE, "-pix_fmt", PIXEL_FORMAT, "-r", str(params.fps)]


def audio_encoder_args() -> list[str]:
    """ffmpeg output options for the audio stream: AAC 128 kb/s, 48 kHz."""
    return ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(SAMPLE_RATE)]


def container_args() -> list[str]:
    """MP4 options: metadata from the source is dropped and the index is moved to the front."""
    return ["-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart"]

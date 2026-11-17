"""Measurements on the rendered file for the gate: ffprobe facts, black and frozen stretches (ffmpeg
`blackdetect` / `freezedetect`), the four frames for P5 and the P5 call itself.

Black and freeze detection look at a horizontal band of the gameplay picture only. Our own overlays never
stop moving (subtitle highlights, progress bar) and are never black, so on the whole frame a frozen or
black source would go unnoticed. The band lies below the hook box and above the subtitle line in both
layouts (`sixstars.factory.layout`). Only the clip part is analysed, not the end card.
"""

import re
from collections.abc import Sequence
from pathlib import Path

from sixstars.ai import calls
from sixstars.ai.calls import BudgetKind
from sixstars.ai.schemas import VisualVerdict
from sixstars.clients.anthropic_client import AnthropicClient
from sixstars.factory import layout, tools
from sixstars.gate.models import FileFacts, GateSettings, VisualFindings

# The band, as fractions of the frame height: from 60 px under the hook box to 150 px above the higher of
# the two subtitle lines (reference canvas 1080x1920).
BAND_TOP = (layout.HOOK_BOTTOM + 60) / layout.REF_HEIGHT
BAND_BOTTOM = (min(layout.CAPTION_Y_A, layout.CAPTION_Y_B) - 150) / layout.REF_HEIGHT
# blackdetect: a pixel darker than this share of full brightness is black (ffmpeg's default).
BLACK_PIXEL_THRESHOLD = 0.10
# Where in the clip the P5 frames are taken (docs/10, progress notes M05).
FRAME_POSITIONS = (0.10, 0.35, 0.60, 0.85)
DETECT_TIMEOUT_S = 300.0

_BLACK = re.compile(
    r"black_start:(?P<start>[\d.]+)\s+black_end:(?P<end>[\d.]+)\s+black_duration:(?P<d>[\d.]+)"
)
_FREEZE_START = re.compile(r"freeze_start:\s*(?P<start>[\d.]+)")
_FREEZE_DURATION = re.compile(r"freeze_duration:\s*(?P<d>[\d.]+)")


def parse_blackdetect(log: str) -> list[float]:
    """The durations of the black stretches ffmpeg reported, in order."""
    return [float(match["d"]) for match in _BLACK.finditer(log)]


def parse_freezedetect(log: str, analysed_s: float) -> list[float]:
    """The durations of the frozen stretches. A freeze that lasts to the end of the input has a start and
    no duration line; it is counted up to `analysed_s`."""
    durations: list[float] = []
    open_start: float | None = None
    for line in log.splitlines():
        start = _FREEZE_START.search(line)
        if start:
            open_start = float(start["start"])
            continue
        duration = _FREEZE_DURATION.search(line)
        if duration:
            durations.append(float(duration["d"]))
            open_start = None
    if open_start is not None:
        durations.append(max(0.0, analysed_s - open_start))
    return durations


def detect_filter(settings: GateSettings) -> str:
    """The ffmpeg filter chain: the gameplay band, then both detectors with the configured limits."""
    height = BAND_BOTTOM - BAND_TOP
    return (
        f"crop=iw:ih*{height:.4f}:0:ih*{BAND_TOP:.4f},"
        f"blackdetect=d={settings.max_black_s:g}:pix_th={BLACK_PIXEL_THRESHOLD:g},"
        f"freezedetect=n={settings.freeze_noise:g}:d={settings.max_freeze_s:g}"
    )


def detect_black_and_freeze(
    path: Path, clip_duration_s: float, settings: GateSettings, ffmpeg: str = "ffmpeg"
) -> tuple[float, float]:
    """The longest black and the longest frozen stretch (seconds) in the first `clip_duration_s` of the
    file; 0 when there is none of at least the configured length."""
    command = [ffmpeg, "-hide_banner", "-nostdin", "-t", f"{clip_duration_s:.3f}", "-i", str(path)]
    command += ["-an", "-vf", detect_filter(settings), "-f", "null", "-"]
    result = tools.run(command, timeout_s=DETECT_TIMEOUT_S, what="black and freeze detection")
    log = result.stderr.decode("utf-8", errors="replace")
    black = parse_blackdetect(log)
    frozen = parse_freezedetect(log, clip_duration_s)
    return max(black, default=0.0), max(frozen, default=0.0)


def measure_file(
    path: Path,
    clip_duration_s: float | None,
    settings: GateSettings,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> FileFacts | None:
    """Everything the gate needs to know about the file; None when it does not exist or is empty.

    Raises `tools.FfmpegError` when the tools cannot read it (the caller decides between a retry and a
    rejection). Black and freeze detection run on the clip part; without a clip length, on the whole file.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return None
    media = tools.probe(path, ffprobe)
    black: float | None = None
    frozen: float | None = None
    analysed = clip_duration_s if clip_duration_s and clip_duration_s > 0 else media.duration_s
    if media.has_video and analysed > 0:
        black, frozen = detect_black_and_freeze(path, min(analysed, media.duration_s), settings, ffmpeg)
    return FileFacts(
        width=media.width,
        height=media.height,
        has_video=media.has_video,
        has_audio=media.has_audio,
        duration_s=media.duration_s if media.duration_s > 0 else None,
        size_bytes=path.stat().st_size,
        longest_black_s=black,
        longest_freeze_s=frozen,
    )


def frame_times(clip_duration_s: float) -> list[float]:
    """Where the P5 frames are taken: 10%, 35%, 60% and 85% of the clip part."""
    return [round(clip_duration_s * position, 3) for position in FRAME_POSITIONS]


def sample_frames(path: Path, clip_duration_s: float, ffmpeg: str = "ffmpeg") -> list[bytes]:
    """Four JPEG frames of the rendered file."""
    return [tools.grab_frame(path, time_s, ffmpeg) for time_s in frame_times(clip_duration_s)]


def findings_from_verdict(verdict: VisualVerdict) -> VisualFindings:
    return VisualFindings(
        other_watermark=verdict.other_watermark,
        platform_logos=verdict.platform_logos,
        menu_or_loading=verdict.menu_or_loading,
        pause_or_brb_screen=verdict.pause_or_brb_screen,
        empty_facecam=verdict.empty_facecam,
        readable_subtitles=verdict.readable_subtitles,
        notes=verdict.notes,
    )


def findings_as_json(findings: VisualFindings) -> dict[str, object]:
    return {
        "other_watermark": findings.other_watermark,
        "platform_logos": findings.platform_logos,
        "menu_or_loading": findings.menu_or_loading,
        "pause_or_brb_screen": findings.pause_or_brb_screen,
        "empty_facecam": findings.empty_facecam,
        "readable_subtitles": findings.readable_subtitles,
        "notes": findings.notes,
    }


def findings_from_json(data: object) -> VisualFindings | None:
    """The stored form back into findings; None when it is not a complete verdict."""
    if not isinstance(data, dict):
        return None
    names = (
        "other_watermark",
        "platform_logos",
        "menu_or_loading",
        "pause_or_brb_screen",
        "empty_facecam",
        "readable_subtitles",
    )
    if not all(isinstance(data.get(name), bool) for name in names):
        return None
    return VisualFindings(**{name: bool(data[name]) for name in names}, notes=str(data.get("notes") or ""))


def check_frames(
    frames: Sequence[bytes],
    *,
    lane: BudgetKind = "vod",
    clip_id: str | None = None,
    client: AnthropicClient | None = None,
) -> VisualFindings:
    """Ask P5 about the frames."""
    return findings_from_verdict(calls.visual_check(frames, lane=lane, ref_id=clip_id, client=client))


def look_at_file(
    path: Path,
    clip_duration_s: float,
    *,
    lane: BudgetKind = "vod",
    clip_id: str | None = None,
    client: AnthropicClient | None = None,
    ffmpeg: str = "ffmpeg",
) -> VisualFindings:
    """Take the four frames from the rendered file and ask P5 about them."""
    return check_frames(
        sample_frames(path, clip_duration_s, ffmpeg), lane=lane, clip_id=clip_id, client=client
    )

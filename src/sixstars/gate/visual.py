"""Measurements on the rendered file for the gate: ffprobe facts, black and frozen stretches (ffmpeg
`blackdetect` / `freezedetect`), the four frames for P5 and the P5 call itself.

Black and freeze detection run twice in one ffmpeg pass, and the longer result counts:

- on the whole frame with the limits of docs/10 (`gate.checks.freeze_noise`, the default black pixel level
  of ffmpeg);
- on a horizontal band of the gameplay picture with tighter tolerances (`band_freeze_noise`,
  `band_black_pixel`). Our own overlays never stop moving (subtitle highlights, progress bar) and are
  never black, so on the whole frame a frozen or black source would go unnoticed. The band lies below the
  hook box and above the subtitle line in both layouts (`sixstars.factory.layout`). The tolerances are
  tighter there because a band of a real scene can be still or dark for a while (a parked car at night
  measured as "frozen" at the whole-frame tolerance on two of four real renders); a frozen or black
  source is exactly still or exactly black.

Only the clip part is analysed, not the end card.
"""

import re
from collections.abc import Sequence
from pathlib import Path

from sixstars.ai import calls
from sixstars.ai.schemas import VisualVerdict
from sixstars.clients.anthropic_client import AnthropicClient, BudgetKind, InvalidModelOutput
from sixstars.factory import layout, tools
from sixstars.gate.models import FileFacts, GateSettings, VisualFindings
from sixstars.logging import get_logger

# The band of layouts A and B, as fractions of the frame height: from 60 px under the hook box to 150 px
# above the higher of the two subtitle lines (reference canvas 1080x1920). Layout C has its own
# (`layout.gameplay_band`).
BAND_TOP, BAND_BOTTOM = layout.gameplay_band(None)
# blackdetect on the whole frame: a pixel darker than this share of full brightness is black (the ffmpeg
# default, which is what docs/10 means by plain `blackdetect`).
BLACK_PIXEL_THRESHOLD = 0.10
# Where in the clip the P5 frames are taken (docs/10, progress notes M05).
FRAME_POSITIONS = (0.10, 0.35, 0.60, 0.85)
# The visual score (P9V) sees the P5 frames plus extra ones, tried in this order: seconds relative to the
# chat peak, then positions in the clip (the end first: a cut closes just after its payoff).
SCORE_PEAK_OFFSETS_S = (-3.0, 0.0, -6.0, 3.0)
SCORE_EXTRA_POSITIONS = (0.975, 0.725, 0.475, 0.225)
SCORE_MIN_GAP_S = 1.0
SCORE_EDGE_S = 0.2
DETECT_TIMEOUT_S = 300.0
# How often P5 is asked when its reply cannot be read (each ask includes the client's own repair retry).
P5_ATTEMPTS = 2

_BLACK = re.compile(
    r"black_start:(?P<start>[\d.]+)\s+black_end:(?P<end>[\d.]+)\s+black_duration:(?P<d>[\d.]+)"
)
_FREEZE_START = re.compile(r"freeze_start:\s*(?P<start>[\d.]+)")
_FREEZE_DURATION = re.compile(r"freeze_duration:\s*(?P<d>[\d.]+)")
_INSTANCE = re.compile(r"^\[(?P<name>[^\]]+)\]")


def parse_blackdetect(log: str) -> list[float]:
    """The durations of the black stretches ffmpeg reported, in order (all detector instances)."""
    return [float(match["d"]) for match in _BLACK.finditer(log)]


def parse_freezedetect(log: str, analysed_s: float) -> list[float]:
    """The durations of the frozen stretches (all detector instances). A freeze that lasts to the end of
    the input has a start and no duration line; it is counted up to `analysed_s`."""
    durations: list[float] = []
    open_starts: dict[str, float] = {}
    for line in log.splitlines():
        instance = _INSTANCE.match(line.strip())
        name = instance["name"] if instance else ""
        start = _FREEZE_START.search(line)
        if start:
            open_starts[name] = float(start["start"])
            continue
        duration = _FREEZE_DURATION.search(line)
        if duration:
            durations.append(float(duration["d"]))
            open_starts.pop(name, None)
    durations += [max(0.0, analysed_s - start_s) for start_s in open_starts.values()]
    return durations


def detect_filter(settings: GateSettings, band: tuple[float, float] | None = None) -> str:
    """The ffmpeg filter graph: the whole frame with the docs/10 limits, and the gameplay band (`band`, the
    top and bottom as fractions of the frame height; by default that of layouts A and B) with the tighter
    band tolerances. Outputs `[whole]` and `[band]`."""
    band_top, band_bottom = band if band is not None else (BAND_TOP, BAND_BOTTOM)
    height = band_bottom - band_top
    black, freeze = f"{settings.max_black_s:g}", f"{settings.max_freeze_s:g}"
    return (
        "[0:v]split=2[a][b];"
        f"[a]blackdetect=d={black}:pix_th={BLACK_PIXEL_THRESHOLD:g},"
        f"freezedetect=n={settings.freeze_noise:g}:d={freeze}[whole];"
        f"[b]crop=iw:ih*{height:.4f}:0:ih*{band_top:.4f},"
        f"blackdetect=d={black}:pix_th={settings.band_black_pixel:g},"
        f"freezedetect=n={settings.band_freeze_noise:g}:d={freeze}[band]"
    )


def detect_black_and_freeze(
    path: Path,
    clip_duration_s: float,
    settings: GateSettings,
    ffmpeg: str = "ffmpeg",
    band: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """The longest black and the longest frozen stretch (seconds) in the first `clip_duration_s` of the
    file, on the whole frame or in the gameplay band; 0 when there is none of at least the configured
    length."""
    command = [ffmpeg, "-hide_banner", "-nostdin", "-t", f"{clip_duration_s:.3f}", "-i", str(path)]
    command += ["-an", "-filter_complex", detect_filter(settings, band)]
    command += ["-map", "[whole]", "-f", "null", "-", "-map", "[band]", "-f", "null", "-"]
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
    layout_kind: layout.LayoutKind | None = None,
    facecam_h: int | None = None,
) -> FileFacts | None:
    """Everything the gate needs to know about the file; None when it does not exist or is empty.
    `layout_kind` places the gameplay band (None: the band of layouts A and B), lower in layout C when
    the cover-ready video drew a taller facecam (`facecam_h`).

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
        black, frozen = detect_black_and_freeze(
            path,
            min(analysed, media.duration_s),
            settings,
            ffmpeg,
            layout.gameplay_band(layout_kind, facecam_h),
        )
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


def score_frame_times(clip_duration_s: float, count: int, peak_s: float | None = None) -> list[float]:
    """Where the frames for the visual score (P9V) are taken, in time order: always the four P5 positions,
    so one extraction serves both calls, plus up to `count - 4` more. The extra ones go where the event
    most likely is: around the chat peak when its place in the clip is known (chat reacts a few seconds
    after what it saw), then between and after the P5 positions. A candidate outside the clip or within
    `SCORE_MIN_GAP_S` of a frame already chosen is skipped."""
    chosen = frame_times(clip_duration_s)
    candidates = [peak_s + offset for offset in SCORE_PEAK_OFFSETS_S] if peak_s is not None else []
    candidates += [clip_duration_s * position for position in SCORE_EXTRA_POSITIONS]
    for time_s in candidates:
        if len(chosen) >= count:
            break
        inside = SCORE_EDGE_S <= time_s <= clip_duration_s - SCORE_EDGE_S
        if inside and all(abs(time_s - other) >= SCORE_MIN_GAP_S for other in chosen):
            chosen.append(round(time_s, 3))
    return sorted(chosen)


def sample_score_frames(
    path: Path, clip_duration_s: float, count: int, peak_s: float | None = None, ffmpeg: str = "ffmpeg"
) -> list[tuple[float, bytes]]:
    """`(time, JPEG)` for every time of `score_frame_times`, in time order."""
    times = score_frame_times(clip_duration_s, count, peak_s)
    return [(time_s, tools.grab_frame(path, time_s, ffmpeg)) for time_s in times]


def p5_frames(shots: Sequence[tuple[float, bytes]], clip_duration_s: float) -> list[bytes]:
    """The four P5 frames out of an extraction made for the visual score."""
    by_time = dict(shots)
    return [by_time[time_s] for time_s in frame_times(clip_duration_s)]


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
    """Ask P5 about the frames.

    On real renders with something wrong in the picture the model answered in the wrong shape about every
    second time, also after the client's repair retry (see the M07 progress notes). The question is
    therefore asked up to `P5_ATTEMPTS` times before `InvalidModelOutput` is raised; a verdict is never
    assumed.
    """
    for attempt in range(1, P5_ATTEMPTS + 1):
        try:
            verdict = calls.visual_check(frames, lane=lane, ref_id=clip_id, client=client)
        except InvalidModelOutput:
            if attempt == P5_ATTEMPTS:
                raise
            get_logger("gate").warning("gate_p5_invalid_reply", attempt=attempt, clip_id=clip_id)
        else:
            return findings_from_verdict(verdict)
    raise AssertionError("unreachable")


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

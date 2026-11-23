"""The clip factory pipeline (docs/09 section 1): one source window in, one finished clip out.

`render_clip(RenderInput) -> clip_id` runs: transcribe the padded window, clip writer (P2), trim, music
detection, profanity, layout (cache or P4), subtitles and overlays, ffmpeg render with loudness
normalisation, then the clip row goes from `rendering` to `gating`. The job handlers `clip.render` and
`express.render` call it with the job payload.

The steps that need no database live in `produce` and `render_file`, so `sixstars clip render-file` can
run them on a local video for design work.

Times: `RenderInput` positions are seconds in the source file ("file time"); the transcript and the clip
writer work in seconds from the start of the padded window ("window time"); subtitles, bleeps and the
stored transcript are in seconds from the first frame of the clip ("clip time").
"""

import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from psycopg.types.json import Jsonb

from sixstars import db, events, jobs, notify
from sixstars import storage as clip_storage
from sixstars.ai.calls import BudgetExceededError, ChatLine
from sixstars.ai.schemas import ClipWriting, SpikeVerdict
from sixstars.clients.anthropic_client import AnthropicClient, BudgetKind
from sixstars.clients.transcribe import Mode, Transcriber
from sixstars.config import env
from sixstars.factory import (
    anchor,
    cards,
    effects,
    facecam,
    montage,
    music,
    precheck,
    thumbs,
    tools,
    variants,
    writer,
)
from sixstars.factory import transcript as transcripts
from sixstars.factory.assets import FactoryAssets, load_assets
from sixstars.factory.audio import SfxTrack, parse_loudnorm_report
from sixstars.factory.bleep import Span, bleep_spans
from sixstars.factory.config import FactoryConfig
from sixstars.factory.director import StylePlan, clip_facts
from sixstars.factory.layout import (
    B_GAMEPLAY_Y,
    C_QUESTION_GAP,
    C_TITLE_OVERLAP,
    H_HEADER_TOP,
    HOOK_BOTTOM,
    HOOK_TOP,
    M_FADE_H,
    M_SEAM_Y,
    P_TEXT_CENTER_Y,
    REF_HEIGHT,
    REF_WIDTH,
    Geometry,
    Layout,
    LayoutA,
    LayoutB,
    LayoutCard,
    LayoutMontage,
    LayoutPov,
    PunchIns,
    ZoomCut,
)
from sixstars.factory.lines import Line, clip_lines, group_lines
from sixstars.factory.params import FactoryParams
from sixstars.factory.presets import RenderMode, uses_nvenc
from sixstars.factory.render import (
    RenderSpec,
    SourceInfo,
    build_envelope_command,
    build_loudnorm_measure_command,
    build_render_command,
)
from sixstars.factory.style import StyleRules
from sixstars.factory.subtitles import SubtitleStyle, build_ass, hook_box_bottom, title_bar_top
from sixstars.factory.textfit import TextMeasurer, estimate_text_width, pillow_measurer
from sixstars.factory.titlebar import TitleBar, draw_title_bar
from sixstars.factory.window import ClipWindow, padded_window
from sixstars.logging import get_logger
from sixstars.scoring import log as scoring_log

JOB_CLIP_RENDER = "clip.render"
# The gate's job (`gate.gate.JOB_GATE`; the gate imports this module, so the name is repeated here).
JOB_GATE = "clip.gate"
JOB_EXPRESS_RENDER = "express.render"
ASS_NAME = "clip.ass"
TITLE_PNG = "title.png"
SCRATCH_OUTPUT = "render.mp4"
# The finished file may differ from the planned length by this much (container rounding).
DURATION_TOLERANCE_S = 0.2
# docs/27 title-bar hook: the watermark sits this far below the hook box (reference canvas pixels).
# A question title in layout A starts here (below the LIVE badge, at the top of the gameplay).
QUESTION_TOP_A = 310
WATERMARK_HOOK_GAP = 14
# A job deferred because the daily AI budget is used up runs again this long after 00:00 UTC.
BUDGET_RETRY_MARGIN_S = 120
LANES = ("spike", "hourly")
MODES = ("normal", "express")
SOURCE_TYPES = ("live", "vod")
PLATFORMS = ("twitch", "youtube")


BeforeWriter = Callable[[music.MusicAnalysis | None], str | None]


class ClipSkippedError(RuntimeError):
    """The clip will not be rendered (no speech, or the clip writer rejected it). Not a failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RenderInput:
    """One clip to make. Positions are seconds in `source_path` (the section file or the live buffer cut).

    `spike_floor_s` is the latest allowed clip start. `pad_start_s` / `pad_end_s` bound what is transcribed
    and offered to the clip writer; when missing, the window is padded by `spikes.context_pad_s`.
    `source_offset_s` is where the file starts in the original video or stream, so `clips.start_s` /
    `clips.end_s` are positions in the original. `chat` times are seconds in the file. `clip_id` (a uuid
    chosen by the caller) or `spike_id` makes the render idempotent: a second call finds the same row.

    `moment_chosen` is for a clip whose moment people who watched picked themselves (a viewer-made Twitch
    clip, decision D110): the clip writer saying "there is no moment here" from the transcript alone then
    does not stop the render, and the gate judges the rendered clip. A reject for a safety flag or a
    spoiler always stands (`reject_stands`).

    `whole_window` is for the same kind of clip: the proposed window is the clip as its maker cut it, so
    the clip writer's in and out points are not applied (`keep_window`). The cut is still moved off the
    words and held to the length limits by the usual trim and snap rules.
    """

    source_path: Path
    window_start_s: float
    window_end_s: float
    creator: str
    spike_floor_s: float | None = None
    creator_id: str | None = None
    platform: str = "youtube"
    source_id: str = ""
    source_url: str = ""
    source_type: str = "vod"
    lane: str = "hourly"
    mode: str = "normal"
    stream_title: str = ""
    spike_id: str | None = None
    clip_id: str | None = None
    pad_start_s: float | None = None
    pad_end_s: float | None = None
    source_offset_s: float = 0.0
    verdict: SpikeVerdict | None = None
    chat: tuple[ChatLine, ...] = ()
    upload: bool = False
    moment_chosen: bool = False
    whole_window: bool = False
    # development-plan/M18 Phase C: "story" for the longer cut of a short clip (`parent_clip_id`).
    version: str = "short"
    parent_clip_id: str | None = None

    def __post_init__(self) -> None:
        if self.lane not in LANES:
            raise ValueError(f"lane must be one of {LANES}, got {self.lane!r}")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {SOURCE_TYPES}, got {self.source_type!r}")
        if self.platform not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}, got {self.platform!r}")
        if self.window_start_s < 0 or self.window_end_s <= self.window_start_s:
            raise ValueError(f"invalid window {self.window_start_s}..{self.window_end_s}")

    def to_payload(self) -> dict[str, Any]:
        """The job payload for `clip.render` / `express.render` (JSON values only)."""
        return {
            "source_path": str(self.source_path),
            "window_start_s": self.window_start_s,
            "window_end_s": self.window_end_s,
            "spike_floor_s": self.spike_floor_s,
            "creator": self.creator,
            "creator_id": self.creator_id,
            "platform": self.platform,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "lane": self.lane,
            "mode": self.mode,
            "stream_title": self.stream_title,
            "spike_id": self.spike_id,
            "clip_id": self.clip_id,
            "pad_start_s": self.pad_start_s,
            "pad_end_s": self.pad_end_s,
            "source_offset_s": self.source_offset_s,
            "verdict": self.verdict.model_dump() if self.verdict is not None else None,
            "chat": [{"t_s": line.t_s, "text": line.text, "user": line.user} for line in self.chat],
            "upload": self.upload,
            "moment_chosen": self.moment_chosen,
            "whole_window": self.whole_window,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], default_mode: str = "normal") -> "RenderInput":
        """Build the input from a job payload. Unknown keys are ignored; `mode` defaults per job type."""

        def number(key: str) -> float | None:
            value = payload.get(key)
            return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

        def text(key: str, default: str = "") -> str:
            value = payload.get(key)
            return str(value) if value is not None else default

        def optional(key: str) -> str | None:
            value = payload.get(key)
            return str(value) if value else None

        start, end = number("window_start_s"), number("window_end_s")
        if not payload.get("source_path") or start is None or end is None:
            raise ValueError("render payload needs source_path, window_start_s and window_end_s")
        verdict = payload.get("verdict")
        chat = payload.get("chat") or []
        return cls(
            source_path=Path(str(payload["source_path"])),
            window_start_s=start,
            window_end_s=end,
            spike_floor_s=number("spike_floor_s"),
            creator=text("creator"),
            creator_id=optional("creator_id"),
            platform=text("platform", "youtube"),
            source_id=text("source_id"),
            source_url=text("source_url"),
            source_type=text("source_type", "vod"),
            lane=text("lane", "spike" if default_mode == "express" else "hourly"),
            mode=text("mode", default_mode),
            stream_title=text("stream_title"),
            spike_id=optional("spike_id"),
            clip_id=optional("clip_id"),
            pad_start_s=number("pad_start_s"),
            pad_end_s=number("pad_end_s"),
            source_offset_s=number("source_offset_s") or 0.0,
            verdict=SpikeVerdict.model_validate(verdict) if isinstance(verdict, Mapping) else None,
            chat=tuple(
                ChatLine(t_s=float(line["t_s"]), text=str(line["text"]), user=line.get("user"))
                for line in chat
                if isinstance(line, Mapping) and "t_s" in line and "text" in line
            ),
            upload=bool(payload.get("upload", False)),
            moment_chosen=payload.get("moment_chosen") is True,
            whole_window=payload.get("whole_window") is True,
        )


@dataclass(slots=True)
class Deps:
    """Replaceable collaborators (tests pass fakes). None means the real one, built on first use."""

    transcriber: Transcriber | None = None
    ai_client: AnthropicClient | None = None
    music_detector: music.MusicDetector | None = None
    storage: clip_storage.ClipStorage | None = None
    assets: FactoryAssets | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    skip_music: bool = False


@dataclass(frozen=True, slots=True)
class RenderJob:
    """Everything `render_file` needs: no database, no AI."""

    source_path: Path
    media: tools.MediaInfo
    window: ClipWindow
    lines: Sequence[Line]
    layout: Layout
    params: FactoryParams
    assets: FactoryAssets
    output_path: Path
    scratch_dir: Path
    series_no: int
    mode: RenderMode = "normal"
    lane: str = "hourly"
    creator: str = ""
    hook: str | None = None
    keywords: Mapping[int, Sequence[str]] = field(default_factory=dict)
    duck: Sequence[Span] = ()
    style: StyleRules = field(default_factory=StyleRules)
    # docs/27 payoff, in clip time (the writer's `payoff_s` shifted to the final cut).
    payoff_s: float | None = None
    payoff_emoji: str | None = None
    # The catalogue id of `style` (factory.styles), for the logs and the clip_renders row.
    style_id: str = ""
    # The clip writer's texts for the design styles (`ClipWriting.style_texts`): title_line, headline,
    # question, pov_line.
    texts: Mapping[str, str] = field(default_factory=dict)
    # Layout M (Phase E): the footage around the clip (file time) the B-roll may come from.
    broll_from: ClipWindow | None = None


def framed_layout(layout: Layout, style: StyleRules) -> Layout:
    """The layout a style draws: the news card (H) or the POV crop (P) when its `frame` says so; else with
    a facecam on top (layout C) when the style says so, or below (layout A); without a facecam, layout B.
    Pure."""
    facecam = (
        layout.facecam_box if isinstance(layout, LayoutA | LayoutCard | LayoutPov | LayoutMontage) else None
    )
    if style.frame == "montage":
        return LayoutMontage(facecam_box=facecam) if facecam is not None else LayoutB()
    if isinstance(layout, LayoutMontage):
        layout = LayoutA(facecam_box=layout.facecam_box)
    if style.frame == "headline_card":
        return LayoutCard(facecam_box=facecam)
    if style.frame == "pov":
        return LayoutPov(facecam_box=facecam)
    if isinstance(layout, LayoutCard | LayoutPov):
        layout = LayoutA(facecam_box=facecam) if facecam is not None else LayoutB()
    if isinstance(layout, LayoutA) and layout.cam_top != style.facecam_top:
        return replace(layout, cam_top=style.facecam_top)
    return layout


def has_emoji(text: str) -> bool:
    """Does `text` contain a pictographic character (an emoji)? Pure."""
    return any(ord(char) >= 0x2190 and not char.isalnum() for char in text)


def chosen_title(
    hook: str | None, texts: Mapping[str, str], emoji: str | None, style: StyleRules
) -> str | None:
    """The text a style shows: the writer's text named by `title_source`, else the hook. The payoff emoji
    is added to a whole-clip hook or title line that has none. Pure."""
    source = style.title_source
    picked = texts.get(source) if source != "hook" else None
    if picked:
        if source == "title_line":
            return title_text(picked, emoji, whole_clip=style.title_whole_clip)
        return picked
    return title_text(hook, emoji, whole_clip=style.title_whole_clip)


def title_text(hook: str | None, emoji: str | None, *, whole_clip: bool) -> str | None:
    """The text of the title bar: the hook, plus the payoff emoji for a title that stays for the whole
    clip (like the references' "NPC has a pet alligator 😭") when the hook has none. Pure."""
    if not hook:
        return None
    if whole_clip and emoji and not has_emoji(hook):
        return f"{hook} {emoji.strip()}"
    return hook


@dataclass(frozen=True, slots=True)
class RenderOutcome:
    """What one ffmpeg render produced. `render_ms` covers the loudness pass and the encode."""

    output_path: Path
    media: tools.MediaInfo
    clip_duration_s: float
    render_ms: int
    measure_ms: int
    file_size_bytes: int
    bleeps: tuple[Span, ...]
    profanity_count: int
    sfx_times_s: tuple[float, ...]
    nvenc: bool


@dataclass(frozen=True, slots=True)
class Produced:
    """The result of `produce`: the decisions taken on the way and the render outcome."""

    transcript: transcripts.WindowTranscript
    writing: ClipWriting
    window: ClipWindow
    lines: tuple[Line, ...]
    layout: Layout
    music: music.MusicAnalysis | None
    outcome: RenderOutcome
    timings_ms: dict[str, int]
    # The render as made: `factory.variants` renders the other styles of the plan from it.
    job: RenderJob | None = None
    # Where the padded window starts in the file, and the writer's input: a story cut reuses both.
    offset_s: float = 0.0
    writer_input: writer.WriterInput | None = None


WritingChooser = Callable[[writer.WriterInput], ClipWriting]
LayoutChooser = Callable[[tools.MediaInfo, float], Layout]
WritingHook = Callable[[ClipWriting, transcripts.WindowTranscript], None]
# Picks the design style once the writer's reply, the facecam and the footage around the clip are known
# (`has_broll`: enough of it for a montage): (rules, catalogue id).
StyleChooser = Callable[[ClipWriting, Layout, bool], tuple[StyleRules, str]]


def _caption_fonts(cfg: FactoryConfig, style: StyleRules) -> str:
    """The caption font to load: the render's own style first, else any style of the catalogue that names
    one (so the variants of the plan find it too)."""
    if style.caption_font:
        return style.caption_font
    return next((entry.rules.caption_font for entry in cfg.catalog.entries if entry.rules.caption_font), "")


def _measurer(assets: FactoryAssets) -> TextMeasurer:
    return pillow_measurer(assets.font_file) if assets.font_file is not None else estimate_text_width


def _caption_measurer(assets: FactoryAssets) -> TextMeasurer:
    font = assets.caption_font_file
    return pillow_measurer(font) if font is not None else _measurer(assets)


def _punch_spans(spec: RenderSpec, job: RenderJob, payoff_s: float | None, ffmpeg: str) -> tuple[Span, ...]:
    """docs/27 punch-ins at the loudest moments of the clip's source audio. An envelope that cannot be
    measured means no punch-ins, never a failed render."""
    if not job.style.punchins or not job.media.has_audio:
        return ()
    try:
        result = tools.run(
            build_envelope_command(spec, ffmpeg=ffmpeg), timeout_s=tools.RENDER_TIMEOUT_S, what="envelope"
        )
    except tools.FfmpegError as exc:
        get_logger("factory").warning("punchin_envelope_failed", error=str(exc)[:200])
        return ()
    envelope = effects.parse_momentary(result.stderr.decode("utf-8", errors="replace"))
    avoid = () if payoff_s is None else (payoff_s,)
    return effects.select_punchins(envelope, spec.clip_duration_s, job.style, avoid_s=avoid)


def _title_bar(job: RenderJob, live_badge: bool) -> TitleBar | None:
    """The title PNG of the style, in the scratch directory: the news card (layout H), the POV line
    (layout P), the red question title (`title_look` question) or the docs/27 title bar. None when the
    font is missing or drawing fails: the hook box of the subtitle file is used instead, never a failed
    render."""
    font = job.assets.title_font_file
    if not job.hook or font is None:
        return None
    path = job.scratch_dir / TITLE_PNG
    emoji, colour = job.assets.emoji_font_file, job.assets.emoji_colour
    try:
        drawn: cards.Drawn | None = None
        kind = job.layout.kind
        if kind == "H":
            drawn = cards.draw_headline_card(
                job.params.watermark or "",
                job.hook,
                path,
                font_file=font,
                avatar=job.assets.avatar,
                emoji_font=emoji,
                emoji_colour=colour,
            )
        elif kind == "P":
            drawn = cards.draw_outline_text(
                job.hook,
                path,
                font_file=font,
                weight=cards.POV_WEIGHT,
                sizes=cards.POV_SIZES,
                max_width=cards.POV_MAX_WIDTH,
                fill=cards.WHITE,
                stroke=cards.POV_STROKE,
                emoji_font=emoji,
                emoji_colour=colour,
            )
        elif job.style.title_look == "question":
            drawn = cards.draw_outline_text(
                job.hook,
                path,
                font_file=job.assets.question_font_file or font,
                weight=None,
                sizes=cards.QUESTION_SIZES,
                max_width=cards.QUESTION_MAX_WIDTH,
                fill=cards.QUESTION_RED,
                stroke=cards.QUESTION_STROKE,
                emoji_font=emoji,
                emoji_colour=colour,
                caps=True,
            )
        else:
            return draw_title_bar(
                job.hook,
                path,
                font_file=font,
                emoji_font=emoji,
                emoji_colour=colour,
            )
        return None if drawn is None else TitleBar(drawn.path, drawn.width, drawn.height)
    except OSError as exc:
        get_logger("factory").warning("title_bar_failed", error=str(exc)[:200])
        return None


def _montage_broll(
    job: RenderJob, duration_s: float, payoff_s: float | None, ffmpeg: str
) -> tuple[tuple[float, float, float], ...]:
    """Layout M: the B-roll cuts as (source start, clip time, length), from the busiest footage around the
    clip (`montage`). Anything that goes wrong means no cuts, never a failed render."""
    try:
        return _plan_broll(job, duration_s, payoff_s, ffmpeg)
    except (tools.FfmpegError, ValueError) as exc:
        get_logger("factory").warning("montage_broll_failed", error=str(exc)[:200])
        return ()


def _plan_broll(
    job: RenderJob, duration_s: float, payoff_s: float | None, ffmpeg: str
) -> tuple[tuple[float, float, float], ...]:
    style = job.style
    if not isinstance(job.layout, LayoutMontage) or style.montage_every_s <= 0 or job.broll_from is None:
        return ()
    slots = montage.montage_timeline(duration_s, style.montage_every_s, payoff_s)
    if not slots:
        return ()
    padded = job.broll_from
    result = tools.run(
        montage.motion_command(job.source_path, padded, ffmpeg=ffmpeg),
        timeout_s=tools.RENDER_TIMEOUT_S,
        what="montage motion",
    )
    motion = montage.parse_motion(result.stderr.decode("utf-8", errors="replace"), padded.start_s)
    clip = ClipWindow(job.window.start_s, job.window.start_s + duration_s)
    picks = montage.pick_broll(motion, clip, padded, length_s=style.montage_every_s, count=len(slots))
    return tuple(
        (round(pick.start_s, 3), round(slot.start_s, 3), round(min(slot.duration_s, pick.duration_s), 3))
        for slot, pick in zip(slots, picks, strict=False)
    )


def _seam_fade(job: RenderJob) -> dict[str, Any]:
    """Layout M: the soft dark band over the seam between gameplay and face (a PNG in the scratch
    directory). Drawing it cannot fail the render."""
    if not isinstance(job.layout, LayoutMontage):
        return {}
    try:
        fade = montage.draw_seam_fade(job.scratch_dir / "fade.png", width=REF_WIDTH, height=M_FADE_H)
    except OSError as exc:
        get_logger("factory").warning("seam_fade_failed", error=str(exc)[:200])
        return {}
    return {"fade_png": fade.path, "fade_top": M_SEAM_Y - M_FADE_H // 2}


def _own_title_top(kind: str, height: int, seam_y: int) -> int:
    """Top of a Phase B text on the reference canvas: the news card's header, the POV line centred on the
    gameplay, or the question title at the top of the gameplay. Pure."""
    if kind == "H":
        return H_HEADER_TOP
    if kind == "P":
        return max(0, P_TEXT_CENTER_Y - height // 2)
    if kind == "C":
        return seam_y + C_QUESTION_GAP
    if kind == "B":
        return B_GAMEPLAY_Y + C_QUESTION_GAP
    return QUESTION_TOP_A


def _sfx_tracks(cues: Sequence[effects.SfxCue], assets: FactoryAssets) -> tuple[SfxTrack, ...]:
    """One track per effect file that has cues (the keyword pop goes through `RenderSpec.sfx_path`)."""
    tracks: list[SfxTrack] = []
    for kind, path in sorted(assets.sfx.items()):
        times = tuple(cue.time_s for cue in cues if cue.kind == kind)
        if times:
            tracks.append(SfxTrack(path, times))
    return tuple(tracks)


def _move_into_place(finished: Path, destination: Path) -> None:
    """Move a finished file to its final path so that the path never holds a partial file.

    A rename is atomic on one file system. Across file systems the file is first copied next to the
    destination under a temporary name, then renamed.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(finished, destination)
    except OSError:
        partial = destination.with_name(destination.name + ".part")
        shutil.copyfile(finished, partial)
        os.replace(partial, destination)
        finished.unlink(missing_ok=True)


def render_file(job: RenderJob, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> RenderOutcome:
    """Write the subtitle file and render the clip with ffmpeg.

    The loudness is measured first and fed to the real pass (two-pass loudnorm) in both modes: the
    audio-only pass takes two to three seconds, and single-pass results landed up to 1 LU off target.
    The clip is rendered into the scratch directory and moved to `output_path` only when ffprobe confirms
    it, so a failed render never leaves a half-written file there.
    """
    log = get_logger("factory")
    params = job.params
    style = job.style
    job = replace(
        job,
        layout=framed_layout(job.layout, style),
        hook=chosen_title(job.hook, job.texts, job.payoff_emoji, style) if style.show_title else None,
    )
    cam_top = job.layout.kind == "C"
    words = [word for line in job.lines for word in line.words]
    matcher = job.assets.profanity()
    profane = matcher.find([word.text for word in words])
    spec = RenderSpec(
        source=SourceInfo(job.source_path, job.media.width, job.media.height, job.media.has_audio),
        window=job.window,
        layout=job.layout,
        ass_path=job.scratch_dir / ASS_NAME,
        output_path=job.scratch_dir / SCRATCH_OUTPUT,
        series_no=job.series_no,
        params=params,
        mode=job.mode,
        fonts=job.assets.fonts,
        duck_spans=tuple(job.duck),
        sfx_path=job.assets.sfx_pop,
        logo_path=job.assets.logo,
        hook_shown=bool(job.hook),
    )
    duration = spec.clip_duration_s
    live_badge = job.lane == "spike"
    bleeps = tuple(bleep_spans(words, profane, duration)) if params.bleep else ()
    pop_times: tuple[float, ...] = ()
    if job.assets.sfx_pop is not None and style.captions:
        pop_times = writer.outside_spans(writer.keyword_times(job.lines, job.keywords), bleeps)
    payoff_s = job.payoff_s
    if not style.payoff_fx or payoff_s is None or not 0 <= payoff_s < duration:
        payoff_s = None
    punch_spans = _punch_spans(spec, job, payoff_s, ffmpeg)
    broll = _montage_broll(job, duration, payoff_s if payoff_s is not None else job.payoff_s, ffmpeg)
    # The montage's cuts get the punch-in sounds (whoosh, impact); they are not punch-ins on screen.
    sfx_spans = punch_spans or tuple(Span(at, at + length) for _start, at, length in broll)
    cues = effects.plan_sfx(
        duration_s=duration, keyword_times=pop_times, punchins=sfx_spans, payoff_s=payoff_s, rules=style
    )
    sfx_times = tuple(cue.time_s for cue in cues if cue.kind == "pop")
    log.info(
        "clip_edit_effects",
        style_id=job.style_id,
        layout=job.layout.kind,
        punchins=[[span.start_s, span.end_s] for span in punch_spans],
        broll=[list(cut) for cut in broll],
        payoff_s=payoff_s,
        sfx={
            kind: [cue.time_s for cue in cues if cue.kind == kind] for kind in sorted({c.kind for c in cues})
        },
        sfx_files=sorted(job.assets.sfx),
    )
    nvenc = params.use_nvenc and job.mode == "express" and tools.nvenc_available(ffmpeg)
    title_bar = bool(job.hook) and style.hook_mode == "title_bar"
    watermark_y = None
    bar = _title_bar(job, live_badge) if title_bar else None
    title_top = title_bar_top(params, live_badge=live_badge)
    # Layout C: the title sits on the seam (its bottom just below it), over the streamer's chest, never
    # on the face; the watermark keeps its corner and shows from the start.
    seam_ref = Geometry.for_canvas(REF_WIDTH, REF_HEIGHT).c_seam_y
    hook_top: float | None = None
    kind = job.layout.kind
    own_place = kind in ("H", "P") or style.title_look == "question"
    if own_place:
        # Phase B styles: the text has its own place and the watermark keeps its corner.
        top = _own_title_top(kind, bar.height if bar is not None else HOOK_BOTTOM - HOOK_TOP, seam_ref)
        hook_top = float(top)
        title_top = top
    elif cam_top:
        hook_top = float(seam_ref + C_TITLE_OVERLAP - (HOOK_BOTTOM - HOOK_TOP))
        if bar is not None:
            title_top = max(0, seam_ref + C_TITLE_OVERLAP - bar.height)
    elif bar is not None:
        # The TikTok-style bar (an image) replaces the hook box of the subtitle file.
        watermark_y = round((title_top + bar.height + WATERMARK_HOOK_GAP) * params.height / REF_HEIGHT)
    elif title_bar:
        # No title font: the hook box stays for the whole clip, the watermark just below it.
        box_bottom = hook_box_bottom(params, live_badge=live_badge) + WATERMARK_HOOK_GAP
        watermark_y = round(box_bottom * params.height / REF_HEIGHT)
    spec = replace(
        spec,
        bleeps=bleeps,
        sfx_times_s=sfx_times,
        nvenc_available=nvenc,
        sfx_tracks=_sfx_tracks(cues, job.assets),
        sfx_gain_db=style.sfx_gain_db,
        compress=style.compress,
        zoom_cut=ZoomCut(style.zoomcut_every_s, style.zoomcut_scale) if style.zoomcuts else None,
        punch_ins=(
            PunchIns(tuple((s.start_s, s.end_s) for s in punch_spans), style.punchin_zoom)
            if punch_spans
            else None
        ),
        hook_shown=bool(job.hook) and not title_bar and not cam_top and not own_place,
        watermark=style.watermark,
        watermark_y=watermark_y,
        title_png=bar.path if bar is not None else None,
        title_top=title_top,
        title_whole_clip=style.title_whole_clip,
        follow_text=style.follow_overlay_text,
        follow_s=min(style.follow_overlay_s, duration),
        broll=broll,
        **_seam_fade(job),
    )
    ass = build_ass(
        job.lines,
        layout=job.layout.kind,
        params=params,
        duration_s=duration,
        keywords=job.keywords,
        profanity=matcher,
        hook=None if bar is not None else job.hook,
        creator=job.creator or None,
        live_badge=live_badge,
        style=SubtitleStyle.from_rules(style),
        measure=_measurer(job.assets),
        caption_measure=_caption_measurer(job.assets),
        hook_mode=style.hook_mode,
        payoff_s=payoff_s,
        payoff_emoji=job.payoff_emoji,
        captions=style.captions,
        hook_top=hook_top,
        hook_whole_clip=style.title_whole_clip,
    )
    job.scratch_dir.mkdir(parents=True, exist_ok=True)
    Path(spec.ass_path).write_text(ass, encoding="utf-8", newline="\n")

    started = time.monotonic()
    measure_ms = 0
    if job.media.has_audio:
        measured = tools.run(
            build_loudnorm_measure_command(spec, ffmpeg=ffmpeg),
            timeout_s=tools.RENDER_TIMEOUT_S,
            what="loudness measure",
        )
        measurement = parse_loudnorm_report(measured.stderr.decode("utf-8", errors="replace"))
        measure_ms = int((time.monotonic() - started) * 1000)
        if measurement is None:
            log.warning("loudnorm_measure_unreadable")
        spec = replace(spec, loudnorm=measurement)
    tools.run(build_render_command(spec, ffmpeg=ffmpeg), timeout_s=tools.RENDER_TIMEOUT_S, what="render")
    render_ms = int((time.monotonic() - started) * 1000)

    scratch_output = Path(spec.output_path)
    media = tools.probe(scratch_output, ffprobe)
    expected = spec.output_duration_s
    if not media.has_video or not media.has_audio:
        raise tools.FfmpegError("rendered file lacks a video or an audio stream")
    if (media.width, media.height) != (params.width, params.height):
        raise tools.FfmpegError(f"rendered file is {media.width}x{media.height}")
    if abs(media.duration_s - expected) > DURATION_TOLERANCE_S:
        raise tools.FfmpegError(f"rendered file is {media.duration_s:.2f} s long, expected {expected:.2f} s")
    _move_into_place(scratch_output, job.output_path)
    size = job.output_path.stat().st_size
    log.info(
        "clip_render_done",
        style_id=job.style_id,
        mode=job.mode,
        layout=job.layout.kind,
        render_ms=render_ms,
        measure_ms=measure_ms,
        duration_s=round(media.duration_s, 2),
        file_size_bytes=size,
        bleeps=len(bleeps),
        nvenc=uses_nvenc(params, job.mode, nvenc_available=nvenc),
    )
    return RenderOutcome(
        output_path=job.output_path,
        media=media,
        clip_duration_s=duration,
        render_ms=render_ms,
        measure_ms=measure_ms,
        file_size_bytes=size,
        bleeps=bleeps,
        profanity_count=len(profane),
        sfx_times_s=sfx_times,
        nvenc=uses_nvenc(params, job.mode, nvenc_available=nvenc),
    )


def reject_stands(writing: ClipWriting, *, moment_chosen: bool) -> bool:
    """Does the clip writer's reject stop the render? Always, except for a clip whose moment viewers
    chose (`RenderInput.moment_chosen`) when nothing but the writer's own "no moment here" is against it:
    no safety flag and no story spoiler. Such a clip is rendered and the gate judges it (its score, and
    again its safety flags, rejected topics and spoiler rules). Pure."""
    if not writing.reject:
        return False
    if not moment_chosen:
        return True
    return bool(writing.safety.flagged()) or writing.spoiler


def keep_window(writing: ClipWriting, proposed: ClipWindow) -> ClipWriting:
    """The writing with the proposed window in place of the writer's own in and out points
    (`RenderInput.whole_window`). Everything else the writer wrote is kept. Pure."""
    return writing.model_copy(update={"start_s": proposed.start_s, "end_s": proposed.end_s})


def wants_story(item: RenderInput, cfg: FactoryConfig) -> bool:
    """Is a story cut asked for this render (development-plan/M18 Phase C)? Not for a story itself, not for
    a clip whose window was cut by someone else (`whole_window`), and only where `factory.story` applies."""
    if item.version != "short" or item.whole_window:
        return False
    return cfg.story.applies(lane=item.lane, mode=item.mode, source_type=item.source_type)


def _story_limits(item: RenderInput, cfg: FactoryConfig) -> dict[str, float]:
    if not wants_story(item, cfg):
        return {}
    return {"story_min_s": cfg.story.min_s, "story_max_s": cfg.story.max_s}


def transcription_modes(mode: str) -> tuple[Mode, ...]:
    """Local first for a normal render, hosted first for an express one; the other is the fallback."""
    return ("hosted", "local") if mode == "express" else ("local", "hosted")


def _windows(item: RenderInput, cfg: FactoryConfig, file_s: float) -> tuple[ClipWindow, ClipWindow]:
    """The proposed window and the padded window, both inside the file (file time)."""
    start = min(item.window_start_s, max(0.0, file_s - 0.1))
    proposed = ClipWindow(start, max(min(item.window_end_s, file_s), start + 0.1))
    if item.pad_start_s is not None and item.pad_end_s is not None:
        padded = ClipWindow(
            max(0.0, min(item.pad_start_s, proposed.start_s)),
            min(file_s, max(item.pad_end_s, proposed.end_s)),
        )
    else:
        padded = padded_window(proposed, cfg.context_pad_s, file_s)
    return proposed, padded


def produce(
    item: RenderInput,
    cfg: FactoryConfig,
    *,
    deps: Deps,
    scratch_dir: Path,
    output_path: Path,
    series_no: int,
    choose_writing: WritingChooser,
    choose_layout: LayoutChooser,
    on_writing: WritingHook | None = None,
    clip_id: str | None = None,
    style: StyleRules | None = None,
    style_id: str = "",
    choose_style: StyleChooser | None = None,
    before_writer: BeforeWriter | None = None,
) -> Produced:
    """Transcribe, write, trim, analyse and render one clip. Touches no database table itself.

    `choose_writing` returns the clip writer's reply (P2, or values typed by hand); `choose_layout` gets
    the source info and the time of the middle of the final window. `on_writing` is called with the reply
    before the render starts. Raises `ClipSkippedError` when there is no speech or the writer rejects.
    `style` is the design style of this render (`style_id` its catalogue id); None means `cfg.style`.
    `choose_style`, when given, picks the style after the writer and the layout detection instead.
    `before_writer`, when given, gets the music flags of the padded window (None when they could not be
    measured) before the clip writer is asked, and returns a reason to skip the clip, or None.
    """
    style = style or cfg.style
    timings: dict[str, int] = {}

    def timed(name: str, started: float) -> None:
        timings[name] = int((time.monotonic() - started) * 1000)

    assets = deps.assets or load_assets(cfg.params.font, caption_font=_caption_fonts(cfg, style))
    media = tools.probe(item.source_path, deps.ffprobe)
    if not media.has_video or media.width < 2 or media.height < 2 or media.duration_s <= 0:
        raise tools.FfmpegError(f"{item.source_path.name} has no usable video stream")
    proposed, padded = _windows(item, cfg, media.duration_s)
    offset = padded.start_s
    ai_lane: BudgetKind = "express" if item.mode == "express" else "vod"

    started = time.monotonic()
    if media.has_audio:
        transcript = transcripts.transcribe_window(
            item.source_path,
            padded,
            modes=transcription_modes(item.mode),
            scratch_dir=scratch_dir,
            transcriber=deps.transcriber or transcripts.default_transcriber(cfg.whisper_model),
            ai_lane=ai_lane,
            ai_client=deps.ai_client,
            ref_id=clip_id,
            ffmpeg=deps.ffmpeg,
            pause_s=cfg.snap.pause_s if cfg.snap.vad else 0.0,
            punctuate_below=cfg.snap.punctuate_below if cfg.snap.enabled else 0.0,
        )
    else:
        transcript = transcripts.WindowTranscript(language="en", original_language=None, words=())
    timed("transcribe", started)
    if not transcript.words:
        raise ClipSkippedError("no_speech")
    if before_writer is not None:
        started = time.monotonic()
        window_music: music.MusicAnalysis | None = None
        if not deps.skip_music:
            try:
                window_music = music.analyse(
                    item.source_path,
                    padded,
                    detector=deps.music_detector,
                    has_audio=media.has_audio,
                    ffmpeg=deps.ffmpeg,
                )
            except music.MusicDetectorUnavailableError as exc:
                get_logger("factory").warning("music_detector_unavailable", error=str(exc)[:200])
        skip = before_writer(window_music)
        timed("precheck", started)
        if skip is not None:
            raise ClipSkippedError(skip)

    lines = group_lines(transcript.words)
    floor = item.spike_floor_s if item.spike_floor_s is not None else proposed.start_s
    window_chat = tuple(
        ChatLine(t_s=line.t_s - offset, text=line.text, user=line.user)
        for line in item.chat
        if padded.start_s <= line.t_s <= padded.end_s
    )
    cut_anchor: anchor.Anchor | None = None
    cut_guidance = ""
    if cfg.cut_rules is not None:
        started = time.monotonic()
        if cfg.cut_rules.anchor:
            readings = (
                anchor.measure_loudness(
                    item.source_path, padded.start_s, padded.duration_s, ffmpeg=deps.ffmpeg
                )
                if media.has_audio
                else []
            )
            cut_anchor = anchor.find_anchor(
                padded.duration_s,
                floor - offset,
                loudness=readings,
                chat_times=[line.t_s for line in window_chat],
                words=transcript.words,
            )
        cut_guidance = anchor.guidance(cfg.cut_rules, cut_anchor)
        timed("anchor", started)
    writer_input = writer.WriterInput(
        creator=item.creator,
        platform=item.platform,
        stream_title=item.stream_title,
        lines=lines,
        window_s=padded.duration_s,
        proposed=ClipWindow(proposed.start_s - offset, proposed.end_s - offset),
        spike_floor_s=floor - offset,
        min_len_s=cfg.min_len_s,
        max_len_s=cfg.max_len_s,
        chat=window_chat,
        verdict=item.verdict,
        profanity_hint=writer.profanity_hint(transcript.words, assets.profanity()),
        clip_id=clip_id,
        snap=cfg.snap,
        cut_guidance=cut_guidance,
        **_story_limits(item, cfg),
    )
    started = time.monotonic()
    writing = choose_writing(writer_input)
    timed("write", started)
    if on_writing is not None:
        on_writing(writing, transcript)
    if reject_stands(writing, moment_chosen=item.moment_chosen):
        raise ClipSkippedError(f"writer: {writing.reject_reason or 'rejected'}")
    if writing.reject:
        get_logger("factory").info("clip_writer_reject_overruled", reason=writing.reject_reason)

    if item.whole_window:
        writing = keep_window(writing, writer_input.proposed)
    window_w = writer.final_window(writing, writer_input)
    window = ClipWindow(window_w.start_s + offset, window_w.end_s + offset)
    clip_text = clip_lines(lines, window_w)
    if not clip_text:
        raise ClipSkippedError("no_speech")
    if cfg.cut_rules is not None:
        scoring_log.record(
            "factory.cut",
            decision="cut",
            clip_id=clip_id,
            spike_id=item.spike_id,
            creator_id=item.creator_id,
            sub_scores={
                "anchor_s": None if cut_anchor is None else cut_anchor.t_s,
                "anchor": None
                if cut_anchor is None
                else {
                    "loud_lufs": cut_anchor.loud_lufs,
                    "chat_per_s": cut_anchor.chat_per_s,
                    "words_per_s": cut_anchor.words_per_s,
                    "combined": cut_anchor.combined,
                },
                "anchor_inside_cut": None
                if cut_anchor is None
                else window_w.start_s <= cut_anchor.t_s <= window_w.end_s,
                "writer_start_s": writing.start_s,
                "writer_end_s": writing.end_s,
                "final_start_s": round(window_w.start_s, 2),
                "final_end_s": round(window_w.end_s, 2),
                "duration_s": round(window_w.duration_s, 2),
                "min_len_s": cfg.min_len_s,
                "max_len_s": cfg.max_len_s,
                "mode": item.mode,
            },
        )

    started = time.monotonic()
    analysis: music.MusicAnalysis | None = None
    if not deps.skip_music:
        try:
            analysis = music.analyse(
                item.source_path,
                window,
                detector=deps.music_detector,
                has_audio=media.has_audio,
                ffmpeg=deps.ffmpeg,
            )
        except music.MusicDetectorUnavailableError as exc:
            get_logger("factory").warning("music_detector_unavailable", error=str(exc)[:200])
    timed("music", started)

    started = time.monotonic()
    layout = choose_layout(media, (window.start_s + window.end_s) / 2)
    timed("layout", started)
    room_s = (window.start_s - padded.start_s) + (padded.end_s - window.end_s)
    if choose_style is not None:
        style, style_id = choose_style(writing, layout, room_s >= montage.MIN_ROOM_S)
        if deps.assets is None and style.caption_font != cfg.style.caption_font:
            assets = load_assets(cfg.params.font, caption_font=_caption_fonts(cfg, style))

    started = time.monotonic()
    job = RenderJob(
        source_path=item.source_path,
        media=media,
        window=window,
        lines=clip_text,
        layout=layout,
        params=cfg.params,
        assets=assets,
        output_path=output_path,
        scratch_dir=scratch_dir,
        series_no=series_no,
        mode="express" if item.mode == "express" else "normal",
        lane=item.lane,
        creator=item.creator,
        hook=writing.hook,
        keywords=writing.keyword_map(),
        duck=music.duck_spans(analysis, cfg.max_music_ratio, window.duration_s),
        style=style,
        payoff_s=None if writing.payoff_s is None else writing.payoff_s - window_w.start_s,
        payoff_emoji=writing.payoff_emoji,
        style_id=style_id,
        texts=writing.style_texts(),
        broll_from=padded,
    )
    outcome = render_file(job, ffmpeg=deps.ffmpeg, ffprobe=deps.ffprobe)
    timed("render", started)
    return Produced(
        transcript=transcript,
        writing=writing,
        window=window,
        lines=tuple(clip_text),
        layout=framed_layout(layout, style),
        music=analysis,
        outcome=outcome,
        timings_ms=timings,
        job=job,
        offset_s=offset,
        writer_input=writer_input,
    )


# ── Database orchestration ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClipRow:
    """The clip row a render works on."""

    id: str
    series_no: int
    status: str
    created: bool


def scratch_dir_for(clip_id: str) -> Path:
    """`$DATA_DIR/render/<clip_id>/`: deleted when the render ends, whatever the result."""
    return env().DATA_DIR / "render" / clip_id


def ready_path_for(clip_id: str) -> Path:
    """`$DATA_DIR/ready/<clip_id>.mp4`: the finished clip, kept until it is posted."""
    return env().DATA_DIR / "ready" / f"{clip_id}.mp4"


def find_clip(item: RenderInput) -> ClipRow | None:
    """The clip row of an earlier call for the same `clip_id` or `spike_id`, if there is one."""
    if item.clip_id is not None:
        row = db.fetch_one("select id, series_no, status from clips where id = %s", (item.clip_id,))
    elif item.spike_id is not None:
        row = db.fetch_one(
            "select id, series_no, status from clips where spike_id = %s order by created_at limit 1",
            (item.spike_id,),
        )
    else:
        row = None
    if row is None:
        return None
    return ClipRow(str(row["id"]), int(row["series_no"] or 0), str(row["status"]), created=False)


def claim_clip(item: RenderInput, *, force: bool = False) -> ClipRow:
    """Find or create the clip row for this input, in one transaction.

    A new row takes the next number of `clip_series_seq` and starts in status `rendering`. A row that
    exists keeps its id and its series number, so a retried job never takes a second number. `force`
    puts an existing row back to `rendering`.
    """
    if item.creator_id is None:
        raise ValueError("render_clip needs creator_id")
    with db.transaction():
        key = item.clip_id or item.spike_id
        if key is not None:
            db.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"clip.render:{key}",))
        existing = find_clip(item)
        if existing is not None:
            if force and existing.status != "rendering":
                db.execute(
                    "update clips set status = 'rendering', reject_reason = null where id = %s",
                    (existing.id,),
                )
                return replace(existing, status="rendering")
            return existing
        row = db.fetch_one(
            """
            insert into clips
              (id, series_no, spike_id, creator_id, source_platform, source_id, source_url, source_type,
               lane, start_s, end_s, gate_profile, status)
            values
              (coalesce(%s::uuid, gen_random_uuid()), nextval('clip_series_seq'), %s, %s, %s, %s, %s, %s,
               %s, %s, %s, %s, 'rendering')
            returning id, series_no
            """,
            (
                item.clip_id,
                item.spike_id,
                item.creator_id,
                item.platform,
                item.source_id,
                item.source_url,
                item.source_type,
                item.lane,
                item.source_offset_s + item.window_start_s,
                item.source_offset_s + item.window_end_s,
                "express" if item.lane == "spike" else "hourly",
            ),
        )
        assert row is not None
        clip_id, series_no = str(row["id"]), int(row["series_no"])
        if item.spike_id is not None:
            db.execute(
                "update spikes set status = 'rendering', clip_id = %s where id = %s",
                (clip_id, item.spike_id),
            )
        events.emit(
            "clip.render_started",
            entity_type="clip",
            entity_id=clip_id,
            data={
                "series_no": series_no,
                "spike_id": item.spike_id,
                "lane": item.lane,
                "mode": item.mode,
                "creator": item.creator,
            },
        )
        return ClipRow(clip_id, series_no, "rendering", created=True)


def _store_writing(clip_id: str, writing: ClipWriting, transcript: transcripts.WindowTranscript) -> None:
    """Save the P2 outputs on the clip row as soon as they exist (also for a clip that is then rejected)."""
    columns = writer.clip_columns(writing)
    columns["original_language"] = transcript.original_language
    assignments = ", ".join(f"{name} = %s" for name in columns)
    db.execute(f"update clips set {assignments} where id = %s", (*columns.values(), clip_id))  # noqa: S608


def _reject(clip_id: str, item: RenderInput, reason: str, *, failed: bool = False) -> None:
    """Close the clip row as rejected and tell the spike row what happened."""
    with db.transaction():
        db.execute(
            "update clips set status = 'rejected', reject_reason = %s where id = %s and status = 'rendering'",
            (reason[:500], clip_id),
        )
        if item.spike_id is not None:
            db.execute(
                "update spikes set status = %s where id = %s and status = 'rendering'",
                ("failed" if failed else "rejected", item.spike_id),
            )
        events.emit(
            "clip.render_failed" if failed else "clip.rejected",
            level="error" if failed else "info",
            entity_type="clip",
            entity_id=clip_id,
            message=reason[:500],
            data={"spike_id": item.spike_id, "stage": "factory"},
        )


def mark_failed(item: RenderInput, error: str) -> str | None:
    """A render that failed for good: the clip row is closed as rejected (`render_failed: ...`), the spike
    as failed, and the owner is told. Returns the clip id, or None when no row exists."""
    row = find_clip(item)
    if row is None:
        return None
    if row.status == "rendering":
        _reject(row.id, item, f"render_failed: {error}", failed=True)
        notify.send("warn", "Clip render failed", f"clip {row.id} ({item.creator}): {error[:300]}")
    return row.id


def _finish(clip_id: str, item: RenderInput, cfg: FactoryConfig, produced: Produced) -> None:
    outcome = produced.outcome
    analysis = produced.music
    words = [word for line in produced.lines for word in line.words]
    with db.transaction():
        db.execute(
            """
            update clips set
              start_s = %s, end_s = %s, duration_s = %s, transcript = %s, music_ratio = %s,
              profanity_count = %s, gate_profile = %s, local_path = %s, file_size_bytes = %s,
              render_ms = %s, status = 'gating'
            where id = %s
            """,
            (
                round(item.source_offset_s + produced.window.start_s, 3),
                round(item.source_offset_s + produced.window.end_s, 3),
                round(outcome.clip_duration_s, 3),
                Jsonb(transcripts.stored_transcript(words)),
                round(analysis.ratio, 4) if analysis is not None else None,
                outcome.profanity_count,
                cfg.gate_profile,
                str(outcome.output_path),
                outcome.file_size_bytes,
                outcome.render_ms,
                clip_id,
            ),
        )
        if item.spike_id is not None:
            db.execute(
                "update spikes set status = 'clipped', clip_id = %s where id = %s", (clip_id, item.spike_id)
            )
        events.emit(
            "clip.rendered",
            entity_type="clip",
            entity_id=clip_id,
            data={
                "spike_id": item.spike_id,
                "lane": item.lane,
                "mode": item.mode,
                "layout": produced.layout.kind,
                "style_id": produced.job.style_id if produced.job is not None else None,
                "duration_s": round(outcome.clip_duration_s, 3),
                "file_duration_s": round(outcome.media.duration_s, 3),
                "render_ms": outcome.render_ms,
                "timings_ms": produced.timings_ms,
                "file_size_bytes": outcome.file_size_bytes,
                "transcriber": f"{produced.transcript.provider}:{produced.transcript.model}",
                "translated": produced.transcript.translated,
                "writer_reject": produced.writing.reject_reason if produced.writing.reject else None,
                "music": analysis.as_json() if analysis is not None else None,
                "bleeps": [[round(span.start_s, 3), round(span.end_s, 3)] for span in outcome.bleeps],
                "sfx_times_s": list(outcome.sfx_times_s),
                "nvenc": outcome.nvenc,
            },
        )


def _prepare_variants(
    clip_id: str, item: RenderInput, plan: StylePlan, produced: Produced, deps: Deps, cfg: FactoryConfig
) -> None:
    """Record the reference render and queue the other styles of the plan (`factory.variants`). Never
    raises: without variants every account gets the reference render."""
    job = produced.job
    if job is None:
        return
    outcome = produced.outcome
    clip_span = ClipWindow(job.window.start_s, job.window.start_s + outcome.clip_duration_s)
    # A montage among the other styles needs the footage around the clip too (Phase E).
    montage_planned = any(
        cfg.catalog.rules_for(style).frame == "montage"
        for style in plan.styles
        if style != plan.reference_style
    )
    copy_span = job.broll_from if montage_planned and job.broll_from is not None else clip_span
    try:
        recipe = variants.recipe_json(
            lines=job.lines,
            layout=job.layout,
            duration_s=outcome.clip_duration_s,
            series_no=job.series_no,
            mode=job.mode,
            lane=job.lane,
            creator=job.creator,
            hook=job.hook,
            keywords=job.keywords,
            duck=job.duck,
            payoff_s=job.payoff_s,
            payoff_emoji=job.payoff_emoji,
            texts=job.texts,
            window_start_s=clip_span.start_s - copy_span.start_s,
            broll=montage_planned and job.broll_from is not None,
        )
        variants.after_reference(
            clip_id,
            plan,
            layout_kind=produced.layout.kind,
            output_path=outcome.output_path,
            file_size_bytes=outcome.file_size_bytes,
            duration_s=outcome.clip_duration_s,
            render_ms=outcome.render_ms,
            source=job.source_path,
            window=copy_span,
            has_audio=job.media.has_audio,
            recipe=recipe,
            ffmpeg=deps.ffmpeg,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        get_logger("factory").error("variants_prepare_failed", error=f"{type(exc).__name__}: {exc}"[:300])


def story_window(produced: Produced, story_min_s: float, story_max_s: float) -> ClipWindow | None:
    """The story cut in window time: the writer's story trim under the trim rules with the story's length
    limits, snapped like the short cut (`writer.final_window`). None without a story trim. Pure."""
    writing, base = produced.writing, produced.writer_input
    if base is None or writing.story_start_s is None or writing.story_end_s is None:
        return None
    story_writing = writing.model_copy(
        update={"start_s": writing.story_start_s, "end_s": writing.story_end_s}
    )
    story_input = replace(
        base,
        proposed=ClipWindow(writing.story_start_s, writing.story_end_s),
        spike_floor_s=base.window_s,
        min_len_s=story_min_s,
        max_len_s=story_max_s,
    )
    return writer.final_window(story_writing, story_input)


def claim_story_clip(parent_id: str, item: RenderInput, window: ClipWindow) -> ClipRow | None:
    """The `clips` row of the story cut of `parent_id` (a new one in `rendering`, under the next series
    number), or None when the parent already has one (a retried job)."""
    with db.transaction():
        db.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"clip.story:{parent_id}",))
        existing = db.fetch_one(
            "select id from clips where parent_clip_id = %s and version = 'story'", (parent_id,)
        )
        if existing is not None:
            return None
        row = db.fetch_one(
            """
            insert into clips
              (series_no, creator_id, source_platform, source_id, source_url, source_type, lane, start_s,
               end_s, gate_profile, status, version, parent_clip_id)
            values
              (nextval('clip_series_seq'), %s, %s, %s, %s, %s, 'hourly', %s, %s, 'hourly', 'rendering',
               'story', %s)
            returning id::text as id, series_no
            """,
            (
                item.creator_id,
                item.platform,
                item.source_id,
                item.source_url,
                item.source_type,
                round(item.source_offset_s + window.start_s, 3),
                round(item.source_offset_s + window.end_s, 3),
                parent_id,
            ),
        )
        assert row is not None
        story_id = str(row["id"])
        events.emit(
            "clip.render_started",
            entity_type="clip",
            entity_id=story_id,
            data={"version": "story", "parent_clip_id": parent_id, "lane": "hourly", "creator": item.creator},
        )
        return ClipRow(story_id, int(row["series_no"]), "rendering", created=True)


def render_story(
    parent_id: str, item: RenderInput, cfg: FactoryConfig, produced: Produced, deps: Deps
) -> str | None:
    """Render the story cut of a short clip (development-plan/M18 Phase C) as its own clip, from the same
    source, transcript, writer reply and layout, then queue it for the gate. Never raises: a story that
    cannot be made is logged (and its row rejected) and the short clip is unaffected. Returns its id."""
    log = get_logger("factory")
    window_w = story_window(produced, cfg.story.min_s, cfg.story.max_s)
    job = produced.job
    if window_w is None or job is None:
        return None
    window = ClipWindow(window_w.start_s + produced.offset_s, window_w.end_s + produced.offset_s)
    story_item = replace(item, spike_id=None, lane="hourly", version="story", parent_clip_id=parent_id)
    row: ClipRow | None = None
    scratch: Path | None = None
    try:
        row = claim_story_clip(parent_id, story_item, window)
        if row is None:
            return None
        lines = group_lines(produced.transcript.words)
        clip_text = clip_lines(lines, window_w)
        if not clip_text:
            raise ClipSkippedError("no_speech")
        writing = produced.writing.model_copy(update={"start_s": window_w.start_s, "end_s": window_w.end_s})
        _store_writing(row.id, writing, produced.transcript)
        analysis: music.MusicAnalysis | None = None
        if not deps.skip_music:
            try:
                analysis = music.analyse(
                    job.source_path,
                    window,
                    detector=deps.music_detector,
                    has_audio=job.media.has_audio,
                    ffmpeg=deps.ffmpeg,
                )
            except music.MusicDetectorUnavailableError as exc:
                log.warning("music_detector_unavailable", error=str(exc)[:200])
        facts = clip_facts(dict(job.texts), has_facecam=isinstance(job.layout, LayoutA))
        plan = variants.plan_for(row.id, "hourly", cfg, facts=facts, moment_type="story")
        payoff = produced.writing.payoff_s
        scratch = scratch_dir_for(row.id)
        scratch.mkdir(parents=True, exist_ok=True)
        story_job = replace(
            job,
            window=window,
            lines=clip_text,
            output_path=ready_path_for(row.id),
            scratch_dir=scratch,
            series_no=row.series_no,
            lane="hourly",
            duck=music.duck_spans(analysis, cfg.max_music_ratio, window.duration_s),
            style=cfg.catalog.rules_for(plan.reference_style),
            style_id=plan.reference_style,
            payoff_s=None if payoff is None else payoff - window_w.start_s,
        )
        started = time.monotonic()
        outcome = render_file(story_job, ffmpeg=deps.ffmpeg, ffprobe=deps.ffprobe)
        story = replace(
            produced,
            writing=writing,
            window=window,
            lines=tuple(clip_text),
            layout=framed_layout(job.layout, story_job.style),
            music=analysis,
            outcome=outcome,
            timings_ms={"render": int((time.monotonic() - started) * 1000)},
            job=story_job,
        )
        _finish(row.id, story_item, cfg, story)
        thumbs.try_make_thumb(
            row.id,
            str(outcome.output_path),
            created_at=None,
            duration_s=outcome.clip_duration_s,
            storage=deps.storage,
            ffmpeg=deps.ffmpeg,
        )
        _prepare_variants(row.id, story_item, plan, story, deps, cfg)
        jobs.enqueue("normal", JOB_GATE, {"clip_id": row.id}, dedupe_key=f"{JOB_GATE}:{row.id}")
        events.emit(
            "clip.story_rendered",
            entity_type="clip",
            entity_id=parent_id,
            data={"story_clip_id": row.id, "duration_s": round(outcome.clip_duration_s, 3)},
        )
        log.info("story_rendered", story_clip_id=row.id, duration_s=round(outcome.clip_duration_s, 2))
        return row.id
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.error("story_failed", error=f"{type(exc).__name__}: {exc}"[:300])
        if row is not None:
            reason = exc.reason if isinstance(exc, ClipSkippedError) else f"render_failed: {exc}"
            _reject(row.id, story_item, reason[:500], failed=not isinstance(exc, ClipSkippedError))
        return None
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def upload_clip(clip_id: str, *, storage: clip_storage.ClipStorage | None = None) -> str:
    """Upload the finished clip to the private bucket and save the key in `clips.storage_path`.

    Called after the gate passes (docs/09 section 1). A clip that already has a storage path is not
    uploaded again. Returns the storage key.
    """
    row = db.fetch_one("select local_path, storage_path, created_at from clips where id = %s", (clip_id,))
    if row is None:
        raise ValueError(f"clip {clip_id} does not exist")
    if row["storage_path"]:
        return str(row["storage_path"])
    if not row["local_path"] or not Path(str(row["local_path"])).is_file():
        raise FileNotFoundError(f"clip {clip_id} has no local file to upload")
    store = storage or clip_storage.ClipStorage()
    key = store.upload(Path(str(row["local_path"])), clip_storage.clip_key(clip_id, row["created_at"]))
    db.execute("update clips set storage_path = %s where id = %s", (key, clip_id))
    events.emit("clip.uploaded", entity_type="clip", entity_id=clip_id, data={"storage_path": key})
    return key


def _pre_p2(item: RenderInput, clip_id: str, window_music: music.MusicAnalysis | None) -> str | None:
    """`factory.precheck` for this clip (AI cost plan A5a): the reason the gate is certain to reject it,
    or None. A failing check never costs the clip: it is logged and the clip goes on to the writer."""
    profile = "express" if item.lane == "spike" else "hourly"
    try:
        reason = precheck.check(
            profile,
            item.creator_id,
            clip_id,
            window_music.frames if window_music is not None else None,
            window_music.frame_s if window_music is not None else music.FRAME_S,
            datetime.now(UTC),
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        get_logger("factory").warning("pre_p2_check_failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return None
    scoring_log.record(
        "factory.pre_p2",
        decision="reject" if reason else "pass",
        clip_id=clip_id,
        spike_id=item.spike_id,
        creator_id=item.creator_id,
        reason=reason,
        sub_scores={
            "profile": profile,
            "music_measured": window_music is not None,
            "window_music_ratio": round(window_music.ratio, 4) if window_music is not None else None,
        },
    )
    return reason


def render_clip(item: RenderInput, *, deps: Deps | None = None, force: bool = False) -> str:
    """Make the clip for `item` and return its clip id.

    The row goes `rendering` → `gating` (file in `$DATA_DIR/ready/<clip_id>.mp4`), or `rendering` →
    `rejected` when there is no speech or the clip writer rejects the clip. Calling it again for the same
    `clip_id` / `spike_id` returns the same id: a finished clip is not rendered twice, an unfinished one is
    rendered again under its first series number. Any other error is raised with the row left in
    `rendering`, so the job can be retried; `mark_failed` closes it after the last attempt.
    """
    deps = deps or Deps()
    log = get_logger("factory")
    row = claim_clip(item, force=force)
    with structlog.contextvars.bound_contextvars(clip_id=row.id, spike_id=item.spike_id):
        if row.status != "rendering":
            log.info("clip_render_skipped", reason="already_done", status=row.status)
            return row.id
        cfg = FactoryConfig.load(item.lane)
        scratch = scratch_dir_for(row.id)
        ai_lane: BudgetKind = "express" if item.mode == "express" else "vod"
        # development-plan/M18: one style per platform account, picked once the writer's reply (moment
        # type, texts) and the facecam are known; this render is the reference account's.
        planned: list[StylePlan] = []

        def choose_style(writing: ClipWriting, layout: Layout, has_broll: bool) -> tuple[StyleRules, str]:
            facts = clip_facts(
                writing.style_texts(), has_facecam=isinstance(layout, LayoutA), broll=has_broll
            )
            plan = variants.plan_for(row.id, item.lane, cfg, facts=facts, moment_type=writing.moment_type)
            planned.append(plan)
            log.info(
                "style_plan",
                reference_style=plan.reference_style,
                by_account=plan.by_account,
                moment_type=writing.moment_type,
                facts=sorted(facts),
            )
            return cfg.catalog.rules_for(plan.reference_style), plan.reference_style

        def choose_writing(writer_input: writer.WriterInput) -> ClipWriting:
            return writer.write(writer_input, lane=ai_lane, client=deps.ai_client)

        def before_writer(window_music: music.MusicAnalysis | None) -> str | None:
            return _pre_p2(item, row.id, window_music)

        def choose_layout(media: tools.MediaInfo, time_s: float) -> Layout:
            return facecam.get_layout(
                item.creator_id,
                item.source_path,
                time_s,
                media.width,
                media.height,
                lane=ai_lane,
                ref_id=row.id,
                client=deps.ai_client,
                ffmpeg=deps.ffmpeg,
            )

        try:
            scratch.mkdir(parents=True, exist_ok=True)
            produced = produce(
                item,
                cfg,
                deps=deps,
                scratch_dir=scratch,
                output_path=ready_path_for(row.id),
                series_no=row.series_no,
                choose_writing=choose_writing,
                choose_layout=choose_layout,
                on_writing=lambda writing, transcript: _store_writing(row.id, writing, transcript),
                clip_id=row.id,
                choose_style=choose_style,
                before_writer=before_writer if precheck.enabled() else None,
            )
        except ClipSkippedError as skipped:
            log.info("clip_skipped", reason=skipped.reason)
            _reject(row.id, item, skipped.reason)
            return row.id
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        _finish(row.id, item, cfg, produced)
        if planned:
            _prepare_variants(row.id, item, planned[-1], produced, deps, cfg)
        if produced.writing.has_story and wants_story(item, cfg):
            render_story(row.id, item, cfg, produced, deps)
        # The console's frame of the clip, made before the gate so a rejected clip has one too. Never
        # raises: a missing thumbnail must not fail the render.
        thumbs.try_make_thumb(
            row.id,
            str(produced.outcome.output_path),
            created_at=None,
            duration_s=produced.outcome.clip_duration_s,
            storage=deps.storage,
            ffmpeg=deps.ffmpeg,
        )
        if item.upload:
            upload_clip(row.id, storage=deps.storage)
        return row.id


# ── Job handlers ─────────────────────────────────────────────

AfterRender = Callable[[str, jobs.Job], None]
# What runs after a clip reaches `gating`, per job type (the gate, then posting for the spike lane). Set by
# the milestones that own those steps with `set_after_render`.
_after_render: dict[str, AfterRender] = {}


def set_after_render(job_type: str, callback: AfterRender | None) -> None:
    """Register (or clear, with None) the step that follows a successful render of `job_type`."""
    if callback is None:
        _after_render.pop(job_type, None)
    else:
        _after_render[job_type] = callback


def next_budget_reset(now: datetime | None = None) -> datetime:
    """When a job stopped by the daily AI budget may run again: shortly after the next 00:00 UTC."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(seconds=BUDGET_RETRY_MARGIN_S)


def _defer(job: jobs.Job) -> None:
    """Queue the same work again for after the budget reset; the current job then ends as done."""
    run_after = next_budget_reset()
    jobs.enqueue(
        "express" if job.queue == "express" else "normal",
        job.type,
        job.payload,
        priority=job.priority,
        run_after=run_after,
        dedupe_key=f"{job.dedupe_key}:budget" if job.dedupe_key else None,
        max_attempts=job.max_attempts,
    )
    events.emit(
        "clip.render_deferred",
        level="warn",
        entity_type="job",
        entity_id=job.id,
        message="ai.daily_budget_usd is used up",
        data={"run_after": run_after.isoformat(), "spike_id": job.payload.get("spike_id")},
    )


def budget_deferrable(item: RenderInput) -> bool:
    """Whether a render stopped by the daily AI budget can wait for the reset. A live render reads a cut of
    the session buffer, which is gone by 00:00 UTC: 402 of 425 such deferrals failed on a missing file
    (production, 11-19 to 11-22). Section files of a video stay until the video is cleaned up. Pure."""
    return item.source_type != "live"


BUDGET_SKIPPED = "budget_skipped"


def _skip_for_budget(item: RenderInput, job: jobs.Job) -> None:
    """Close a live render the budget stopped: the clip row (if one was made) is rejected as
    `budget_skipped: ...`, the spike as rejected, and a `clip.budget_skipped` event says so."""
    reason = f"{BUDGET_SKIPPED}: ai.daily_budget_usd is used up and a live cut cannot wait for the reset"
    row = find_clip(item)
    if row is not None:
        _reject(row.id, item, reason)
    elif item.spike_id is not None:
        db.execute(
            "update spikes set status = 'rejected' where id = %s and status in ('confirmed', 'rendering')",
            (item.spike_id,),
        )
    events.emit(
        "clip.budget_skipped",
        level="warn",
        entity_type="clip" if row is not None else "job",
        entity_id=row.id if row is not None else job.id,
        message=reason,
        data={"spike_id": item.spike_id, "lane": item.lane, "job_id": job.id},
    )


def _handle(job: jobs.Job, default_mode: str) -> None:
    item = RenderInput.from_payload(job.payload, default_mode=default_mode)
    try:
        clip_id = render_clip(item)
    except BudgetExceededError:
        if budget_deferrable(item):
            _defer(job)
        else:
            _skip_for_budget(item, job)
        return
    except Exception as exc:
        if job.attempts >= job.max_attempts:
            mark_failed(item, f"{type(exc).__name__}: {exc}")
        raise
    callback = _after_render.get(job.type)
    if callback is not None:
        row = db.fetch_one("select status from clips where id = %s", (clip_id,))
        if row is not None and row["status"] == "gating":
            callback(clip_id, job)


def handle_clip_render(job: jobs.Job) -> None:
    """Job `clip.render` (queue `normal`): local transcription, `factory.preset_normal`, two-pass loudness."""
    _handle(job, "normal")


def handle_express_render(job: jobs.Job) -> None:
    """Job `express.render` (queue `express`): hosted transcription, `factory.preset_express`."""
    _handle(job, "express")


def register_handlers(job_types: Sequence[str] = (JOB_CLIP_RENDER, JOB_EXPRESS_RENDER)) -> None:
    """Register the factory's job handlers with the worker (safe to call more than once)."""
    from sixstars.services import worker

    handlers: dict[str, jobs.Handler] = {
        JOB_CLIP_RENDER: handle_clip_render,
        JOB_EXPRESS_RENDER: handle_express_render,
    }
    for job_type in job_types:
        if worker.HANDLERS.get(job_type) is not handlers[job_type]:
            worker.register(job_type, handlers[job_type])

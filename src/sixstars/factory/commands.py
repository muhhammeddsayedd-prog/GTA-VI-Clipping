"""The `sixstars clip` commands: `render-file` (a local video, no clip row: for design work) and `render`
(one stored spike through the whole pipeline). Each returns a process exit code.
"""

import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer

from sixstars import db
from sixstars.ai.calls import ChatLine
from sixstars.ai.schemas import ClipWriting
from sixstars.clients.anthropic_client import BudgetKind
from sixstars.factory import facecam, tools, writer
from sixstars.factory.config import FactoryConfig
from sixstars.factory.layout import Box, Layout, LayoutA, LayoutB
from sixstars.factory.pipeline import (
    ClipSkippedError,
    Deps,
    Produced,
    RenderInput,
    produce,
    render_clip,
)
from sixstars.factory.subtitles import keyword_flags
from sixstars.factory.transcript import default_transcriber

STILL_TIMES_S = (1.0, 5.0)


def parse_box(text: str) -> Box:
    """`"x,y,w,h"` in source pixels."""
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        raise ValueError("a box is x,y,w,h")
    x, y, w, h = (int(part) for part in parts)
    return Box(x, y, w, h)


def parse_overrides(pairs: Sequence[str]) -> dict[str, object]:
    """`key=json` pairs (repeatable `--set`) as a mapping of settings keys to values."""
    values: dict[str, object] = {}
    for pair in pairs:
        key, separator, raw = pair.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"--set takes key=json, got {pair!r}")
        values[key.strip()] = json.loads(raw)
    return values


def manual_writing(item: writer.WriterInput, hook: str, keywords: Sequence[str]) -> ClipWriting:
    """A clip writer reply typed by hand: the window as given, the hook and keywords from the options.

    Each keyword is highlighted on every subtitle line that contains it.
    """
    highlights: list[dict[str, object]] = []
    for line in item.lines:
        found = [word for word in keywords if any(keyword_flags(line.text.split(), [word]))]
        if found:
            highlights.append({"line": line.index, "words": found[:2]})
    return ClipWriting.model_validate(
        {
            "start_s": item.proposed.start_s,
            "end_s": item.proposed.end_s,
            "hook": hook or "Watch this",
            "title": "render-file test clip",
            "caption": "render-file test clip",
            "hashtags": ["test", "clip", "render"],
            "topic": "render-file-test",
            "keywords": highlights,
            "spoiler": False,
            "safety": {},
            "score": 5,
            "score_parts": {"hook": 5, "clarity": 5, "payoff": 5, "rewatch": 5},
            "reject": False,
        }
    )


def save_stills(
    video: Path, directory: Path, clip_duration_s: float, file_duration_s: float, ffmpeg: str = "ffmpeg"
) -> list[Path]:
    """Save stills at 1 s, 5 s, the middle of the clip and the middle of the end card."""
    directory.mkdir(parents=True, exist_ok=True)
    end_card = (clip_duration_s + file_duration_s) / 2
    if file_duration_s - clip_duration_s < 0.1:
        end_card = max(0.0, file_duration_s - 0.2)
    times = {"01s": 1.0, "05s": 5.0, "mid": clip_duration_s / 2, "end": end_card}
    paths: list[Path] = []
    for name, time_s in times.items():
        if time_s >= file_duration_s:
            continue
        path = directory / f"{video.stem}_{name}.png"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{time_s:.3f}"]
        command += ["-i", str(video), "-frames:v", "1", "-update", "1", str(path)]
        tools.run(command, timeout_s=tools.FRAME_TIMEOUT_S, what="still")
        paths.append(path)
    return paths


def _summary(produced: Produced, stills: Sequence[Path]) -> dict[str, Any]:
    outcome = produced.outcome
    return {
        "output": str(outcome.output_path),
        "layout": produced.layout.kind,
        "window_s": [round(produced.window.start_s, 3), round(produced.window.end_s, 3)],
        "clip_duration_s": round(outcome.clip_duration_s, 3),
        "file_duration_s": round(outcome.media.duration_s, 3),
        "file_size_bytes": outcome.file_size_bytes,
        "render_ms": outcome.render_ms,
        "timings_ms": produced.timings_ms,
        "hook": produced.writing.hook,
        "title": produced.writing.title,
        "keywords": {str(line): words for line, words in produced.writing.keyword_map().items()},
        "transcriber": f"{produced.transcript.provider}:{produced.transcript.model}",
        "language": produced.transcript.original_language,
        "words": sum(len(line.words) for line in produced.lines),
        "music_ratio": round(produced.music.ratio, 4) if produced.music is not None else None,
        "profanity_count": outcome.profanity_count,
        "bleeps": [[round(span.start_s, 3), round(span.end_s, 3)] for span in outcome.bleeps],
        "sfx_times_s": list(outcome.sfx_times_s),
        "stills": [str(path) for path in stills],
    }


def render_file(
    video: Path,
    *,
    start: float,
    end: float,
    creator: str,
    lane: str = "hourly",
    mode: str = "normal",
    output: Path | None = None,
    hook: str = "",
    keywords: Sequence[str] = (),
    facecam_box: str = "",
    layout: str = "auto",
    series_no: int = 1,
    use_ai: bool = False,
    title: str = "",
    whisper_model: str = "",
    set_pairs: Sequence[str] = (),
    stills_dir: Path | None = None,
    no_music: bool = False,
) -> int:
    """Render a local video file without writing a clip row.

    Without `use_ai` nothing but the file is needed: the settings are the documented defaults (plus
    `--set` overrides), the window is used as given, the hook and keywords come from the options and the
    layout is B unless a facecam box is given. With `use_ai` the settings come from the database and the
    clip writer (P2) and the layout detection (P4) run as in production.
    """
    if not video.is_file():
        typer.echo(f"no such file: {video}", err=True)
        return 2
    try:
        overrides = parse_overrides(set_pairs)
        forced_box = parse_box(facecam_box) if facecam_box else None
        item = RenderInput(
            source_path=video,
            window_start_s=start,
            window_end_s=end,
            creator=creator,
            lane=lane,
            mode=mode,
            stream_title=title,
            spike_floor_s=None,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        return 2
    if use_ai:
        cfg = FactoryConfig.load(lane)
        if overrides:
            typer.echo("--set is ignored with --ai: the settings table is used", err=True)
    else:
        if whisper_model:
            overrides["vod.whisper_model"] = whisper_model
        cfg = FactoryConfig.offline(lane, overrides)
        # Nothing but the file is needed without --ai: no model is asked for sentence ends either.
        cfg = replace(cfg, snap=replace(cfg.snap, punctuate_below=0.0))
    model = whisper_model or cfg.whisper_model
    deps = Deps(transcriber=default_transcriber(model), skip_music=no_music)
    ai_lane: BudgetKind = "express" if mode == "express" else "vod"

    def choose_writing(writer_input: writer.WriterInput) -> ClipWriting:
        if use_ai:
            return writer.write(writer_input, lane=ai_lane)
        return manual_writing(writer_input, hook, keywords)

    def choose_layout(media: tools.MediaInfo, time_s: float) -> Layout:
        if forced_box is not None:
            return LayoutA(facecam_box=forced_box)
        if layout.lower() == "b" or not use_ai:
            return LayoutB()
        return facecam.get_layout(
            None,
            video,
            time_s,
            media.width,
            media.height,
            lane=ai_lane,
        )

    target = output or video.with_name(f"{video.stem}_{lane}_{mode}.mp4")
    scratch = Path(tempfile.mkdtemp(prefix="sixstars-render-"))
    try:
        produced = produce(
            item,
            cfg,
            deps=deps,
            scratch_dir=scratch,
            output_path=target,
            series_no=series_no,
            choose_writing=choose_writing,
            choose_layout=choose_layout,
        )
    except ClipSkippedError as skipped:
        typer.echo(f"no clip: {skipped.reason}", err=True)
        return 1
    except (tools.FfmpegError, ValueError) as exc:
        typer.echo(f"render failed: {exc}", err=True)
        return 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    stills: list[Path] = []
    if stills_dir is not None:
        outcome = produced.outcome
        stills = save_stills(target, stills_dir, outcome.clip_duration_s, outcome.media.duration_s)
    typer.echo(json.dumps(_summary(produced, stills), ensure_ascii=True, indent=2))
    return 0


def _spike_chat(sample: object, peak_s: float) -> tuple[ChatLine, ...]:
    lines: list[ChatLine] = []
    for entry in sample if isinstance(sample, list) else []:
        if not isinstance(entry, Mapping) or "text" not in entry:
            continue
        offset = entry.get("t_rel")
        if not isinstance(offset, int | float):
            continue
        user = entry.get("user")
        lines.append(
            ChatLine(t_s=peak_s + float(offset), text=str(entry["text"]), user=str(user) if user else None)
        )
    return tuple(lines)


def input_from_spike(
    spike_id: str,
    source_path: Path,
    *,
    source_offset_s: float = 0.0,
    lane: str | None = None,
    mode: str | None = None,
    window: tuple[float, float] | None = None,
    upload: bool = False,
) -> RenderInput:
    """Build the render input for a stored spike and a local file that holds its window.

    `source_offset_s` is where the file starts in the original video (the padded window start for a
    section downloaded by the VOD lane). A replay spike carries its window in video seconds; a live spike
    does not, so `window` (seconds in the file) must be given for it.
    """
    row = db.fetch_one(
        """
        select s.*, c.handle, c.platform as creator_platform,
               v.title as video_title, v.source_id as video_source_id, v.url as video_url,
               l.source_id as live_source_id, l.url as live_url
        from spikes s
        join creators c on c.id = s.creator_id
        left join videos v on v.id = s.video_id
        left join live_sessions l on l.id = s.live_session_id
        where s.id = %s
        """,
        (spike_id,),
    )
    if row is None:
        raise ValueError(f"spike {spike_id} does not exist")
    parts = row["parts"] if isinstance(row["parts"], Mapping) else {}
    live = row["mode"] == "live"

    def position(value: object) -> float | None:
        return float(value) - source_offset_s if isinstance(value, int | float) else None

    if window is not None:
        start, end = window
    elif row["window_start_s"] is not None and row["window_end_s"] is not None:
        start = float(row["window_start_s"]) - source_offset_s
        end = float(row["window_end_s"]) - source_offset_s
    else:
        raise ValueError("this spike has no window in video seconds: pass the window inside the file")
    peak = float(row["spike_offset_s"]) - source_offset_s if row["spike_offset_s"] is not None else start
    chosen_lane = lane or ("spike" if live and row["tier"] == "S" else "hourly")
    return RenderInput(
        source_path=source_path,
        window_start_s=max(0.0, start),
        window_end_s=end,
        spike_floor_s=None if live else position(parts.get("spike_floor")),
        creator=str(row["handle"]),
        creator_id=str(row["creator_id"]),
        platform=str(row["creator_platform"]),
        source_id=str(row["video_source_id"] or row["live_source_id"] or ""),
        source_url=str(row["video_url"] or row["live_url"] or ""),
        source_type="live" if live else "vod",
        lane=chosen_lane,
        mode=mode or ("express" if chosen_lane == "spike" else "normal"),
        stream_title=str(row["video_title"] or ""),
        spike_id=spike_id,
        pad_start_s=None if live else position(parts.get("pad_start")),
        pad_end_s=None if live else position(parts.get("pad_end")),
        source_offset_s=source_offset_s,
        chat=_spike_chat(row["chat_sample"], peak),
        upload=upload,
    )


def render_spike(
    spike_id: str,
    *,
    source: Path | None,
    source_offset_s: float = 0.0,
    lane: str = "",
    mode: str = "",
    upload: bool = False,
    force: bool = False,
) -> int:
    """Render the clip of one stored spike from a local file that holds its window."""
    if source is None:
        typer.echo(
            "--source is needed: the local video file that holds the spike window "
            "(the VOD lane downloads it; see `sixstars vod process`)",
            err=True,
        )
        return 2
    if not source.is_file():
        typer.echo(f"no such file: {source}", err=True)
        return 2
    try:
        item = input_from_spike(
            spike_id,
            source,
            source_offset_s=source_offset_s,
            lane=lane or None,
            mode=mode or None,
            upload=upload,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        return 2
    clip_id = render_clip(item, force=force)
    row = db.fetch_one(
        "select status, series_no, local_path, storage_path, reject_reason, render_ms from clips "
        "where id = %s",
        (clip_id,),
    )
    typer.echo(json.dumps({"clip_id": clip_id, **(row or {})}, default=str, indent=2))
    return 0 if row is not None and row["status"] != "rejected" else 1

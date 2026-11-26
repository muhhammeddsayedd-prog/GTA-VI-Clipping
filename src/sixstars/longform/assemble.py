"""Assemble the episode (M20 L4): cold open + segments N..1 + end card, chapters, title, description,
thumbnail, upload to Storage, notify. Safe to run again: an episode past `assembling` is left alone.
"""

import subprocess
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from sixstars import config, db, events, jobs, notify, settings
from sixstars.factory.assets import load_assets
from sixstars.factory.escape import normalize_filter_path, render_filter
from sixstars.factory.presets import audio_encoder_args, container_args
from sixstars.factory.tools import probe, run
from sixstars.logging import get_logger
from sixstars.longform import chapters as ch
from sixstars.longform import package as pkg
from sixstars.longform import thumb as thumbs
from sixstars.longform.build import JOB_ASSEMBLE, episode_dir
from sixstars.longform.config import LongformConfig, RenderSpec
from sixstars.longform.segment import video_encoder_args
from sixstars.longform.subs16x9 import OverlayStyle, build_title_ass, creator_label
from sixstars.storage import EPISODES_BUCKET, ClipStorage

THUMB_W, THUMB_H = 1280, 720  # YouTube's recommended 16:9 thumbnail size
log = get_logger(__name__)

ITEMS_SQL = """
select ei.position, ei.segment_path, ei.duration_s, coalesce(cl.title, cl.hook, '') as title,
       coalesce(cr.title_name, cr.display_name, cr.handle) as creator, cr.platform, cr.handle
  from episode_items ei
  join clips cl on cl.id = ei.clip_id
  join creators cr on cr.id = ei.creator_id
 where ei.episode_id = %s and ei.status = 'rendered'
 order by ei.position desc
"""


def _ass_filter(path: Path, fonts_dir: Path | None) -> str:
    options = [("filename", normalize_filter_path(str(path)))]
    if fonts_dir is not None:
        options.append(("fontsdir", normalize_filter_path(str(fonts_dir))))
    return render_filter("ass", options)


def cold_open_command(
    segment: Path, seconds: float, ass: Path, fonts_dir: Path | None, render: RenderSpec, out: Path
) -> list[str]:
    """The last `seconds` of #1 (its payoff) with the cold-open line on top, encoded like a segment."""
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-sseof", f"-{seconds:.3f}", "-i", str(segment),
        "-vf", f"{_ass_filter(ass, fonts_dir)},format=yuv420p",
        *video_encoder_args(render), *audio_encoder_args(), *container_args(), str(out),
    ]  # fmt: skip


def end_card_command(
    seconds: float, ass: Path, fonts_dir: Path | None, render: RenderSpec, out: Path
) -> list[str]:
    """A plain dark card with the end-card line, silent stereo audio, encoded like a segment."""
    size = f"{render.width}x{render.height}"
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"color=c=0x0d0d10:s={size}:r={render.fps}:d={seconds:.3f}",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={seconds:.3f}",
        "-vf", f"{_ass_filter(ass, fonts_dir)},format=yuv420p", "-shortest",
        *video_encoder_args(render), *audio_encoder_args(), *container_args(), str(out),
    ]  # fmt: skip


def concat_command(list_file: Path, out: Path) -> list[str]:
    """Join same-format parts without re-encoding."""
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart", str(out),
    ]  # fmt: skip


def concat_list(parts: Sequence[Path]) -> str:
    """The concat demuxer's list file (single quotes escaped)."""
    return "".join("file '{}'\n".format(str(p).replace("'", "'\\''")) for p in parts)


def thumbnail_command(segment: Path, at_s: float, ass: Path, fonts_dir: Path | None, out: Path) -> list[str]:
    """One frame of #1, scaled to 1280x720, with the big words on top, as JPEG."""
    scale = f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease:flags=lanczos"
    pad = f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2"
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-ss", f"{at_s:.3f}", "-i", str(segment),
        "-frames:v", "1",
        "-vf", f"{scale},{pad},{_ass_filter(ass, fonts_dir)}", "-q:v", "2", str(out),
    ]  # fmt: skip


def thumb_style(style: OverlayStyle) -> OverlayStyle:
    """The title style at thumbnail size."""
    return OverlayStyle(**{**style.__dict__, "title_size": style.thumb_text_size, "mark_text": ""})


def moments(rows: Sequence[dict[str, Any]]) -> list[ch.Moment]:
    return [
        ch.Moment(
            position=int(r["position"]),
            creator=str(r["creator"] or r["handle"]),
            title=str(r["title"]),
            credit=f"https://{creator_label(str(r['platform']), str(r['handle']))}",
            duration_s=float(r["duration_s"] or 0),
        )
        for r in rows
    ]


EPISODE_SQL = """
select e.episode_date, e.status, e.format,
       coalesce(cr.title_name, cr.display_name, cr.handle) as featured, cr.avatar_url
  from episodes e
  left join creators cr on cr.id = e.featured_creator_id
 where e.id = %s
"""


def min_seconds(fmt: str, cfg: LongformConfig) -> float:
    """The shortest episode worth posting: the creator-day minimum on a creator day."""
    return cfg.solo.min_s if fmt == "creator" else cfg.min_s


def assemble(episode_id: str, cfg: LongformConfig | None = None) -> str:
    """Build the final MP4 and its packaging. Returns the episode status."""
    cfg = cfg or LongformConfig.load()
    ep = db.fetch_one(EPISODE_SQL, (episode_id,))
    if ep is None:
        return "missing"
    if ep["status"] not in ("fetching", "rendering", "assembling"):
        return str(ep["status"])
    db.execute("update episodes set status = 'assembling' where id = %s", (episode_id,))
    day: date = ep["episode_date"]
    fmt = str(ep["format"] or "mix")
    featured = str(ep["featured"]) if fmt == "creator" and ep["featured"] else None
    rows = [dict(r) for r in db.fetch_all(ITEMS_SQL, (episode_id,))]
    played = moments(rows)
    total_moments = sum(m.duration_s for m in played)
    minimum = min_seconds(fmt, cfg)
    if len(played) < ch.MIN_CHAPTERS or total_moments < minimum:
        reason = f"only {len(played)} moments / {total_moments:.0f}s rendered (minimum {minimum:.0f}s)"
        db.execute("update episodes set status = 'failed', error = %s where id = %s", (reason, episode_id))
        events.emit("episode.failed", "warn", "episode", episode_id, reason)
        notify.send("warn", f"Episode {day} not built", reason)
        return "failed"
    work = episode_dir(episode_id)
    assets = load_assets(settings.get_str("factory.font", "Montserrat Black"))
    fonts = Path(assets.fonts.fonts_dir) if assets.fonts.fonts_dir else None
    r, style = cfg.render, cfg.style
    best = Path(rows[-1]["segment_path"])  # position 1 plays last
    cold_ass, end_ass = work / "cold.ass", work / "end.ass"
    cold_ass.write_text(
        build_title_ass(
            style.cold_open_text, duration_s=cfg.cold_open_s, style=style, width=r.width, height=r.height
        ),
        encoding="utf-8",
    )
    end_ass.write_text(
        build_title_ass(
            style.end_card_text, duration_s=cfg.end_card_s, style=style, width=r.width, height=r.height
        ),
        encoding="utf-8",
    )
    timeout = max(cfg.max_s * 2, 600.0)
    cold, end = work / "part-cold.mp4", work / "part-end.mp4"
    run(
        cold_open_command(best, cfg.cold_open_s, cold_ass, fonts, r, cold),
        timeout_s=timeout,
        what="cold open",
    )
    run(end_card_command(cfg.end_card_s, end_ass, fonts, r, end), timeout_s=timeout, what="end card")
    parts = [cold, *(Path(row["segment_path"]) for row in rows), end]
    list_file = work / "parts.txt"
    list_file.write_text(concat_list(parts), encoding="utf-8")
    final = work / f"episode-{day.isoformat()}.mp4"
    run(concat_command(list_file, final), timeout_s=timeout, what="episode concat")
    total = probe(final).duration_s
    cold_s = probe(cold).duration_s
    chs = ch.chapters(played, cold_s)
    packed = pkg.write(episode_id, played, day=day, fmt=fmt, featured=featured, length_s=total)
    title = packed.title
    footer = settings.get_str("posting.youtube_description_footer", "")
    description = ch.description(played, chs, day, footer, intro=packed.intro, hashtags=packed.hashtags)
    best_first = [Path(row["segment_path"]) for row in reversed(rows)]
    made = thumbs.make(
        episode_id,
        best_first,
        work,
        fmt=fmt,
        n=len(played),
        thumb_text=packed.thumb_text,
        scene=packed.thumb_scene,
        featured=featured,
        avatar_url=ep["avatar_url"] if fmt == "creator" else None,
        spec=cfg.thumb,
    )
    thumb = made.path
    env_name = config.env().APP_ENV
    key = f"{EPISODES_BUCKET}/{env_name}/{day:%Y/%m/%d}/{episode_id}.mp4"
    thumb_key = f"{EPISODES_BUCKET}/{env_name}/{day:%Y/%m/%d}/{episode_id}.jpg"
    store = ClipStorage()
    store.upload_resumable(final, key)
    store.upload(thumb, thumb_key, "image/jpeg")
    db.execute(
        """
        update episodes
           set status = 'ready', n_moments = %s, duration_s = %s, title = %s, description = %s, chapters = %s,
               tags = %s, thumb_text = %s, storage_path = %s, thumb_path = %s, local_path = %s,
               hashtags = %s, thumb_scene = %s, thumb_source = %s,
               error = case when %s then null else 'chapters not valid for YouTube' end
         where id = %s
        """,
        (
            len(played), round(total, 1), title, description,
            db_json([{"start_s": c.start_s, "label": c.label} for c in chs]),
            list(packed.tags), packed.thumb_text,
            key, thumb_key, str(final), list(packed.hashtags), packed.thumb_scene, made.source,
            ch.valid(chs, total), episode_id,
        ),
    )  # fmt: skip
    for part in (cold, end, cold_ass, end_ass, list_file):
        part.unlink(missing_ok=True)
    minutes, seconds = divmod(int(total), 60)
    events.emit(
        "episode.ready",
        "info",
        "episode",
        episode_id,
        title,
        {
            "duration_s": round(total, 1),
            "format": fmt,
            "package": packed.source,
            "thumb": made.source,
            "thumb_usd": made.cost_usd,
        },
    )
    kind = f"Creator day: {featured}" if featured else "Mix day"
    notify.send(
        "info",
        f"Episode {day} ready",
        f"{title}\n{kind}, {len(played)} moments, {minutes}:{seconds:02d}\n"
        f"Text: {packed.source}, thumbnail: {made.source}\nOpen the console: Episodes",
    )
    log.info("episode_ready", episode_id=episode_id, duration_s=total, n_moments=len(played))
    return "ready"


def db_json(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def handle_assemble(job: jobs.Job) -> None:
    """Job `longform.assemble`."""
    try:
        assemble(str(job.payload["episode_id"]))
    except (subprocess.SubprocessError, OSError) as exc:
        if job.attempts + 1 >= job.max_attempts:
            db.execute(
                "update episodes set status = 'failed', error = %s where id = %s",
                (str(exc)[:500], str(job.payload["episode_id"])),
            )
        raise


__all__ = ["JOB_ASSEMBLE", "assemble", "handle_assemble"]

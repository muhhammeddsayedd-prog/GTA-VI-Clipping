"""Build the moments of a planned episode (M20 L3), in two phases.

1. `longform.item` (one job per moment): re-download the moment (VOD range, or the live session's archive
   VOD), transcribe it locally, find the clip in it and choose the full cut. The file and the segment's words
   are kept on the item (status `fetched`); a moment that cannot be downloaded is `dropped`.
2. When no moment is still `planned`, the kept ones are renumbered without gaps (the #N badge is burned into
   the segment, so numbers are final before any render) and one `longform.render` job per moment renders
   its 16:9 segment and deletes the download. The last render queues `longform.assemble`.

Every job is safe to run again: finished work is detected and skipped.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import config, db, events, jobs, settings
from sixstars.factory.assets import load_assets
from sixstars.factory.bleep import bleep_spans
from sixstars.factory.lines import Word
from sixstars.factory.tools import probe
from sixstars.factory.transcript import default_transcriber, transcribe_window
from sixstars.factory.window import ClipWindow
from sixstars.logging import get_logger
from sixstars.longform.align import align, stored_words, words_in
from sixstars.longform.config import LongformConfig
from sixstars.longform.segment import SegmentJob, render_segment
from sixstars.longform.subs16x9 import build_segment_ass, creator_label
from sixstars.vod import sections, ytdlp
from sixstars.vod.config import VodConfig

JOB_ITEM = "longform.item"
JOB_RENDER = "longform.render"
JOB_ASSEMBLE = "longform.assemble"
log = get_logger(__name__)

ITEM_SQL = """
select ei.id::text as item_id, ei.episode_id::text as episode_id, ei.position, ei.est_s, ei.status,
       ei.segment_path, ei.source_path, ei.words, ei.cut_start_s, ei.cut_end_s,
       c.id::text as clip_id, c.source_type, c.source_platform, c.source_url,
       c.start_s, c.end_s, c.transcript, cr.platform as creator_platform, cr.handle,
       ls.vod_source_id, ls.platform as session_platform
  from episode_items ei
  join clips c on c.id = ei.clip_id
  join creators cr on cr.id = ei.creator_id
  left join spikes s on s.id = c.spike_id
  left join live_sessions ls on ls.id = s.live_session_id
 where ei.id = %s
"""


class SourceGoneError(RuntimeError):
    """The moment cannot be downloaded any more (no archive, deleted, sub-only)."""


@dataclass(frozen=True)
class Source:
    url: str
    platform: str
    start_s: float
    end_s: float


def episode_dir(episode_id: str) -> Path:
    return config.env().DATA_DIR / "longform" / episode_id


def source_of(row: dict[str, Any]) -> Source:
    """Where to download the moment from, with the clip's positions in that video."""
    start, end = float(row["start_s"]), float(row["end_s"])
    if row["source_type"] == "live":
        vod = row["vod_source_id"]
        if not vod:
            raise SourceGoneError("live session has no archive VOD")
        platform = str(row["session_platform"] or row["source_platform"])
        url = (
            f"https://www.twitch.tv/videos/{vod}"
            if platform == "twitch"
            else f"https://www.youtube.com/watch?v={vod}"
        )
        return Source(url, platform, start, end)
    return Source(str(row["source_url"]), str(row["source_platform"]), start, end)


def fetch_range(src: Source, est_s: float, cfg: LongformConfig) -> tuple[float, float]:
    """The clip plus room to widen it toward `est_s`, plus `fetch_pad_s` on each side for the alignment."""
    extra = max(est_s - (src.end_s - src.start_s), 0.0)
    before = cfg.fetch_pad_s + extra * cfg.cut.lead_share
    after = cfg.fetch_pad_s + extra * (1 - cfg.cut.lead_share)
    return src.start_s - before, src.end_s + after


def words_json(words: list[Word]) -> dict[str, Any]:
    return {"words": [{"w": w.text, "s": w.start_s, "e": w.end_s} for w in words]}


def load_item(item_id: str) -> dict[str, Any] | None:
    row = db.fetch_one(ITEM_SQL, (item_id,))
    return None if row is None else dict(row)


def fetch_item(item_id: str, cfg: LongformConfig) -> str:
    """Phase 1: download, transcribe, align. Returns the item's new status."""
    r = load_item(item_id)
    if r is None:
        return "missing"
    if r["status"] != "planned":
        return str(r["status"])
    work = episode_dir(r["episode_id"])
    work.mkdir(parents=True, exist_ok=True)
    try:
        src = source_of(r)
    except SourceGoneError as exc:
        return drop(item_id, str(exc))
    vod = VodConfig.load()
    want_start, want_end = fetch_range(src, float(r["est_s"] or cfg.segment_min_s), cfg)
    try:
        got = sections.download_section(
            src.url,
            sections.section_for(want_start, want_end),
            work,
            f"src-{item_id}",
            access=ytdlp.access_for(src.platform),
            max_height=vod.max_height,
            timeout_s=vod.download_timeout_s,
            exact_cuts=True,
        )
    except (ytdlp.VideoUnavailableError, ValueError) as exc:
        return drop(item_id, f"download: {exc}")
    transcript = transcribe_window(
        got.path,
        ClipWindow(0.0, got.duration_s),
        modes=("local",),
        scratch_dir=work,
        transcriber=default_transcriber(cfg.whisper_model),
        translate=False,
    )
    file_words = list(transcript.words)
    nominal = src.start_s - got.start_s
    cut = align(
        file_words,
        stored_words(r["transcript"]),
        nominal_s=nominal,
        clip_len_s=src.end_s - src.start_s,
        target_s=float(r["est_s"] or cfg.segment_min_s),
        file_len_s=got.duration_s,
        rules=cfg.cut,
        min_s=cfg.segment_min_s,
        max_s=cfg.segment_max_s,
    )
    db.execute(
        """
        update episode_items
           set status = 'fetched', fetch_url = %s, fetch_start_s = %s, fetch_end_s = %s, source_path = %s,
               cut_start_s = %s, cut_end_s = %s, align_offset_s = %s, words = %s, error = null
         where id = %s
        """,
        (
            src.url,
            got.start_s,
            got.start_s + got.duration_s,
            str(got.path),
            cut.start_s,
            cut.end_s,
            round(cut.clip_start_s - nominal, 3),
            Jsonb(words_json(words_in(file_words, cut.start_s, cut.end_s))),
            item_id,
        ),
    )
    log.info("longform_item_fetched", item_id=item_id, matched=cut.matched, duration_s=cut.duration_s)
    return "fetched"


def render_item(item_id: str, cfg: LongformConfig) -> str:
    """Phase 2: render the 16:9 segment with the final position, then delete the download."""
    r = load_item(item_id)
    if r is None:
        return "missing"
    if r["status"] == "rendered" and r["segment_path"] and Path(r["segment_path"]).is_file():
        return "rendered"
    if r["status"] != "fetched" or not r["source_path"] or not Path(r["source_path"]).is_file():
        return drop(item_id, "downloaded file is missing")
    source = Path(r["source_path"])
    work = source.parent
    start, end = float(r["cut_start_s"]), float(r["cut_end_s"])
    seg_words = stored_words(r["words"])
    assets = load_assets(settings.get_str("factory.font", "Montserrat Black"))
    matcher = assets.profanity()
    bleeps = tuple(bleep_spans(seg_words, matcher.find([w.text for w in seg_words]), end - start))
    ass_path = work / f"seg-{item_id}.ass"
    ass_path.write_text(
        build_segment_ass(
            words=seg_words,
            duration_s=end - start,
            position=int(r["position"]),
            creator_label=creator_label(str(r["creator_platform"]), str(r["handle"])),
            style=cfg.style,
            width=cfg.render.width,
            height=cfg.render.height,
            profanity=matcher,
        ),
        encoding="utf-8",
    )
    out = work / f"seg-{int(r['position']):03d}-{item_id}.mp4"
    render_segment(
        SegmentJob(
            source=source,
            start_s=start,
            duration_s=end - start,
            ass_path=ass_path,
            fonts_dir=assets.fonts.fonts_dir,
            out_path=out,
            render=cfg.render,
            target_lufs=settings.get_float("factory.loudness_lufs", -14.0),
            has_audio=probe(source).has_audio,
            bleeps=bleeps,
        ),
        timeout_s=VodConfig.load().download_timeout_s,
    )
    duration = probe(out).duration_s
    db.execute(
        "update episode_items set status = 'rendered', segment_path = %s, duration_s = %s, "
        "source_path = null where id = %s",
        (str(out), round(duration, 3), item_id),
    )
    source.unlink(missing_ok=True)
    ass_path.unlink(missing_ok=True)
    return "rendered"


def drop(item_id: str, reason: str) -> str:
    db.execute(
        "update episode_items set status = 'dropped', error = %s where id = %s and status <> 'rendered'",
        (reason[:500], item_id),
    )
    events.emit("episode.item_dropped", "warn", "episode_item", item_id, reason[:200])
    log.warning("longform_item_dropped", item_id=item_id, reason=reason)
    return "dropped"


def counts(episode_id: str) -> dict[str, int]:
    rows = db.fetch_all(
        "select status, count(*) as n from episode_items where episode_id = %s group by status", (episode_id,)
    )
    return {str(r["status"]): int(r["n"]) for r in rows}


def renumber(episode_id: str) -> None:
    """Give the kept moments positions 1..k in their countdown order (gaps left by dropped ones close).
    Dropped moments move above 100000 and the kept ones pass through 200000+ so no two rows ever share a
    position (unique per episode) and every position stays >= 1."""
    with db.transaction() as conn:
        conn.execute(
            "update episode_items set position = position + 100000 "
            "where episode_id = %s and status = 'dropped' and position < 100000",
            (episode_id,),
        )
        conn.execute(
            """
            with ordered as (
              select id, row_number() over (order by position) as rn
                from episode_items where episode_id = %s and status in ('fetched', 'rendered')
            )
            update episode_items ei set position = 200000 + ordered.rn from ordered where ei.id = ordered.id
            """,
            (episode_id,),
        )
        conn.execute(
            "update episode_items set position = position - 200000 "
            "where episode_id = %s and position > 200000",
            (episode_id,),
        )


def start_build(episode_id: str, cfg: LongformConfig | None = None) -> int:
    """Queue phase 1 for every planned moment and mark the episode `fetching`."""
    cfg = cfg or LongformConfig.load()
    rows = db.fetch_all(
        "select id::text as id from episode_items where episode_id = %s and status = 'planned'", (episode_id,)
    )
    for item in rows:
        jobs.enqueue(
            "normal", JOB_ITEM, {"item_id": item["id"]}, cfg.priority, dedupe_key=f"{JOB_ITEM}:{item['id']}"
        )
    db.execute("update episodes set status = 'fetching' where id = %s and status = 'planned'", (episode_id,))
    return len(rows)


def advance(episode_id: str, cfg: LongformConfig) -> str:
    """Move the episode on when a phase has finished: renumber + render, then assemble."""
    n = counts(episode_id)
    if n.get("planned", 0) > 0:
        return "fetching"
    if n.get("fetched", 0) > 0:
        claimed = db.execute(
            "update episodes set status = 'rendering' where id = %s and status = 'fetching'", (episode_id,)
        )
        if claimed:
            renumber(episode_id)
            for item in db.fetch_all(
                "select id::text as id from episode_items where episode_id = %s and status = 'fetched'",
                (episode_id,),
            ):
                jobs.enqueue(
                    "normal",
                    JOB_RENDER,
                    {"item_id": item["id"]},
                    cfg.priority,
                    dedupe_key=f"{JOB_RENDER}:{item['id']}",
                )
        return "rendering"
    jobs.enqueue(
        "normal",
        JOB_ASSEMBLE,
        {"episode_id": episode_id},
        cfg.priority,
        dedupe_key=f"{JOB_ASSEMBLE}:{episode_id}",
    )
    return "assembling"


def episode_of(item_id: str) -> str | None:
    row = db.fetch_one("select episode_id::text as e from episode_items where id = %s", (item_id,))
    return None if row is None else str(row["e"])


def handle_item(job: jobs.Job) -> None:
    """Job `longform.item` (phase 1). A download that keeps failing drops the moment on the last attempt."""
    cfg = LongformConfig.load()
    item_id = str(job.payload["item_id"])
    try:
        fetch_item(item_id, cfg)
    except ytdlp.YtDlpError as exc:
        if job.attempts + 1 < job.max_attempts:
            raise
        drop(item_id, f"download failed: {exc}")
    episode_id = episode_of(item_id)
    if episode_id is not None:
        advance(episode_id, cfg)


def handle_render(job: jobs.Job) -> None:
    """Job `longform.render` (phase 2)."""
    cfg = LongformConfig.load()
    item_id = str(job.payload["item_id"])
    render_item(item_id, cfg)
    episode_id = episode_of(item_id)
    if episode_id is not None:
        advance(episode_id, cfg)


def build_day(day: date) -> int:
    """Start building the planned episode of `day` (CLI/route). Returns the number of moments queued."""
    row = db.fetch_one(
        "select id::text as id from episodes where episode_date = %s and status = 'planned'", (day,)
    )
    return 0 if row is None else start_build(str(row["id"]))

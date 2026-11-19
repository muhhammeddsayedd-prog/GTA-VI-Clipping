"""One render per design style of a clip (development-plan/M18 Phase A, decision D114).

The clip factory renders a clip once, in the style the director picked for the reference account
(`director.plan_styles`). When the plan gives other accounts other styles:

1. `after_reference` stores the plan, records the reference render in `clip_renders`, adds a `pending` row
   per other style, copies the clip's span of the source to `$DATA_DIR/variants/<clip_id>/source.mkv`
   (the source file itself may be deleted as soon as the render ends: VOD windows, Twitch clip downloads,
   the live buffer) with the render inputs next to it (`recipe.json`), and queues `clip.variants`;
2. the job `clip.variants` waits until the gate has decided. An approved clip gets each pending style
   rendered from the copy, checked like the gate checks a file (size, length, black and frozen picture in
   the style's own gameplay band) and uploaded to Storage; a rejected clip gets none. The copy is deleted
   at the end.

The posting engine sends each account the render of its style (`render_for_post`), or the reference
render when that one is missing or failed. Content checks (score, music, safety, duplicates) and P5 ran
on the reference render: the source and the cut are the same in every style.
"""

import json
import shutil
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from psycopg.types.json import Jsonb

from sixstars import db, events, jobs
from sixstars import storage as clip_storage
from sixstars.config import env
from sixstars.factory import tools
from sixstars.factory.assets import load_assets
from sixstars.factory.bleep import Span
from sixstars.factory.config import FactoryConfig
from sixstars.factory.director import AccountSlot, DirectorRules, StylePlan, plan_styles
from sixstars.factory.layout import Box, Layout, LayoutA, LayoutB, LayoutKind, gameplay_band
from sixstars.factory.lines import Line, Word
from sixstars.factory.performance import PostViews, StyleStats, post_scores, summarise
from sixstars.factory.window import ClipWindow
from sixstars.logging import get_logger

JOB_VARIANTS = "clip.variants"
SOURCE_NAME = "source.mkv"
RECIPE_NAME = "recipe.json"
# The job waits for the gate this often, at most this many times (the YouTube pre-check can take an hour).
WAIT_S = 60
MAX_WAIT_ROUNDS = 180
# The source copy: near-lossless and fast; deleted when the variants are done.
COPY_PRESET = "ultrafast"
COPY_CRF = 12
COPY_TIMEOUT_S = 600.0
# Clip statuses in which the gate has not decided yet, and in which the variants are wanted.
UNDECIDED = ("rendering", "gating", "precheck_pending")
WANTED = ("approved", "posted")


def variants_dir(clip_id: str) -> Path:
    return env().DATA_DIR / "variants" / clip_id


def variant_path(clip_id: str, style_id: str) -> Path:
    """`$DATA_DIR/ready/<clip_id>.<style_id>.mp4`, next to the reference render."""
    return env().DATA_DIR / "ready" / f"{clip_id}.{style_id}.mp4"


def variant_key(clip_key: str, style_id: str) -> str:
    """The storage key of a variant: the clip's key with the style before the extension. Pure."""
    stem = clip_key.removesuffix(".mp4")
    return f"{stem}.{style_id}.mp4"


# ── Planning ─────────────────────────────────────────────────


def load_accounts() -> list[AccountSlot]:
    rows = db.fetch_all("select id, platform, lanes from platform_accounts where enabled order by id")
    return [AccountSlot(str(row["id"]), str(row["platform"]), tuple(row["lanes"] or ())) for row in rows]


def recent_styles(account_ids: Sequence[str], depth: int) -> dict[str, list[str]]:
    """The styles of the latest posts of each account, newest first (at most `depth` each)."""
    if not account_ids or depth <= 0:
        return {}
    rows = db.fetch_all(
        """
        select p.account_id, coalesce(r.style_id, c.style_id) as style_id
        from posts p
        join clips c on c.id = p.clip_id
        left join clip_renders r on r.id = p.render_id
        where p.account_id = any(%s) and p.status in ('pending', 'published')
          and p.created_at > now() - interval '7 days'
        order by p.created_at desc
        limit %s
        """,
        (list(account_ids), depth * len(account_ids) * 4),
    )
    out: dict[str, list[str]] = {}
    for row in rows:
        styles = out.setdefault(str(row["account_id"]), [])
        if row["style_id"] and len(styles) < depth:
            styles.append(str(row["style_id"]))
    return out


def plan_for(
    clip_id: str,
    lane: str,
    cfg: FactoryConfig,
    *,
    facts: frozenset[str] = frozenset(),
    moment_type: str | None = None,
) -> StylePlan:
    """The director's plan for a clip, from the enabled accounts, their latest styles and what the clip
    offers (`director.clip_facts`)."""
    accounts = load_accounts()
    ids = [account.id for account in accounts]
    recent = recent_styles(ids, cfg.director.no_repeat_n)
    stats = style_stats(ids, cfg.director) if cfg.director.mode == "bandit" else None
    return plan_styles(
        clip_id,
        lane,
        accounts,
        cfg.catalog,
        cfg.director,
        recent,
        facts=facts,
        moment_type=moment_type,
        stats=stats,
    )


def scored_posts(account_ids: Sequence[str], rules: DirectorRules) -> list[PostViews]:
    """(account, style, views) of the published, not dry-run posts between `min_age_h` and `window_d` old
    (development-plan/M18 Phase D)."""
    if not account_ids:
        return []
    rows = db.fetch_all(
        """
        select p.account_id, coalesce(r.style_id, c.style_id) as style_id, p.views
        from posts p
        join clips c on c.id = p.clip_id
        left join clip_renders r on r.id = p.render_id
        where p.account_id = any(%s) and p.status = 'published' and not p.dry_run and p.views is not null
          and p.published_at <= now() - make_interval(secs => %s)
          and p.published_at >= now() - make_interval(secs => %s)
        """,
        (list(account_ids), rules.min_age_h * 3600, rules.window_d * 86400),
    )
    return [(str(row["account_id"]), row["style_id"], int(row["views"])) for row in rows]


def style_stats(account_ids: Sequence[str], rules: DirectorRules) -> dict[str, dict[str, StyleStats]]:
    """account -> style -> `StyleStats` from the posts' views (`performance`)."""
    return summarise(post_scores(scored_posts(account_ids, rules)))


# ── The recipe: what a variant render needs besides the style ─


def _box_json(box: Box | None) -> dict[str, int] | None:
    return None if box is None else {"x": box.x, "y": box.y, "w": box.w, "h": box.h}


def _box(value: object) -> Box | None:
    if not isinstance(value, Mapping):
        return None
    return Box(int(value["x"]), int(value["y"]), int(value["w"]), int(value["h"]))


def recipe_json(
    *,
    lines: Sequence[Line],
    layout: Layout,
    duration_s: float,
    series_no: int,
    mode: str,
    lane: str,
    creator: str,
    hook: str | None,
    keywords: Mapping[int, Sequence[str]],
    duck: Sequence[Span],
    payoff_s: float | None,
    payoff_emoji: str | None,
    texts: Mapping[str, str] | None = None,
    window_start_s: float = 0.0,
    broll: bool = False,
) -> dict[str, Any]:
    """The inputs of a render as JSON (times in clip time, boxes in source pixels). Pure."""
    return {
        "duration_s": duration_s,
        "series_no": series_no,
        "mode": mode,
        "lane": lane,
        "creator": creator,
        "hook": hook,
        "payoff_s": payoff_s,
        "payoff_emoji": payoff_emoji,
        "texts": dict(texts or {}),
        # Where the clip starts in the source copy, and whether the copy holds the footage around it for a
        # montage's B-roll (Phase E).
        "window_start_s": window_start_s,
        "broll": broll,
        "layout": None
        if not isinstance(layout, LayoutA)
        else {"facecam_box": _box_json(layout.facecam_box), "gameplay_box": _box_json(layout.gameplay_box)},
        "lines": [
            {"index": line.index, "words": [[word.text, word.start_s, word.end_s] for word in line.words]}
            for line in lines
        ],
        "keywords": {str(index): list(words) for index, words in keywords.items()},
        "duck": [[span.start_s, span.end_s] for span in duck],
    }


def layout_from_recipe(recipe: Mapping[str, Any]) -> Layout:
    raw = recipe.get("layout")
    if not isinstance(raw, Mapping):
        return LayoutB()
    facecam = _box(raw.get("facecam_box"))
    if facecam is None:
        return LayoutB()
    return LayoutA(facecam_box=facecam, gameplay_box=_box(raw.get("gameplay_box")))


def lines_from_recipe(recipe: Mapping[str, Any]) -> tuple[Line, ...]:
    return tuple(
        Line(
            index=int(line["index"]),
            words=tuple(Word(str(text), float(start), float(end)) for text, start, end in line["words"]),
        )
        for line in recipe.get("lines", [])
    )


# ── After the reference render ───────────────────────────────


def copy_command(
    source: Path, window: ClipWindow, target: Path, *, has_audio: bool, ffmpeg: str
) -> list[str]:
    """ffmpeg arguments that copy the clip's span of the source, re-encoded near-lossless so the cut is
    frame-exact (a stream copy cuts on keyframes). Pure."""
    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-ss", f"{window.start_s:.3f}"]
    command += ["-t", f"{window.duration_s:.3f}", "-i", str(source), "-map", "0:v:0"]
    command += ["-c:v", "libx264", "-preset", COPY_PRESET, "-crf", str(COPY_CRF)]
    command += ["-map", "0:a:0", "-c:a", "flac"] if has_audio else ["-an"]
    return [*command, str(target)]


def _upsert_render(clip_id: str, style_id: str, **columns: Any) -> None:
    names = ["clip_id", "style_id", *columns]
    values = [clip_id, style_id, *columns.values()]
    updates = ", ".join(f"{name} = excluded.{name}" for name in columns) or "style_id = excluded.style_id"
    db.execute(
        f"insert into clip_renders ({', '.join(names)}) values ({', '.join(['%s'] * len(names))}) "  # noqa: S608
        f"on conflict (clip_id, style_id) do update set {updates}",
        values,
    )


def after_reference(
    clip_id: str,
    plan: StylePlan,
    *,
    layout_kind: LayoutKind,
    output_path: Path,
    file_size_bytes: int,
    duration_s: float,
    render_ms: int,
    source: Path,
    window: ClipWindow,
    has_audio: bool,
    recipe: Mapping[str, Any],
    ffmpeg: str = "ffmpeg",
) -> bool:
    """Record the reference render and prepare the other styles of the plan. Never raises: a variant that
    cannot be prepared is marked failed and its accounts get the reference render. Returns True when a
    `clip.variants` job was queued."""
    log = get_logger("factory")
    db.execute(
        "update clips set style_id = %s, layout_kind = %s, style_plan = %s where id = %s",
        (plan.reference_style, layout_kind, Jsonb(plan.as_json()), clip_id),
    )
    _upsert_render(
        clip_id,
        plan.reference_style,
        layout_kind=layout_kind,
        is_reference=True,
        status="ready",
        local_path=str(output_path),
        file_size_bytes=file_size_bytes,
        duration_s=round(duration_s, 3),
        render_ms=render_ms,
    )
    others = [style for style in plan.styles if style != plan.reference_style]
    if not others:
        return False
    for style in others:
        _upsert_render(clip_id, style, status="pending")
    folder = variants_dir(clip_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tools.run(
            copy_command(source, window, folder / SOURCE_NAME, has_audio=has_audio, ffmpeg=ffmpeg),
            timeout_s=COPY_TIMEOUT_S,
            what="variant source copy",
        )
        (folder / RECIPE_NAME).write_text(json.dumps(recipe), encoding="utf-8")
    except (OSError, tools.FfmpegError) as exc:
        log.warning("variant_source_failed", clip_id=clip_id, error=str(exc)[:300])
        _fail_pending(clip_id, f"source copy failed: {exc}"[:300])
        shutil.rmtree(folder, ignore_errors=True)
        return False
    jobs.enqueue(
        "normal",
        JOB_VARIANTS,
        {"clip_id": clip_id, "round": 0},
        run_after=datetime.now(UTC) + timedelta(seconds=WAIT_S),
        dedupe_key=f"{JOB_VARIANTS}:{clip_id}",
    )
    events.emit(
        "clip.variants_queued",
        entity_type="clip",
        entity_id=clip_id,
        data={"reference_style": plan.reference_style, "styles": others, "plan": plan.as_json()},
    )
    return True


def _fail_pending(clip_id: str, error: str, status: str = "failed") -> None:
    db.execute(
        "update clip_renders set status = %s, error = %s where clip_id = %s and status = 'pending'",
        (status, error, clip_id),
    )


# ── The job ──────────────────────────────────────────────────


def file_problems(
    facts: Any,
    *,
    width: int,
    height: int,
    expected_s: float,
    tolerance_s: float,
    max_black_s: float,
    max_freeze_s: float,
) -> list[str]:
    """Why a variant file must not be posted (the gate's technical checks), empty when it may. Pure."""
    if facts is None:
        return ["file missing"]
    problems: list[str] = []
    if (facts.width, facts.height) != (width, height):
        problems.append(f"frame is {facts.width}x{facts.height}")
    if not facts.has_video or not facts.has_audio:
        problems.append("a video or audio stream is missing")
    if facts.duration_s is None or abs(facts.duration_s - expected_s) > tolerance_s:
        problems.append(f"length {facts.duration_s} s, expected {expected_s:.2f} s")
    if facts.longest_black_s is None or facts.longest_black_s >= max_black_s:
        problems.append(f"black picture for {facts.longest_black_s} s")
    if facts.longest_freeze_s is None or facts.longest_freeze_s >= max_freeze_s:
        problems.append(f"frozen picture for {facts.longest_freeze_s} s")
    return problems


def _requeue(clip_id: str, round_no: int) -> None:
    jobs.enqueue(
        "normal",
        JOB_VARIANTS,
        {"clip_id": clip_id, "round": round_no},
        run_after=datetime.now(UTC) + timedelta(seconds=WAIT_S),
        dedupe_key=f"{JOB_VARIANTS}:{clip_id}:{round_no}",
    )


def _render_one(
    clip_id: str,
    style_id: str,
    recipe: Mapping[str, Any],
    cfg: FactoryConfig,
    clip_key: str | None,
    storage: clip_storage.ClipStorage,
) -> None:
    """Render, check, upload and record one style. Raises nothing the caller must handle: the row says
    what happened."""
    # the pipeline imports this module
    from sixstars.factory.pipeline import RenderJob, framed_layout, render_file
    from sixstars.gate import profiles
    from sixstars.gate import visual as visuals

    log = get_logger("factory")
    folder = variants_dir(clip_id)
    source = folder / SOURCE_NAME
    entry = cfg.catalog.get(style_id)
    if entry is None:
        _upsert_render(clip_id, style_id, status="failed", error="style no longer in factory.styles")
        return
    scratch = folder / f"scratch-{style_id}"
    output = variant_path(clip_id, style_id)
    try:
        media = tools.probe(source)
        duration = float(recipe["duration_s"])
        offset = float(recipe.get("window_start_s") or 0.0)
        assets = load_assets(cfg.params.font, caption_font=entry.rules.caption_font)
        keywords = {int(key): tuple(words) for key, words in dict(recipe.get("keywords", {})).items()}
        job = RenderJob(
            source_path=source,
            media=media,
            window=ClipWindow(offset, min(offset + duration, media.duration_s)),
            lines=lines_from_recipe(recipe),
            layout=layout_from_recipe(recipe),
            params=cfg.params,
            assets=assets,
            output_path=output,
            scratch_dir=scratch,
            series_no=int(recipe.get("series_no", 0)),
            mode="express" if recipe.get("mode") == "express" else "normal",
            lane=str(recipe.get("lane", "hourly")),
            creator=str(recipe.get("creator", "")),
            hook=recipe.get("hook"),
            keywords=keywords,
            duck=tuple(Span(float(s), float(e)) for s, e in recipe.get("duck", [])),
            style=entry.rules,
            payoff_s=recipe.get("payoff_s"),
            payoff_emoji=recipe.get("payoff_emoji"),
            style_id=style_id,
            texts={str(k): str(v) for k, v in dict(recipe.get("texts") or {}).items() if v},
            broll_from=ClipWindow(0.0, media.duration_s) if recipe.get("broll") else None,
        )
        outcome = render_file(job)
        kind = framed_layout(job.layout, entry.rules).kind
        settings = profiles.load()
        facts = visuals.measure_file(output, outcome.clip_duration_s, settings, layout_kind=kind)
        problems = file_problems(
            facts,
            width=settings.width,
            height=settings.height,
            expected_s=outcome.media.duration_s,
            tolerance_s=settings.duration_tolerance_s,
            max_black_s=settings.max_black_s,
            max_freeze_s=settings.max_freeze_s,
        )
        checks = {
            "band": list(gameplay_band(kind)),
            "longest_black_s": None if facts is None else facts.longest_black_s,
            "longest_freeze_s": None if facts is None else facts.longest_freeze_s,
            "problems": problems,
        }
        storage_path = None
        if not problems and clip_key is not None and storage.is_configured():
            storage_path = storage.upload(output, variant_key(clip_key, style_id))
        _upsert_render(
            clip_id,
            style_id,
            layout_kind=kind,
            status="failed" if problems else "ready",
            local_path=str(output),
            storage_path=storage_path,
            file_size_bytes=outcome.file_size_bytes,
            duration_s=round(outcome.clip_duration_s, 3),
            render_ms=outcome.render_ms,
            checks=Jsonb(checks),
            error="; ".join(problems) or None,
        )
        log.info("variant_rendered", style_id=style_id, problems=problems, render_ms=outcome.render_ms)
    except Exception as exc:  # noqa: BLE001 - one failed style must not stop the others
        log.error("variant_failed", style_id=style_id, error=f"{type(exc).__name__}: {exc}"[:300])
        _upsert_render(clip_id, style_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_variants(clip_id: str, round_no: int = 0, *, storage: clip_storage.ClipStorage | None = None) -> str:
    """Render the pending styles of a clip once the gate approved it. Returns what happened."""
    log = get_logger("factory")
    row = db.fetch_one("select status, lane, storage_path, created_at from clips where id = %s", (clip_id,))
    folder = variants_dir(clip_id)
    if row is None:
        shutil.rmtree(folder, ignore_errors=True)
        return "no_clip"
    status = str(row["status"])
    if status in UNDECIDED:
        if round_no < MAX_WAIT_ROUNDS:
            _requeue(clip_id, round_no + 1)
            return "waiting"
        _fail_pending(clip_id, "the gate did not decide in time", status="skipped")
        shutil.rmtree(folder, ignore_errors=True)
        return "gave_up"
    if status not in WANTED:
        _fail_pending(clip_id, f"clip is {status}", status="skipped")
        shutil.rmtree(folder, ignore_errors=True)
        return "skipped"
    recipe_file = folder / RECIPE_NAME
    if not recipe_file.is_file() or not (folder / SOURCE_NAME).is_file():
        _fail_pending(clip_id, "the source copy is gone")
        return "no_source"
    recipe = json.loads(recipe_file.read_text(encoding="utf-8"))
    cfg = FactoryConfig.load(str(row["lane"]))
    store = storage or clip_storage.ClipStorage()
    clip_key = clip_storage.clip_key(clip_id, row["created_at"])
    pending = [
        str(item["style_id"])
        for item in db.fetch_all(
            "select style_id from clip_renders where clip_id = %s and status = 'pending' order by style_id",
            (clip_id,),
        )
    ]
    started = time.monotonic()
    for style_id in pending:
        _render_one(clip_id, style_id, recipe, cfg, clip_key, store)
    shutil.rmtree(folder, ignore_errors=True)
    done = db.fetch_all("select style_id, status, error from clip_renders where clip_id = %s", (clip_id,))
    events.emit(
        "clip.variants_done",
        entity_type="clip",
        entity_id=clip_id,
        data={
            "renders": {str(r["style_id"]): {"status": r["status"], "error": r["error"]} for r in done},
            "seconds": round(time.monotonic() - started, 1),
        },
    )
    log.info("variants_done", rendered=len(pending))
    return "done"


def handle_variants(job: jobs.Job) -> None:
    """Job `clip.variants` (queue `normal`): `{"clip_id": ..., "round": n}`."""
    clip_id = str(job.payload["clip_id"])
    round_value = job.payload.get("round", 0)
    round_no = round_value if isinstance(round_value, int) else 0
    with structlog.contextvars.bound_contextvars(clip_id=clip_id, job_id=job.id):
        run_variants(clip_id, round_no)


# ── Posting ──────────────────────────────────────────────────


def render_for_post(clip_id: str, account_id: str) -> tuple[str | None, str | None]:
    """`(clip_renders.id, local file)` for posting `clip_id` on `account_id`: the ready render of the
    account's planned style when its file exists, else the reference render. `(None, None)` for a clip
    without any `clip_renders` row (rendered before migration 0027): the caller uses `clips.local_path`."""
    rows = db.fetch_all(
        """
        select r.id::text as id, r.local_path, r.is_reference, r.status,
               r.style_id = (c.style_plan -> 'by_account' ->> %s) as planned
        from clips c join clip_renders r on r.clip_id = c.id
        where c.id = %s
        """,
        (account_id, clip_id),
    )
    for row in rows:
        if (
            row["planned"]
            and row["status"] == "ready"
            and row["local_path"]
            and Path(row["local_path"]).is_file()
        ):
            return row["id"], row["local_path"]
    reference = next((row for row in rows if row["is_reference"]), None)
    if reference is not None:
        return reference["id"], reference["local_path"]
    return None, None


def register_handlers() -> None:
    """Register `clip.variants` with the worker (safe to call more than once)."""
    from sixstars.services import worker

    if worker.HANDLERS.get(JOB_VARIANTS) is not handle_variants:
        worker.register(JOB_VARIANTS, handle_variants)


__all__ = [
    "JOB_VARIANTS",
    "after_reference",
    "file_problems",
    "handle_variants",
    "plan_for",
    "recipe_json",
    "register_handlers",
    "render_for_post",
    "run_variants",
    "variant_key",
]

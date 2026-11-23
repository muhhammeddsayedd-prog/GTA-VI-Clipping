"""The cover engine's build step (docs/29a section 7, stages 3-5 per account): after the frames are mined,
make one cover per platform account the clip may post to, check it, and store it for delivery.

For each account (enabled, the clip's lane allowed, platform in `cover.platforms`):

- `frame_only` lanes (campaigns until the owner approves `full`): the plain best frame, no line, no mark;
  it must pass the rule checks except OCR.
- `full` lanes: the cover line (one P12 call per clip, `hookline`), then up to 3 frames (the mined top
  frames in order) composed with the arm's layout and line style and checked (rules, then the vision
  check); the first that passes wins. Then the plain best frame (rules except OCR). Then nothing: the
  platform's default (`fallback_default`).

Every attempt is a `covers` row with its scores and the config version. A passing cover goes to
`covers-work/<clip_id>/` and, for Instagram, to `covers-public` (its `cover_url`). Nothing here can stop a
post: delivery only reads what is stored (`covers.deliver`).
"""

import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sixstars import db, events
from sixstars import storage as clip_storage
from sixstars.covers import compose, hookline, learn, miner, qa, store
from sixstars.covers.config import CoverConfig, lane_key
from sixstars.logging import get_logger

MAX_ATTEMPTS = 3
PLAIN_IGNORE = ("ocr", "text_outside_safe_band", "too_many_words")

_CLIP_SQL = """
select c.id::text as id, c.lane, c.title, c.hook, c.transcript,
       coalesce(cc.slug, '') as campaign_slug
from clips c
left join clip_campaigns cc on cc.id = (to_jsonb(c) ->> 'campaign_id')::uuid
where c.id = %s
"""

_ACCOUNTS_SQL = """
select id, platform from platform_accounts
where enabled and %s = any(lanes)
order by id
"""


@dataclass(frozen=True, slots=True)
class Tools:
    """The warm models of one job."""

    renderer: Any
    landmarker: Any
    ocr_engine: Any

    def close(self) -> None:
        self.renderer.close()
        self.landmarker.close()


def open_tools() -> Tools:
    from rapidocr import RapidOCR

    from sixstars.covers import scorer

    landmarker = scorer.Landmarker(miner.landmarker_model())
    return Tools(compose.Renderer(), landmarker, RapidOCR())


def _frames(
    folder: Path, mine: Mapping[str, Any]
) -> list[tuple[Path, Path, tuple[int, int, int, int] | None, int]]:
    """The mined top frames: (face file, game file, face box, clean-layer time in ms), best first."""
    out = []
    for rank, frame in enumerate(mine.get("top") or [], start=1):
        t_ms = int(frame["t_ms"])
        face = folder / "top" / f"{rank}_face_{t_ms}.jpg"
        game = folder / "top" / f"{rank}_game_{t_ms}.jpg"
        if face.is_file() and game.is_file():
            box = frame.get("face_box")
            out.append((face, game, tuple(box) if isinstance(box, list) and len(box) == 4 else None, t_ms))
    return out


def _store_files(
    cover_id: str, clip_id: str, result: compose.ComposedCover, platform: str
) -> tuple[str, str | None]:
    """Upload the cover (and its 3:4 preview) to covers-work; Instagram's also to covers-public."""
    storage = clip_storage.ClipStorage()
    if not storage.is_configured():
        return str(result.jpeg), None
    key = storage.upload(
        result.jpeg, clip_storage.cover_work_key(clip_id, f"{cover_id}.jpg"), content_type="image/jpeg"
    )
    storage.upload(
        result.preview, clip_storage.cover_work_key(clip_id, f"{cover_id}_3x4.jpg"), content_type="image/jpeg"
    )
    public = None
    if platform == "instagram":
        public_key = storage.upload(
            result.jpeg, clip_storage.cover_public_key(cover_id), content_type="image/jpeg"
        )
        public = storage.public_url(public_key)
    return key, public


def _attempt(
    tools: Tools,
    *,
    clip: Mapping[str, Any],
    account_id: str,
    platform: str,
    lane: str,
    mode: str,
    config: CoverConfig,
    frame: tuple[Path, Path, tuple[int, int, int, int] | None, int],
    layout: compose.CoverLayout,
    line: hookline.Candidate | None,
    attempt: int,
    rendered_ts_ms: int | None,
    work: Path,
    recent: Sequence[store.RecentCover],
    transcript: str,
    control: bool = False,
) -> tuple[str, bool]:
    """Compose and check one cover; store its row. Returns (cover id, passed)."""
    face, game, box, t_ms = frame
    plain = line is None
    spec = compose.CoverSpec(
        layout=layout,
        face_path=face,
        game_path=game,
        face_box=box,
        lines=line.lines if line is not None else (),
        key_word=line.key_word if line is not None else "",
        brand_mark=config.brand_mark_for(lane) and not plain,
        lift=(config.exposure["lift_below"], config.exposure["lift_target"], config.exposure["gamma_max"]),
    )
    name = f"{account_id}_{attempt}"
    result = compose.compose(spec, work, name, render=tools.renderer)
    facts = qa.measure(
        result.jpeg,
        result.preview,
        layout=result.layout,
        line=line.text if line is not None else "",
        line_style=line.style if line is not None else "none",
        text_rect=result.extras.get("text_rect"),
        parts_dir=Path(str(result.extras["parts_dir"])),
        engine=tools.ocr_engine,
        landmarker=tools.landmarker,
        placed_face=result.extras.get("face_rect"),
    )
    hashes = [int(r.phash, 16) for r in recent if r.phash]
    ex = config.exposure
    limits = (ex["luma_min"], ex["luma_max"], ex["clipped_max"])
    arms = [(r.layout, r.line_style) for r in recent]
    if plain:
        problems = [
            p
            for p in qa.rule_problems(facts, recent_hashes=hashes, recent_arms=arms, exposure_limits=limits)
            if p.split(":")[0] not in PLAIN_IGNORE
        ]
        verdict = qa.QaResult(
            tuple(problems), {"total": 10, "hard_fail": False, "skipped": "plain"}, config.qa_pass_score
        )
    else:
        verdict = qa.review(
            facts,
            title=str(clip.get("title") or clip.get("hook") or ""),
            transcript=transcript,
            pass_score=config.qa_pass_score,
            recent_hashes=hashes,
            recent_arms=arms,
            exposure_limits=limits,
            clip_id=str(clip["id"]),
        )
    layout_name = "plain" if plain else result.layout
    status: store.CoverStatus = (
        ("fallback_plain" if plain and mode == "full" and not control else "passed")
        if verdict.passed
        else "failed"
    )
    arm = learn.arm_name(layout_name, line.style if line is not None else "none")
    cover_id = store.insert(
        store.NewCover(
            clip_id=str(clip["id"]),
            lane=lane,
            platform=platform,
            arm=arm,
            config_version=config.version,
            account_id=account_id,
            mode="frame_only" if mode == "frame_only" else "full",
            layout=layout_name,
            line_text=line.text if line is not None else None,
            line_style=line.style if line is not None else "none",
            key_word=line.key_word if line is not None else None,
            frame_ts_ms=t_ms,
            rendered_ts_ms=rendered_ts_ms,
            scores={
                "qa": verdict.as_json(),
                "face_px": facts.face_h_px,
                "face_source": facts.face_source,
                "luma": facts.luma,
                "clipped": facts.clipped,
                "font_size": result.font_size,
                "render_ms": result.render_ms,
            },
            phash=f"{facts.phash:016x}" if facts.phash is not None else None,
            status=status,
            attempt=attempt,
            error=", ".join(verdict.rule_problems)[:500] or None,
        )
    )
    if verdict.passed:
        storage_path, public_url = _store_files(cover_id, str(clip["id"]), result, platform)
        db.execute(
            "update covers set storage_path = %s, public_url = %s where id = %s",
            (storage_path, public_url, cover_id),
        )
    return cover_id, verdict.passed


def build(clip_id: str, *, rng: random.Random | None = None) -> dict[str, str]:
    """Make and check the covers of one mined clip. Returns {account id: final status}."""
    log = get_logger("covers")
    config = CoverConfig.load()
    if not config.enabled:
        return {}
    folder = miner.cover_dir(clip_id)
    mine_path = folder / miner.MINE_NAME
    clip = db.fetch_one(_CLIP_SQL, (clip_id,))
    if clip is None or not mine_path.is_file():
        return {}
    mine = json.loads(mine_path.read_text(encoding="utf-8"))
    frames = _frames(folder, mine)
    lane = lane_key(str(clip["lane"]), str(clip["campaign_slug"]) or None)
    mode = config.mode_for(lane)
    accounts = [
        (str(a["id"]), str(a["platform"]))
        for a in db.fetch_all(_ACCOUNTS_SQL, (str(clip["lane"]),))
        if config.runs_for(str(a["platform"]), lane)
    ]
    if mode == "off" or not accounts or not frames:
        return {}
    line_result: hookline.LineResult | None = None
    transcript = ""
    if mode == "full":
        item = hookline.load_input(clip_id, [a for a, _p in accounts])
        if item is not None:
            transcript = " ".join(hookline.transcript_window(item.transcript, item.peak_s))
            try:
                line_result = hookline.write_line(item, profanity=hookline.profanity_terms())
            except Exception as exc:  # noqa: BLE001 - no line means a plain cover, never a failed post
                log.warning("cover_line_failed", clip_id=clip_id, error=f"{type(exc).__name__}: {exc}"[:300])
    proposed: compose.CoverLayout = mine.get("layout") or "split"
    rendered_ts = mine.get("cover_timestamp_ms")
    work = folder / "composed"
    work.mkdir(parents=True, exist_ok=True)
    outcome: dict[str, str] = {}
    tools = open_tools()
    try:
        for account_id, platform in accounts:
            recent = store.recent_for_account(account_id, qa.RECENT_COVERS)
            final = "fallback_default"
            attempt = 0
            candidates = list(line_result.valid) if line_result is not None else []
            if mode == "full" and candidates:
                try:
                    stats = learn.load_stats(platform)
                except Exception:  # noqa: BLE001 - no stats means the scorer's proposal
                    stats = {}
                layout, line = learn.choose(
                    platform,
                    proposed,
                    candidates,
                    recent,
                    exploration=config.exploration,
                    rng=rng,
                    stats=stats,
                )
                for frame in frames[:MAX_ATTEMPTS]:
                    attempt += 1
                    _cover, passed = _attempt(
                        tools,
                        clip=clip,
                        account_id=account_id,
                        platform=platform,
                        lane=lane,
                        mode=mode,
                        config=config,
                        frame=frame,
                        layout=layout,
                        line=line,
                        attempt=attempt,
                        rendered_ts_ms=rendered_ts,
                        work=work,
                        recent=recent,
                        transcript=transcript,
                        control=line is None,
                    )
                    if passed:
                        final = "passed"
                        break
            if final != "passed":
                attempt += 1
                _cover, passed = _attempt(
                    tools,
                    clip=clip,
                    account_id=account_id,
                    platform=platform,
                    lane=lane,
                    mode=mode,
                    config=config,
                    frame=frames[0],
                    layout="split",
                    line=None,
                    attempt=attempt,
                    rendered_ts_ms=rendered_ts,
                    work=work,
                    recent=recent,
                    transcript=transcript,
                )
                if passed:
                    final = "fallback_plain" if mode == "full" else "passed"
                else:
                    store.insert(
                        store.NewCover(
                            clip_id=clip_id,
                            lane=lane,
                            platform=platform,
                            arm="default",
                            config_version=config.version,
                            account_id=account_id,
                            mode="frame_only" if mode == "frame_only" else "full",
                            layout=None,
                            line_style="none",
                            rendered_ts_ms=rendered_ts,
                            status="fallback_default",
                            attempt=attempt + 1,
                        )
                    )
            outcome[account_id] = final
    finally:
        tools.close()
    events.emit(
        "cover.built",
        entity_type="clip",
        entity_id=clip_id,
        data={"lane": lane, "mode": mode, "accounts": outcome},
    )
    log.info("cover_built", clip_id=clip_id, lane=lane, mode=mode, accounts=outcome)
    return outcome

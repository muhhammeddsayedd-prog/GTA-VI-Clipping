"""Live checks of the AI layer: every prompt of docs/15 runs once against the real models on a
realistic input (M05 acceptance tests). Skipped unless `RUN_LIVE_TESTS=1`.

    RUN_LIVE_TESTS=1 uv run pytest tests/live/test_ai_prompts.py -v -s

Optional environment:
- `AI_LIVE_DATABASE_URL`: a throwaway Postgres with the migrations applied (the integration test
  database). When set, settings are read from it and every call writes its `api_usage` row there, so
  the spend of the run can be read back. Without it the settings below are used and nothing is stored.
- `AI_LIVE_FRAMES_DIR`: a directory with JPEG frames of a real GTA 5 stream that has a facecam
  (`src_*.jpg`, extracted with ffmpeg) and optionally `chunks.json` (transcript chunks for P3). The
  vision tests are skipped without it. Media is never committed.
- `AI_LIVE_OUT_DIR`: where the replies (`ai_live_results.json`) and `layout_check.jpg` are written;
  defaults to the frames directory, else the pytest temp directory.

The text inputs are in `tests/fixtures/ai/`: `gta5_window_transcript.json` is the real caption track of
65 seconds of a public GTA 5 RP stream; the chat samples, the gambling and sponsored segments, the
channel descriptions and the emails in `live_cases.json` are written by hand.
"""

import io
import json
import os
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from sixstars import config, db, settings
from sixstars.ai import calls, costs
from sixstars.ai.calls import ChatLine, ClipContext, Segment, SpikeContext, TranscriptChunk
from sixstars.ai.schemas import ClipWriting, count_words
from sixstars.factory.lines import Line, group_lines, words_from_transcript

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ai"
CASES: dict[str, Any] = json.loads((FIXTURES / "live_cases.json").read_text(encoding="utf-8"))
WINDOW: dict[str, Any] = json.loads((FIXTURES / "gta5_window_transcript.json").read_text(encoding="utf-8"))
STAGING_GAME = "Grand Theft Auto V"
FALLBACK_SETTINGS: dict[str, Any] = {
    "ai.model_writer": "claude-sonnet-5-5",
    "ai.model_fast": "claude-haiku-5-5",
    "ai.daily_budget_usd": 25,
    "factory.watermark": "@6starsclipping",
    "gate.spoiler_block_until": "2026-12-03T00:00:00Z",
    "gate.reject_topics": [
        "real-world politics",
        "gambling",
        "sponsored segment",
        "real-world violence",
        "sexual content",
        "personal information",
    ],
}
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
PEAK_S = 27.0  # the robbery starts about 27 s into the window file
RESULTS: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def ai_live(live_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Staging settings (GTA 5). With `AI_LIVE_DATABASE_URL` the rest comes from that database and
    usage rows are written to it."""
    url = os.environ.get("AI_LIVE_DATABASE_URL", "")
    if any(part in url for part in ("supabase.co", "supabase.com", "supabase.net")):
        pytest.fail("AI_LIVE_DATABASE_URL must be a throwaway database, not Supabase")
    stored: dict[str, Any] = {}
    if url:
        monkeypatch.setenv("DATABASE_URL", url)
        config.env.cache_clear()
        db.close_pool()
        stored = {row["key"]: row["value"] for row in db.fetch_all("select key, value from settings")}
    values = {**FALLBACK_SETTINGS, **stored, "env.target_game": STAGING_GAME}
    values["factory.watermark"] = "@6starsclipping"
    monkeypatch.setattr(settings, "_all", lambda: values)
    if not url:
        monkeypatch.setattr(calls.anthropic_client.usage, "spent_today", lambda provider: 0.0)
    yield
    db.close_pool()
    config.env.cache_clear()


@pytest.fixture(scope="session")
def out_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    chosen = os.environ.get("AI_LIVE_OUT_DIR") or os.environ.get("AI_LIVE_FRAMES_DIR")
    path = Path(chosen) if chosen else tmp_path_factory.mktemp("ai_live")
    path.mkdir(parents=True, exist_ok=True)
    yield path
    (path / "ai_live_results.json").write_text(
        json.dumps(RESULTS, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


@pytest.fixture(scope="session")
def frames_dir() -> Path:
    chosen = os.environ.get("AI_LIVE_FRAMES_DIR", "")
    if not chosen or not list(Path(chosen).glob("src_*.jpg")):
        pytest.skip("AI_LIVE_FRAMES_DIR has no src_*.jpg frames: vision checks need real stream frames")
    return Path(chosen)


def keep(name: str, value: Any) -> None:
    RESULTS[name] = value.model_dump() if hasattr(value, "model_dump") else value
    print(f"\n{name}: {json.dumps(RESULTS[name], ensure_ascii=False, default=str)}")


def window_lines() -> list[Line]:
    return group_lines(words_from_transcript(WINDOW))


def chat(rows: list[list[Any]], offset_s: float = 0.0) -> list[ChatLine]:
    return [ChatLine(float(t) + offset_s, str(text)) for t, text in rows]


def invented_lines(texts: list[str], step_s: float = 3.0) -> list[Line]:
    """Subtitle lines for a hand-written segment: one sentence every `step_s` seconds."""
    words: list[dict[str, Any]] = []
    for index, sentence in enumerate(texts):
        tokens = sentence.split()
        for position, token in enumerate(tokens):
            start = index * step_s + position * (step_s * 0.8 / len(tokens))
            words.append({"w": token, "s": round(start, 2), "e": round(start + 0.2, 2)})
    return group_lines(words_from_transcript({"language": "en", "words": words}))


def clip_context(lines: list[Line], window_s: float, chat_rows: list[ChatLine], **extra: Any) -> ClipContext:
    values: dict[str, Any] = {
        "creator": "speedyrp",
        "platform": "twitch",
        "stream_title": "GTA 5 RP | new in the city",
        "lines": lines,
        "window_s": window_s,
        "proposed_start_s": 0.0,
        "proposed_end_s": window_s,
        "spike_floor_s": window_s / 2,
        "min_len_s": 20.0,
        "max_len_s": 60.0,
        "chat": chat_rows,
        "now": NOW,
    }
    values.update(extra)
    return ClipContext(**values)


def assert_post_copy(writing: ClipWriting) -> None:
    """The limits of docs/15, checked again on the live reply."""
    assert 1 <= count_words(writing.hook) <= 8
    assert len(writing.title) <= 70 and "#" not in writing.title
    assert len(writing.caption) <= 120
    assert 3 <= len(writing.hashtags) <= 5
    assert all(tag == tag.lower() and " " not in tag and "#" not in tag for tag in writing.hashtags)
    assert 1 <= writing.score <= 10
    assert writing.hook.isascii() or any(ord(char) > 0x2000 for char in writing.hook)  # English plus emoji


# ── P1 ───────────────────────────────────────────────────────


def good_spike(spike_id: str = "live-good") -> SpikeContext:
    return SpikeContext(
        spike_id=spike_id,
        creator="speedyrp",
        platform="twitch",
        stream_title="GTA 5 RP | new in the city",
        score_z=4.8,
        msgs=74,
        uniq=58,
        top_tokens=("LMAO", "CLIP", "NO WAY", "💀"),
        chat=chat(CASES["good_spike_chat"]),
        transcript=" ".join(line.text for line in window_lines()),
    )


def raid_spike() -> SpikeContext:
    return SpikeContext(
        spike_id="live-raid",
        creator="speedyrp",
        platform="twitch",
        stream_title="GTA 5 RP | new in the city",
        score_z=5.2,
        msgs=96,
        uniq=71,
        top_tokens=("RAID", "bigmike", "welcome"),
        chat=chat(CASES["raid_chat"]),
        transcript=CASES["raid_transcript"],
    )


def test_p1_single_spike_is_confirmed(out_dir: Path) -> None:
    verdict = calls.confirm_spikes([good_spike()], lane="express")[0]
    keep("p1_single_good", verdict)
    assert verdict.is_moment
    assert verdict.confidence >= 0.6
    assert verdict.moment_type != "other"


def test_p1_batch_separates_a_moment_from_a_raid(out_dir: Path) -> None:
    good, raid = calls.confirm_spikes([good_spike("live-good-2"), raid_spike()])
    keep("p1_batch_good", good)
    keep("p1_batch_raid", raid)
    assert good.is_moment and good.confidence >= 0.6
    assert not raid.is_moment
    assert raid.noise_reason


# ── P2 ───────────────────────────────────────────────────────


def test_p2_writes_a_real_gta5_moment(out_dir: Path) -> None:
    lines = window_lines()
    ctx = clip_context(
        lines,
        float(WINDOW["window_s"]),
        chat(CASES["good_spike_chat"], PEAK_S),
        proposed_start_s=7.0,
        proposed_end_s=52.0,
        spike_floor_s=PEAK_S,
        profanity_hint=("fuck", "shit", "bitch"),
    )
    writing = calls.write_clip(ctx, lane="express")
    keep("p2_good", writing)
    assert_post_copy(writing)
    assert not writing.reject, writing.reject_reason
    assert not writing.spoiler
    assert writing.safety.flagged() == []
    assert writing.start_s <= PEAK_S and 20.0 <= writing.length_s <= 60.0
    assert writing.score >= 6
    known = {line.index: line.text.lower() for line in lines}
    for line_no, words in writing.keyword_map().items():
        assert line_no in known
        assert all(word.lower().strip(".,!?") in known[line_no] for word in words)


def test_p2_rejects_a_gambling_segment(out_dir: Path) -> None:
    lines = invented_lines(CASES["gambling_lines"])
    ctx = clip_context(lines, 36.0, chat(CASES["gambling_chat"]), stream_title="GTA RP then slots later")
    writing = calls.write_clip(ctx)
    keep("p2_gambling", writing)
    assert_post_copy(writing)
    assert writing.safety.gambling
    assert writing.reject and writing.reject_reason


def test_p2_rejects_a_sponsored_read(out_dir: Path) -> None:
    lines = invented_lines(CASES["sponsored_lines"])
    ctx = clip_context(lines, 32.0, chat(CASES["sponsored_chat"]))
    writing = calls.write_clip(ctx)
    keep("p2_sponsored", writing)
    assert_post_copy(writing)
    assert writing.safety.sponsored
    assert writing.reject and writing.reject_reason


def test_p2_blocks_a_story_spoiler_and_keeps_it_out_of_the_hook(out_dir: Path) -> None:
    lines = invented_lines(CASES["spoiler_lines"])
    ctx = clip_context(
        lines, 28.0, chat(CASES["spoiler_chat"]), stream_title="GTA 5 story finale", min_len_s=15.0
    )
    writing = calls.write_clip(ctx)
    keep("p2_spoiler", writing)
    assert_post_copy(writing)
    assert writing.spoiler
    assert writing.reject and writing.reject_reason
    shown = f"{writing.hook} {writing.title}".lower()
    for word in (
        "michael",
        "trevor",
        "devin",
        "deathwish",
        "survive",
        "option c",
        "choice",
        "choose",
        "ending",
    ):
        assert word not in shown


# ── P3 ───────────────────────────────────────────────────────


def test_p3_picks_moments_from_a_transcript(frames_dir: Path, out_dir: Path) -> None:
    source = frames_dir / "chunks.json"
    if not source.is_file():
        pytest.skip("chunks.json not found in AI_LIVE_FRAMES_DIR")
    raw = json.loads(source.read_text(encoding="utf-8"))["chunks"]
    chunks = [TranscriptChunk(float(c["start_s"]), float(c["end_s"]), str(c["text"])) for c in raw]
    low, high = (int(value) for value in settings.get("vod.fallback_moments", [3, 8]))
    moments = calls.pick_moments(
        chunks, low, high, 20.0, 60.0, title="GTA 5 RP funny moments", creator="speedyrp"
    )
    keep("p3_moments", [moment.model_dump() for moment in moments])
    assert 1 <= len(moments) <= high
    for moment in moments:
        assert chunks[0].start_s <= moment.start_s < moment.end_s <= chunks[-1].end_s
        assert 20.0 <= moment.end_s - moment.start_s <= 60.05
    ordered = sorted(moments, key=lambda item: item.start_s)
    assert all(a.end_s <= b.start_s for a, b in zip(ordered, ordered[1:], strict=False))


# ── P4 ───────────────────────────────────────────────────────


def test_p4_finds_the_facecam(frames_dir: Path, out_dir: Path) -> None:
    path = sorted(frames_dir.glob("src_*.jpg"))[0]
    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        layout = calls.detect_layout(data, width, height, ref_id="live-layout")
        keep("p4_layout", layout)
        check = image.convert("RGB")
    draw = ImageDraw.Draw(check)
    game = layout.gameplay
    draw.rectangle((game.x, game.y, game.x + game.w - 1, game.y + game.h - 1), outline=(0, 255, 0), width=4)
    if layout.facecam is not None:
        cam = layout.facecam
        draw.rectangle((cam.x, cam.y, cam.x + cam.w - 1, cam.y + cam.h - 1), outline=(255, 0, 0), width=4)
    check.save(out_dir / "layout_check.jpg", quality=90)
    assert layout.facecam is not None, "the test frames come from a stream with a facecam"
    cam = layout.facecam
    assert cam.x + cam.w <= width and cam.y + cam.h <= height
    # A webcam overlay is a small part of the picture, and the game is most of it.
    assert 0.01 <= (cam.w * cam.h) / (width * height) <= 0.4
    assert (game.w * game.h) / (width * height) >= 0.5
    assert layout.confidence >= 0.5


# ── P5 ───────────────────────────────────────────────────────


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, size: int, anchor: str = "mm") -> None:
    draw.text(
        xy,
        text,
        font=_font(size),
        fill="white",
        stroke_width=max(2, size // 12),
        stroke_fill="black",
        anchor=anchor,
    )


def rendered_frame(source: Path, subtitle: str, foreign: bool = False) -> bytes:
    """A 1080x1920 frame laid out like our render: gameplay on top, facecam below, subtitles, credit and
    watermark. With `foreign` it also carries what a reposted video would: another account's handle and
    a TikTok mark."""
    with Image.open(source) as opened:
        frame = opened.convert("RGB")
    width, height = frame.size
    game = frame.crop((width // 4, 0, width, height)).resize((1080, 1150))
    cam = frame.crop((0, int(height * 0.03), int(width * 0.225), int(height * 0.31))).resize((1080, 770))
    canvas = Image.new("RGB", (1080, 1920))
    canvas.paste(game, (0, 0))
    canvas.paste(cam, (0, 1150))
    draw = ImageDraw.Draw(canvas)
    _label(draw, (540, 1040), subtitle, 76)
    _label(draw, (1040, 240), "@6starsclipping", 30, anchor="rm")
    _label(draw, (40, 1860), "🎥 @speedyrp", 34, anchor="lm")
    if foreign:
        _label(draw, (540, 700), "@bestgtaclips_daily", 64)
        draw.rounded_rectangle((60, 380, 430, 500), radius=24, fill=(0, 0, 0))
        _label(draw, (245, 440), "♪ TikTok", 72)
        _label(draw, (245, 540), "@bestgtaclips_daily", 30)
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=90)
    return out.getvalue()


SUBTITLES = ("WHY IS HE ROBBING ME", "MY BAD MY BAD", "I WAS PATTING HIM DOWN", "I'M NEW I'M SORRY")


def test_p5_passes_a_clean_render(frames_dir: Path, out_dir: Path) -> None:
    sources = sorted(frames_dir.glob("src_*.jpg"))[:4]
    frames = [rendered_frame(path, SUBTITLES[index]) for index, path in enumerate(sources)]
    (out_dir / "render_clean.jpg").write_bytes(frames[0])
    verdict = calls.visual_check(frames, ref_id="live-clean")
    keep("p5_clean", verdict)
    assert verdict.problems() == [], verdict.notes


def test_p5_flags_a_reposted_video(frames_dir: Path, out_dir: Path) -> None:
    sources = sorted(frames_dir.glob("src_*.jpg"))[:4]
    frames = [rendered_frame(path, SUBTITLES[index], foreign=True) for index, path in enumerate(sources)]
    (out_dir / "render_foreign.jpg").write_bytes(frames[0])
    verdict = calls.visual_check(frames, ref_id="live-foreign")
    keep("p5_foreign", verdict)
    assert verdict.other_watermark or verdict.platform_logos
    assert verdict.notes


# ── P6 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["preferred", "restricted", "auto"])
def test_p6_reads_the_permission_tier(tier: str, out_dir: Path) -> None:
    case = CASES["permission"][tier]
    verdict = calls.read_permission(case["description"], case["name"], case["titles"])
    keep(f"p6_{tier}", verdict)
    assert verdict.tier == tier
    if tier == "auto":
        assert verdict.evidence is None
    else:
        assert verdict.evidence
        quote = verdict.evidence.strip("\"'“”. ").lower()
        assert quote[:25] in case["description"].lower()


# ── P7 ───────────────────────────────────────────────────────


def test_p7_recognises_a_takedown(out_dir: Path) -> None:
    case = CASES["takedown"]["real"]
    verdict = calls.classify_takedown(case["sender"], case["subject"], case["body"])
    keep("p7_takedown", verdict)
    assert verdict.is_takedown and verdict.kind == "copyright"
    assert "https://www.tiktok.com/@6starsclipping/video/7412345678901234567" in verdict.urls
    assert "https://www.youtube.com/shorts/aB3dEfGh1jK" in verdict.urls
    assert any("speedyrp" in handle.lower() for handle in verdict.creator_handles)
    assert {"tiktok", "youtube"} <= set(verdict.platforms)
    assert verdict.summary


def test_p7_ignores_a_newsletter_with_an_injected_instruction(out_dir: Path) -> None:
    case = CASES["takedown"]["newsletter"]
    verdict = calls.classify_takedown(case["sender"], case["subject"], case["body"])
    keep("p7_newsletter", verdict)
    assert not verdict.is_takedown and verdict.kind == "not_takedown"


# ── P8 ───────────────────────────────────────────────────────


def test_p8_translates_spanish_segments(out_dir: Path) -> None:
    case = CASES["translate"]
    segments = [Segment(float(start), float(end), str(text)) for start, end, text in case["segments"]]
    translated = calls.translate_segments(segments, case["source_lang"])
    keep("p8_translation", [item.model_dump() for item in translated])
    assert [(item.start, item.end) for item in translated] == [(seg.start, seg.end) for seg in segments]
    joined = " ".join(item.en for item in translated)
    assert "Trevor" in joined and "Sandy Shores" in joined
    assert "tío" not in joined.lower() and "joder" not in joined.lower()
    assert all(item.en for item in translated)


# ── Spend ────────────────────────────────────────────────────


def test_zz_spend_is_recorded(out_dir: Path) -> None:
    """Runs last (file order): the calls above must have left `api_usage` rows."""
    if not os.environ.get("AI_LIVE_DATABASE_URL"):
        pytest.skip("AI_LIVE_DATABASE_URL is not set: usage rows are not stored")
    rows = costs.spend_by_operation()
    keep("spend_by_operation", [asdict(row) for row in rows])
    keep("spend_today_usd", costs.today_spend())
    assert rows, "no api_usage rows were written"
    assert {row.operation for row in rows} >= {calls.P1, calls.P2, calls.P6, calls.P7, calls.P8}
    assert 0 < costs.today_spend() < 2.0

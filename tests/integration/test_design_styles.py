"""Design styles against a real database (development-plan/M18 Phase A): the plan reads the enabled
accounts and their latest styles, the posting engine gets each account's file, and the `clip.variants`
job renders the other styles of an approved clip (that last part needs ffmpeg, like the other render
tests)."""

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from posting_support import add_creator, clean_posting_tables, seed
from psycopg.types.json import Jsonb

from sixstars import config, db
from sixstars.clients.fakes import FakeAnthropicClient, FakeTranscriber
from sixstars.clients.transcribe import Transcript
from sixstars.clients.transcribe import Word as ClientWord
from sixstars.factory import pipeline, variants
from sixstars.factory.config import FactoryConfig
from sixstars.factory.director import clip_facts, plan_styles
from sixstars.factory.layout import Box, LayoutA
from sixstars.factory.lines import Line, Word
from sixstars.factory.window import ClipWindow
from sixstars.posting.store import DbStore
from sixstars.storage import FakeClipStorage


@pytest.fixture
def staging_accounts() -> None:
    """The staging accounts: tiktok_main, instagram_main and youtube_main enabled, tiktok_live not."""
    clean_posting_tables()
    seed("staging")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(path))
    config.env.cache_clear()
    return path


def add_clip(creator_id: str, status: str = "approved", **columns: object) -> str:
    values: dict[str, object] = {
        "series_no": 1,
        "creator_id": creator_id,
        "source_platform": "twitch",
        "source_id": "v1",
        "source_url": "https://www.twitch.tv/videos/1",
        "source_type": "vod",
        "lane": "hourly",
        "start_s": 0,
        "end_s": 10,
        "gate_profile": "hourly",
        "status": status,
    }
    values.update(columns)
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    row = db.fetch_one(
        f"insert into clips ({names}) values ({marks}) returning id::text as id",  # noqa: S608
        tuple(values.values()),
    )
    assert row is not None
    return str(row["id"])


def test_the_plan_covers_the_enabled_accounts_and_skips_their_last_style(staging_accounts: None) -> None:
    accounts = variants.load_accounts()
    assert [account.id for account in accounts] == ["instagram_main", "tiktok_main", "youtube_main"]
    cfg = FactoryConfig.load("hourly")
    assert cfg.catalog.ids == (
        "cam_top_question",
        "cam_top_title",
        "cam_top_viral",
        "cam_top_wordcap",
        "headline_card",
        "pov_fullframe",
    ), "migrations 0027 to 0029"
    assert cfg.catalog.rules_for("cam_top_title").title_source == "title_line"
    assert cfg.director.multiplier("discovery", "headline_card") == 3.0

    creator = add_creator()
    earlier = add_clip(creator, status="posted", style_id="cam_top_viral")
    db.execute(
        "insert into posts (clip_id, account_id, platform, lane, status, idempotency_key) "
        "values (%s, 'tiktok_main', 'tiktok', 'hourly', 'published', 'k1')",
        (earlier,),
    )
    assert variants.recent_styles(["tiktok_main", "youtube_main"], 1) == {"tiktok_main": ["cam_top_viral"]}

    plan = variants.plan_for("11111111-1111-1111-1111-111111111111", "hourly", cfg)
    assert plan.by_account["tiktok_main"] == "cam_top_title", "the last tiktok clip was cam_top_viral"
    assert set(plan.by_account) == {"instagram_main", "tiktok_main", "youtube_main"}
    assert plan.reference_account == "instagram_main"


def test_each_account_is_posted_its_own_style_and_falls_back_to_the_reference(
    staging_accounts: None, tmp_path: Path
) -> None:
    creator = add_creator()
    reference_file, title_file = tmp_path / "ref.mp4", tmp_path / "title.mp4"
    reference_file.write_bytes(b"ref")
    title_file.write_bytes(b"title")
    plan = {"by_account": {"tiktok_main": "cam_top_title", "youtube_main": "cam_top_viral"}}
    clip_id = add_clip(
        creator, style_id="cam_top_viral", style_plan=Jsonb(plan), local_path=str(reference_file)
    )
    db.execute(
        "insert into clip_renders (clip_id, style_id, is_reference, status, local_path) values "
        "(%s, 'cam_top_viral', true, 'ready', %s), (%s, 'cam_top_title', false, 'ready', %s)",
        (clip_id, str(reference_file), clip_id, str(title_file)),
    )
    store = DbStore()
    render_id, path = store.file_for_post(clip_id, "tiktok_main", str(reference_file))
    assert path == str(title_file) and render_id is not None
    _, path = store.file_for_post(clip_id, "youtube_main", str(reference_file))
    assert path == str(reference_file)
    _, path = store.file_for_post(clip_id, "instagram_main", str(reference_file))
    assert path == str(reference_file), "an account missing from the plan gets the reference"

    db.execute("update clip_renders set status = 'failed' where style_id = 'cam_top_title'")
    _, path = store.file_for_post(clip_id, "tiktok_main", str(reference_file))
    assert path == str(reference_file), "a failed variant falls back to the reference"

    older = add_clip(creator, local_path=str(reference_file))
    assert store.file_for_post(older, "tiktok_main", "/old/file.mp4") == (None, "/old/file.mp4")


def test_variants_wait_for_the_gate_and_are_skipped_for_a_rejected_clip(
    staging_accounts: None, data_dir: Path
) -> None:
    creator = add_creator()
    clip_id = add_clip(creator, status="gating")
    db.execute(
        "insert into clip_renders (clip_id, style_id, status) values (%s, 'cam_top_title', 'pending')",
        (clip_id,),
    )
    assert variants.run_variants(clip_id, 0) == "waiting"
    queued = db.fetch_one("select payload from jobs where type = %s", (variants.JOB_VARIANTS,))
    assert queued is not None and queued["payload"] == {"clip_id": clip_id, "round": 1}

    db.execute("update clips set status = 'rejected' where id = %s", (clip_id,))
    assert variants.run_variants(clip_id, 1) == "skipped"
    row = db.fetch_one("select status, error from clip_renders where clip_id = %s", (clip_id,))
    assert row is not None and (row["status"], row["error"]) == ("skipped", "clip is rejected")


def test_an_approved_clip_gets_its_other_style_rendered_checked_and_uploaded(
    staging_accounts: None, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    source = tmp_path / "source.mp4"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi"]
    command += ["-i", "testsrc2=size=1920x1080:rate=30:duration=14", "-f", "lavfi"]
    command += ["-i", "sine=frequency=440:sample_rate=48000:duration=14", "-c:v", "libx264"]
    command += ["-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)]
    subprocess.run(command, check=True, capture_output=True, timeout=300)

    creator = add_creator()
    clip_id = add_clip(creator, status="gating")
    cfg = FactoryConfig.load("hourly")
    plan = plan_styles(clip_id, "hourly", variants.load_accounts(), cfg.catalog, cfg.director)
    assert len(plan.styles) >= 2, "three accounts, three styles that need nothing of the clip"
    other = plan.styles[1]
    reference_file = data_dir / "ready" / f"{clip_id}.mp4"
    reference_file.parent.mkdir(parents=True)
    reference_file.write_bytes(b"reference")
    lines = (Line(0, (Word("no", 0.5, 0.8), Word("way", 0.8, 1.2))), Line(1, (Word("bro", 2.0, 2.4),)))
    recipe = variants.recipe_json(
        lines=lines,
        layout=LayoutA(facecam_box=Box(1440, 720, 480, 360)),
        duration_s=8.0,
        series_no=3,
        mode="normal",
        lane="hourly",
        creator="test_streamer",
        hook="No way he did that",
        keywords={0: ["way"]},
        duck=(),
        payoff_s=6.0,
        payoff_emoji="😭",
    )
    queued = variants.after_reference(
        clip_id,
        plan,
        layout_kind="C",
        output_path=reference_file,
        file_size_bytes=9,
        duration_s=8.0,
        render_ms=1,
        source=source,
        window=ClipWindow(3.0, 11.0),
        has_audio=True,
        recipe=recipe,
    )
    assert queued and (data_dir / "variants" / clip_id / variants.SOURCE_NAME).is_file()
    clip = db.fetch_one("select style_id, layout_kind, style_plan from clips where id = %s", (clip_id,))
    assert clip is not None and clip["style_id"] == plan.reference_style and clip["layout_kind"] == "C"
    assert clip["style_plan"]["by_account"] == plan.by_account
    job = db.fetch_one("select payload, dedupe_key from jobs where type = %s", (variants.JOB_VARIANTS,))
    assert job is not None and job["payload"] == {"clip_id": clip_id, "round": 0}

    db.execute("update clips set status = 'approved' where id = %s", (clip_id,))
    storage = FakeClipStorage()
    assert variants.run_variants(clip_id, 0, storage=storage) == "done"

    rows = {
        row["style_id"]: row
        for row in db.fetch_all("select * from clip_renders where clip_id = %s", (clip_id,))
    }
    assert rows[plan.reference_style]["is_reference"] and rows[plan.reference_style]["status"] == "ready"
    variant = rows[other]
    assert variant["status"] == "ready", variant["error"]
    assert variant["layout_kind"] == "C" and Path(variant["local_path"]).is_file()
    assert variant["storage_path"].endswith(f"/{clip_id}.{other}.mp4")
    assert variant["storage_path"] in storage.objects
    assert variant["checks"]["problems"] == []
    assert not (data_dir / "variants" / clip_id).exists(), "the source copy is deleted"
    done = db.fetch_one(
        "select data from events where entity_id = %s and type = 'clip.variants_done'", (clip_id,)
    )
    assert done is not None and done["data"]["renders"][other]["status"] == "ready"


def test_a_clip_with_every_text_gets_three_styles_rendered(
    staging_accounts: None, data_dir: Path, tmp_path: Path
) -> None:
    """Phase B: with the writer's texts and a facecam, the three accounts get three different styles and
    the other two are rendered in their own layouts."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    source = tmp_path / "source.mp4"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi"]
    command += ["-i", "testsrc2=size=1920x1080:rate=30:duration=10", "-f", "lavfi"]
    command += ["-i", "sine=frequency=440:sample_rate=48000:duration=10", "-c:v", "libx264"]
    command += ["-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)]
    subprocess.run(command, check=True, capture_output=True, timeout=300)

    creator = add_creator()
    clip_id = add_clip(creator, status="gating")
    cfg = FactoryConfig.load("hourly")
    texts = {
        "title_line": "Speed vs the locked truck",
        "headline": "Speed learns you can't steal every car in GTA 6",
        "question": "Can you steal any car in GTA 6?",
        "pov_line": "POV the truck has a key cloner",
    }
    facts = clip_facts(texts, has_facecam=True)
    plan = plan_styles(clip_id, "hourly", variants.load_accounts(), cfg.catalog, cfg.director, facts=facts)
    assert len(set(plan.by_account.values())) == 3 and len(plan.styles) == 3
    reference_file = data_dir / "ready" / f"{clip_id}.mp4"
    reference_file.parent.mkdir(parents=True)
    reference_file.write_bytes(b"reference")
    recipe = variants.recipe_json(
        lines=(Line(0, (Word("no", 0.5, 0.8), Word("way", 0.8, 1.2))),),
        layout=LayoutA(facecam_box=Box(1440, 720, 480, 360)),
        duration_s=6.0,
        series_no=4,
        mode="normal",
        lane="hourly",
        creator="test_streamer",
        hook="No way he did that",
        keywords={},
        duck=(),
        payoff_s=None,
        payoff_emoji=None,
        texts=texts,
    )
    assert variants.after_reference(
        clip_id,
        plan,
        layout_kind="C",
        output_path=reference_file,
        file_size_bytes=9,
        duration_s=6.0,
        render_ms=1,
        source=source,
        window=ClipWindow(2.0, 8.0),
        has_audio=True,
        recipe=recipe,
    )
    db.execute("update clips set status = 'approved' where id = %s", (clip_id,))
    assert variants.run_variants(clip_id, 0, storage=FakeClipStorage()) == "done"

    expected_kind = {
        "cam_top_viral": "C",
        "cam_top_title": "C",
        "cam_top_question": "C",
        "cam_top_wordcap": "C",
        "headline_card": "H",
        "pov_fullframe": "P",
    }
    for row in db.fetch_all("select * from clip_renders where clip_id = %s and not is_reference", (clip_id,)):
        assert row["status"] == "ready", (row["style_id"], row["error"])
        assert row["layout_kind"] == expected_kind[row["style_id"]]
        assert row["checks"]["problems"] == []


def test_a_story_cut_is_rendered_as_its_own_clip_and_queued_for_the_gate(
    staging_accounts: None, data_dir: Path, tmp_path: Path
) -> None:
    """Phase C: the writer proposes a story trim; after the short clip the same job renders it as a second
    clip (version story, parent the short one) in a style for story moments, and queues its gate."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    source = tmp_path / "long.mp4"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi"]
    command += ["-i", "testsrc2=size=1920x1080:rate=30:duration=150", "-f", "lavfi"]
    command += ["-i", "sine=frequency=440:sample_rate=48000:duration=150", "-c:v", "libx264"]
    command += ["-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)]
    subprocess.run(command, check=True, capture_output=True, timeout=600)

    words = tuple(
        ClientWord(f"word{i}", round(2.0 + i * 0.8, 3), round(2.0 + i * 0.8 + 0.5, 3)) for i in range(175)
    )
    reply = {
        "start_s": 60.0,
        "end_s": 85.0,
        "hook": "No way this happened",
        "title": "A long chase",
        "caption": "From the start to the crash.",
        "hashtags": ["gta", "chase", "crash"],
        "topic": "police chase ends in crash",
        "keywords": [],
        "spoiler": False,
        "safety": {},
        "score": 8,
        "score_parts": {"hook": 8, "clarity": 8, "payoff": 8, "rewatch": 8},
        "reject": False,
        "reject_reason": None,
        "moment_type": "action",
        "story_start_s": 10.0,
        "story_end_s": 100.0,
    }
    deps = pipeline.Deps(
        transcriber=FakeTranscriber(
            Transcript(language="en", words=words, duration_s=150.0, provider="fake")
        ),
        ai_client=FakeAnthropicClient(
            {
                "p2_clip_writer": reply,
                "p4_layout_detect": {
                    "facecam": None,
                    "gameplay": {"x": 0, "y": 0, "w": 1280, "h": 720},
                    "confidence": 0.92,
                },
            }
        ),
        skip_music=True,
        storage=FakeClipStorage(),
    )
    creator = add_creator()
    item = pipeline.RenderInput(
        source_path=source,
        window_start_s=60.0,
        window_end_s=85.0,
        creator="test_streamer",
        creator_id=creator,
        platform="youtube",
        source_id="vid-story",
        source_url="https://youtube.com/watch?v=vid-story",
        source_type="vod",
        lane="hourly",
        mode="normal",
        spike_floor_s=70.0,
        pad_start_s=0.0,
        pad_end_s=150.0,
    )
    short_id = pipeline.render_clip(item, deps=deps)

    short = db.fetch_one("select status, version, parent_clip_id from clips where id = %s", (short_id,))
    assert short is not None and (short["status"], short["version"], short["parent_clip_id"]) == (
        "gating",
        "short",
        None,
    )
    story = db.fetch_one(
        "select id::text as id, status, version, lane, duration_s, hook, topic, style_plan from clips "
        "where parent_clip_id = %s",
        (short_id,),
    )
    assert story is not None, "the story clip was not created"
    assert (story["status"], story["version"], story["lane"]) == ("gating", "story", "hourly")
    assert 60.0 <= float(story["duration_s"]) <= 150.0
    assert story["hook"] == "No way this happened" and story["topic"] == "police-chase-ends-in-crash"
    assert story["style_plan"]["moment_type"] == "story"
    gate_job = db.fetch_one("select payload from jobs where type = 'clip.gate'", ())
    assert gate_job is not None and gate_job["payload"] == {"clip_id": story["id"]}
    assert (data_dir / "ready" / f"{story['id']}.mp4").is_file()
    family = db.fetch_one(
        "select count(*) as n from clips where coalesce(parent_clip_id, id) = %s::uuid", (short_id,)
    )
    assert family is not None and family["n"] == 2

    # A retried job does not make a second story.
    assert pipeline.claim_story_clip(short_id, item, ClipWindow(0.0, 90.0)) is None


def test_the_gate_does_not_count_a_clips_own_family_as_a_duplicate(staging_accounts: None) -> None:
    from sixstars.gate import store as gate_store
    from sixstars.gate.profiles import load as load_gate_settings

    creator = add_creator()
    short = add_clip(creator, status="approved", topic="police-chase-ends-in-crash", start_s=100, end_s=130)
    story = add_clip(
        creator,
        status="gating",
        topic="police-chase-ends-in-crash",
        start_s=40,
        end_s=150,
        version="story",
        parent_clip_id=short,
    )
    stranger = add_clip(
        creator, status="approved", topic="police-chase-ends-in-crash", start_s=500, end_s=520
    )
    row = gate_store.get_clip(story)
    assert row is not None and row.version == "story" and row.family_id == short
    recent = gate_store.recent_clips(row, load_gate_settings(), datetime.now(UTC))
    assert {item.id for item in recent} == {stranger}, "the short clip of the family is not a duplicate"
    from sixstars.gate.gate import story_limits

    limits = story_limits(row, load_gate_settings()).hourly
    assert (limits.min_len_s, limits.max_len_s) == (60.0, 150.0)


def test_the_vod_lane_widens_its_sections_only_while_story_cuts_are_on(staging_accounts: None) -> None:
    from sixstars import settings as app_settings
    from sixstars.vod import sections
    from sixstars.vod.process import story_rules

    rules = story_rules()
    assert rules.enabled, "migration 0029 turns story cuts on"
    start, end = rules.widen(1000.0, 1060.0)
    assert sections.section_for(start, end, 7200.0) == sections.section_for(910.0, 1090.0, 7200.0)
    app_settings.set("factory.story", {"enabled": False})
    assert not story_rules().enabled
    assert story_rules().widen(1000.0, 1060.0) == (1000.0, 1060.0)

"""Design styles against a real database (development-plan/M18 Phase A): the plan reads the enabled
accounts and their latest styles, the posting engine gets each account's file, and the `clip.variants`
job renders the other styles of an approved clip (that last part needs ffmpeg, like the other render
tests)."""

import shutil
import subprocess
from pathlib import Path

import pytest
from posting_support import add_creator, clean_posting_tables, seed
from psycopg.types.json import Jsonb

from sixstars import config, db
from sixstars.factory import variants
from sixstars.factory.config import FactoryConfig
from sixstars.factory.director import plan_styles
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
    assert cfg.catalog.ids == ("cam_top_title", "cam_top_viral")

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
    assert len(plan.styles) == 2
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

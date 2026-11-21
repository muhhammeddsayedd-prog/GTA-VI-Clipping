"""D116 end to end: an hourly slot with migration 0035's settings keeps one clip apart across platforms."""

from datetime import timedelta
from pathlib import Path

from posting_support import (
    IG,
    T0,
    TT,
    YT,
    add_clip,
    add_creator,
    clip_row,
    fixed,
    go_live,
    posts,
    production,
    video,
)

from sixstars import settings
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.posting.hourly import run_hourly_slot
from sixstars.posting.models import Reason

__all__ = ["production", "video"]  # fixtures

MIGRATION = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0035_cross_platform_gap.sql"


def d116_on() -> None:
    settings.set("posting.cross_platform_min_gap_s", 2700)
    settings.set("posting.platform_priority", ["youtube", "tiktok", "instagram"])


def test_the_migration_seeds_the_documented_values() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "('posting.cross_platform_min_gap_s', '2700'::jsonb" in sql
    assert """('posting.platform_priority', '["youtube", "tiktok", "instagram"]'::jsonb""" in sql
    docs = (MIGRATION.parents[2] / "docs" / "16-settings-reference.md").read_text("utf-8")
    for key in ("posting.cross_platform_min_gap_s", "posting.platform_priority"):
        assert f"| `{key}` |" in docs, key


def test_youtube_takes_the_best_clip_and_the_others_the_next_ones(production: None, video: Path) -> None:
    go_live()
    d116_on()
    best = add_clip(add_creator("Best"), video, rank=9.0)
    second = add_clip(add_creator("Second"), video, rank=7.0)
    third = add_clip(add_creator("Third"), video, rank=5.0)
    client = FakeUploadPostClient()

    run = run_hourly_slot(T0, client, clock=fixed())

    assert {r["account_id"]: r["clip"] for r in posts()} == {YT: best, TT: second, IG: third}
    first_upload = client.calls_to("upload_video")[0]
    assert first_upload["platform"] == "youtube"  # YouTube picks and sends first
    assert run.plan is not None and not run.plan.slot_empty
    assert {clip_row(c)["status"] for c in (best, second, third)} == {"posted"}


def test_in_the_next_slot_the_other_platforms_may_take_youtubes_clip(production: None, video: Path) -> None:
    go_live()
    d116_on()
    best = add_clip(add_creator("Best"), video, rank=9.0)
    client = FakeUploadPostClient()

    first = run_hourly_slot(T0, client, clock=fixed())
    assert first.plan is not None
    codes = {d.account_id: d.code for d in first.plan.decisions}
    assert codes[IG] is Reason.CROSS_PLATFORM_TOO_SOON and codes[TT] is Reason.CROSS_PLATFORM_TOO_SOON
    assert [r["account_id"] for r in posts()] == [YT]

    later = T0 + timedelta(hours=1)  # 15:00 UTC: TikTok and Instagram post, YouTube does not
    second = run_hourly_slot(later, client, clock=fixed(later + timedelta(minutes=2)))

    # an hour after YouTube, TikTok (next in priority) may take it; Instagram then waits for TikTok's gap
    assert sorted((r["account_id"], r["clip"]) for r in posts()) == sorted([(YT, best), (TT, best)])
    assert second.plan is not None
    assert {d.account_id: d.code for d in second.plan.decisions}[IG] is Reason.CROSS_PLATFORM_TOO_SOON

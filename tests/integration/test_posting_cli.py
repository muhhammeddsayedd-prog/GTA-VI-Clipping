"""The `sixstars posting` commands against Postgres. The commands that can send use the fake client."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from posting_support import (
    IG,
    YT,
    add_clip,
    add_creator,
    clip_row,
    count,
    go_live,
    posts,
    production,
    staging,
    video,
)
from typer.testing import CliRunner

from sixstars import db, settings
from sixstars.cli import app
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.posting import commands

__all__ = ["production", "staging", "video"]  # fixtures

runner = CliRunner()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeUploadPostClient:
    """Every command in this file gets the fake client, never the real one."""
    fake = FakeUploadPostClient()
    monkeypatch.setattr(commands, "client_factory", lambda: fake)
    return fake


def live_now() -> datetime:
    """Posting is live at the real current time (the commands read the real clock)."""
    go_live()
    settings.set("launch.launch_at", "2026-01-01T00:00:00Z")
    db.execute("update platform_accounts set hourly_hours_utc = null")
    return datetime.now(UTC)


def same_hour(start: datetime) -> bool:
    return datetime.now(UTC).hour == start.hour


def test_plan_prints_the_decisions_and_writes_nothing(
    production: None, video: Path, client: FakeUploadPostClient
) -> None:
    now = live_now()
    clip = add_clip(add_creator(), video, approved_at=now - timedelta(hours=1))
    before = (count("posts"), count("events"), count("jobs"), count("heartbeats"))

    result = runner.invoke(app, ["posting", "plan"])

    assert result.exit_code == 0, result.output
    assert "mode=live" in result.output and "nothing was written or sent" in result.output
    assert f"instagram_main   allow    ok clip={clip}" in result.output
    assert "tiktok_live      skip     account_disabled" in result.output
    assert (count("posts"), count("events"), count("jobs"), count("heartbeats")) == before
    assert client.calls == [] and clip_row(clip)["status"] == "approved"


def test_plan_shows_a_dry_run_and_the_emergency_stop(production: None, video: Path) -> None:
    add_clip(add_creator(), video, approved_at=datetime.now(UTC) - timedelta(hours=1))

    dry = runner.invoke(app, ["posting", "plan"])
    settings.set("control.emergency_stop", True)
    stopped = runner.invoke(app, ["posting", "hourly", "--dry-run"])

    assert dry.exit_code == 0 and "mode=dry" in dry.output and "dry_run_setting" in dry.output
    assert stopped.exit_code == 0 and "mode=stopped" in stopped.output
    assert "a real run would do nothing" in stopped.output
    assert count("posts") == 0


def test_plan_refuses_a_slot_that_is_not_on_the_hour(production: None) -> None:
    result = runner.invoke(app, ["posting", "plan", "--slot", "2026-11-20T12:30:00+00:00"])

    assert result.exit_code == 1 and "exactly on the hour" in result.output


def test_hourly_runs_the_current_slot_through_the_engine(
    production: None, video: Path, client: FakeUploadPostClient
) -> None:
    now = live_now()
    clip = add_clip(add_creator(), video, approved_at=now - timedelta(hours=1))

    result = runner.invoke(app, ["posting", "hourly"])

    assert result.exit_code == 0, result.output
    if same_hour(now):
        assert "3 real posts, 0 dry-run rows" in result.output
        assert {(r["clip"], r["status"]) for r in posts()} == {(clip, "published")}
        assert len(client.calls_to("upload_video")) == 3


def test_hourly_in_dry_run_records_rows_and_sends_nothing(
    production: None, video: Path, client: FakeUploadPostClient
) -> None:
    settings.set("launch.launch_at", "2026-01-01T00:00:00Z")  # launch.dry_run is still true
    add_clip(add_creator(), video, approved_at=datetime.now(UTC) - timedelta(hours=1))

    result = runner.invoke(app, ["posting", "hourly"])

    assert result.exit_code == 0, result.output
    assert "mode=dry" in result.output and client.calls == []
    assert {(r["status"], r["dry_run"]) for r in posts()} <= {("skipped", True)}


def test_trigger_queues_the_hourly_job(production: None) -> None:
    result = runner.invoke(app, ["posting", "trigger"])

    assert result.exit_code == 0 and "scheduled: slot " in result.output
    assert count("jobs") == 1 and count("heartbeats") == 1


def test_clip_runs_in_staging_only(production: None, video: Path, client: FakeUploadPostClient) -> None:
    live_now()
    clip = add_clip(add_creator(), video)

    result = runner.invoke(app, ["posting", "clip", clip, "--account", YT])

    assert result.exit_code == 2 and "staging only" in result.output
    assert count("posts") == 0 and client.calls == []


def test_clip_posts_privately_to_youtube_in_staging(
    staging: None, video: Path, client: FakeUploadPostClient
) -> None:
    now = live_now()
    clip = add_clip(add_creator(), video, approved_at=now - timedelta(hours=1))

    result = runner.invoke(app, ["posting", "clip", clip, "--account", YT])

    assert result.exit_code == 0, result.output
    if same_hour(now):
        (upload,) = client.calls_to("upload_video")
        assert upload["fields"]["privacyStatus"] == "private" and upload["platform"] == "youtube"
        (row,) = posts()
        assert (row["account_id"], row["status"], row["dry_run"]) == (YT, "published", False)

        again = runner.invoke(app, ["posting", "clip", clip, "--account", YT])
        assert again.exit_code == 1 and "not posted" in again.output
        assert len(client.calls_to("upload_video")) == 1


def test_clip_refuses_instagram_in_staging(staging: None, video: Path, client: FakeUploadPostClient) -> None:
    now = live_now()
    clip = add_clip(add_creator(), video, approved_at=now - timedelta(hours=1))

    result = runner.invoke(app, ["posting", "clip", clip, "--account", IG])

    assert result.exit_code == 1 and "staging_instagram_not_allowed" in result.output
    assert count("posts") == 0 and client.calls == []


def test_clip_in_staging_dry_run_records_a_dry_row(
    staging: None, video: Path, client: FakeUploadPostClient
) -> None:
    clip = add_clip(add_creator(), video, approved_at=datetime.now(UTC) - timedelta(hours=1))

    result = runner.invoke(app, ["posting", "clip", clip, "--account", YT])

    assert result.exit_code == 0 and "dry run recorded" in result.output
    (row,) = posts()
    assert (row["status"], row["dry_run"]) == ("skipped", True) and client.calls == []

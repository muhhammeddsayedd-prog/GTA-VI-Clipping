"""The Twitch Clips source against Postgres with fakes for Twitch, Claude, yt-dlp and the render: one
discovery step from a clip listing to `viewer_clips` rows and jobs; the creator rules; the limits; the
`viewer_clip.process` job (happy path, retries, a deleted clip, the emergency stop, clean-up); and the
gate's source-range duplicate check seeing a viewer clip and a VOD-lane clip of the same moment."""

from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from sixstars import config, db, jobs, settings
from sixstars.ai.calls import BudgetExceededError
from sixstars.clients.fakes import FIXED_TIME, FakeAnthropicClient, FakeTwitchClient, fake_clip
from sixstars.discovery.config import DiscoveryConfig
from sixstars.factory import pipeline, tools
from sixstars.factory.pipeline import RenderInput
from sixstars.gate import checks, profiles
from sixstars.gate import store as gate_store
from sixstars.gate.models import ReasonCode
from sixstars.viewer_clips import JOB_PROCESS, process, select, source, store
from sixstars.viewer_clips import config as clips_config

NOW = FIXED_TIME + timedelta(hours=4)
MIGRATION = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0019_twitch_clips.sql"
P6 = "p6_permission_read"
AUTO = {"tier": "auto", "evidence": None}
RESTRICTED = {"tier": "restricted", "evidence": "do not clip my streams"}
GONE = "ERROR: [twitch:clips] FakeClipSlug0: This clip is no longer available"


@pytest.fixture(autouse=True)
def clean(
    database: None, test_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("truncate creators cascade")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.env.cache_clear()
    settings.set("env.target_game", "Grand Theft Auto V")
    yield
    process.configure(None)
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("truncate creators cascade")


class World:
    """Fake Twitch with a clip listing, fake Claude answering the permission check."""

    def __init__(self, clips: int = 5, verdict: Any = AUTO) -> None:
        self.twitch = FakeTwitchClient()
        self.twitch.clips = [fake_clip(index) for index in range(clips)]
        self.anthropic = FakeAnthropicClient({P6: verdict})

    def run(self, at: Any = NOW) -> source.ClipsReport:
        settings.invalidate()
        return source.run(self.twitch, DiscoveryConfig.from_settings(), at, self.anthropic)


def count(table: str, where: str = "true") -> int:
    row = db.fetch_one(f"select count(*) as n from {table} where {where}")  # noqa: S608
    assert row is not None
    return int(row["n"])


def rows(sql: str, *params: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in db.fetch_all(sql, params)]


def event_types(prefix: str = "viewer_clip.") -> list[str]:
    return [r["type"] for r in rows("select type from events where type like %s order by id", prefix + "%")]


# ── The discovery step ───────────────────────────────────────


def test_a_pass_queues_the_best_new_clips_with_rows_jobs_and_events() -> None:
    world = World()
    report = world.run()

    assert (report.status, report.found, report.eligible, report.queued) == ("ok", 5, 5, 3)
    assert report.as_source() == {"name": "twitch_clips", "status": "ok", "found": 5, "detail": ""}
    assert report.skipped == {select.NO_ROOM: 2}
    stored = rows("select * from viewer_clips order by rank_score desc")
    assert [row["source_id"] for row in stored] == ["FakeClipSlug0", "FakeClipSlug1", "FakeClipSlug2"]
    top = stored[0]
    assert (top["status"], top["platform"], top["clipped_by"]) == ("new", "twitch", "Viewer0")
    assert (top["vod_id"], float(top["vod_offset_s"]), float(top["duration_s"])) == ("video0", 600.0, 30.0)
    assert top["view_count"] == 1000 and float(top["rank_score"]) == 250.0
    assert top["rank_parts"]["views_per_hour"] == 250.0 and top["rank_parts"]["language_factor"] == 1.0
    # the creator is the broadcaster, with the login as handle (never the viewer who made the clip)
    creator = rows("select * from creators where id = %s", top["creator_id"])[0]
    assert (creator["platform"], creator["channel_id"], creator["handle"]) == ("twitch", "user0", "user0")
    assert creator["display_name"] == "Streamer0" and creator["description_checked_at"] is not None
    assert count("creators", "handle ilike 'viewer%%'") == 0
    queued = rows("select * from jobs where type = %s order by id", JOB_PROCESS)
    assert [job["payload"] for job in queued] == [{"viewer_clip_id": str(row["id"])} for row in stored]
    assert all(job["queue"] == "normal" and job["dedupe_key"].startswith("viewer_clip:") for job in queued)
    assert event_types() == ["viewer_clip.queued"] * 3
    # the summary rows expose the ranking parts and what happened to each clip
    assert [(row["picked"], row["skipped"]) for row in report.top] == [
        (True, None),
        (True, None),
        (True, None),
        (False, select.NO_ROOM),
        (False, select.NO_ROOM),
    ]
    assert report.top[0]["parts"]["views"] == 1000 and report.permission_checked == 5
    # one listing request and one user lookup; the listing asks for the look-back window
    (listing,) = world.twitch.calls_to("get_clips")
    assert listing["started_at"] == NOW - timedelta(hours=24) and listing["ended_at"] == NOW
    assert listing["max_items"] == 100 and len(world.twitch.calls_to("get_users")) == 1


def test_a_clip_is_never_queued_twice() -> None:
    world = World(clips=3)
    assert world.run().queued == 3
    again = world.run(NOW + timedelta(minutes=10))
    assert again.queued == 0 and again.skipped == {select.ALREADY_SEEN: 3}
    assert count("viewer_clips") == 3 and count("jobs", f"type = '{JOB_PROCESS}'") == 3
    assert [row["skipped"] for row in again.top] == [select.ALREADY_SEEN] * 3


def test_switched_off_not_configured_and_stopped_do_nothing() -> None:
    world = World()
    settings.set("twitch_clips.enabled", False)
    off = world.run()
    assert (off.status, off.detail, off.queued) == ("skipped", "twitch_clips.enabled is false", 0)
    settings.set("twitch_clips.enabled", True)
    world.twitch.configured = False
    assert world.run().status == "not_configured"
    world.twitch.configured = True
    settings.set("control.emergency_stop", True)
    stopped = world.run()
    assert (stopped.status, stopped.detail) == ("skipped", "control.emergency_stop is set")
    assert world.twitch.calls == [] and count("viewer_clips") == 0 and count("jobs") == 0


def test_a_database_without_the_setting_is_off() -> None:
    db.execute("delete from settings where key = 'twitch_clips.enabled'")
    assert World().run().status == "skipped"


def test_a_game_without_a_category_and_a_failing_listing_do_not_raise() -> None:
    world = World()
    settings.set("env.target_game", "Grand Theft Auto VII")
    empty = world.run()
    assert (empty.status, empty.found) == ("empty", 0) and "no Twitch category" in empty.detail

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("helix is down")

    settings.set("env.target_game", "Grand Theft Auto V")
    world.twitch.get_clips = broken  # type: ignore[method-assign]
    failed = world.run()
    assert failed.status == "error" and "helix is down" in failed.detail and count("viewer_clips") == 0


def test_clips_that_do_not_fit_are_counted_by_reason() -> None:
    world = World(clips=0)
    world.twitch.clips = [
        fake_clip(0, duration_s=12.0),
        fake_clip(1, view_count=4),
        fake_clip(2, created_at=NOW - timedelta(minutes=5)),
        fake_clip(3, title="gta 6 leak footage"),
        fake_clip(4, duration_s=59.9),
    ]
    report = world.run()
    assert report.skipped == {
        select.TOO_SHORT: 1,
        select.LOW_VIEWS: 1,
        select.TOO_FRESH: 1,
        select.TITLE_BLOCKLIST: 1,
    }
    assert [row["source_id"] for row in rows("select source_id from viewer_clips")] == ["FakeClipSlug4"]


def test_forbidden_and_excluded_creators_never_produce_a_job() -> None:
    world = World(clips=3)
    db.execute(
        "insert into creators (platform, channel_id, handle, tier, description_checked_at) "
        "values ('twitch', 'user0', 'streamer0', 'blocked', now())"
    )
    settings.set("discovery.excluded_channels", ["twitch:user1"])
    report = world.run()
    assert report.queued == 1 and report.skipped == {select.FORBIDDEN_CREATOR: 2}
    assert [row["source_id"] for row in rows("select source_id from viewer_clips")] == ["FakeClipSlug2"]
    assert rows("select tier from creators where channel_id = 'user1'")[0]["tier"] == "blocked"


def test_a_creator_who_forbids_clipping_is_restricted_by_the_permission_check() -> None:
    world = World(clips=2, verdict=RESTRICTED)
    report = world.run()
    assert report.queued == 0 and report.skipped == {select.FORBIDDEN_CREATOR: 2}
    assert report.permission_checked == 2 and count("jobs", f"type = '{JOB_PROCESS}'") == 0
    assert {row["tier"] for row in rows("select tier from creators")} == {"restricted"}


def test_an_unchecked_creator_waits_when_the_check_cannot_run() -> None:
    world = World(clips=2)
    world.anthropic = FakeAnthropicClient({P6: RuntimeError("no budget")})
    report = world.run()
    assert report.queued == 0 and report.skipped == {select.CREATOR_UNCHECKED: 2}
    # nothing is stored, so the clips are looked at again; the creators get a permission job
    assert count("viewer_clips") == 0 and report.permission_jobs == 2
    assert count("jobs", "type = 'creator.permission'") == 2
    settings.set("discovery.require_permission_check", False)
    assert world.run(NOW + timedelta(minutes=10)).queued == 2


def test_one_streamer_cannot_fill_the_pass_and_the_caps_hold() -> None:
    world = World(clips=0)
    world.twitch.clips = [
        fake_clip(i, broadcaster_id="user0", broadcaster_name="Streamer0", vod_offset_s=1000.0 * (i + 1))
        for i in range(4)
    ] + [fake_clip(9, view_count=50)]
    first = world.run()
    assert first.queued == 2 and first.skipped[select.PER_CREATOR_LIMIT] == 3
    assert sorted(r["source_id"] for r in rows("select source_id from viewer_clips")) == [
        "FakeClipSlug0",
        "FakeClipSlug9",
    ]
    # backlog: with two waiting and a limit of two, nothing more is queued and no creator is looked up
    settings.set("twitch_clips.max_backlog", 2)
    lookups = len(world.twitch.calls_to("get_users"))
    full = world.run(NOW + timedelta(minutes=10))
    assert full.queued == 0 and "2 waiting (backlog limit 2)" in full.detail
    assert len(world.twitch.calls_to("get_users")) == lookups
    # daily cap: rows of the last 24 hours count, whatever their status
    settings.set("twitch_clips.max_backlog", 6)
    settings.set("twitch_clips.daily_cap", 2)
    capped = world.run(NOW + timedelta(minutes=20))
    assert capped.queued == 0 and "2 queued in 24 h (cap 2)" in capped.detail
    settings.set("twitch_clips.daily_cap", 3)
    assert world.run(NOW + timedelta(minutes=30)).queued == 1


def test_a_row_whose_job_is_gone_is_closed_and_frees_the_backlog() -> None:
    world = World(clips=4)
    settings.set("twitch_clips.max_backlog", 2)
    settings.set("twitch_clips.max_queued_per_pass", 2)
    assert world.run().queued == 2
    # one job failed for good without the handler closing its row (the worker was dead)
    first, second = rows("select id::text as id from viewer_clips order by rank_score desc")
    db.execute("update jobs set status = 'failed' where payload->>'viewer_clip_id' = %s", (first["id"],))
    assert world.run(NOW + timedelta(minutes=10)).queued == 0  # inside the grace period: still waiting
    db.execute("alter table viewer_clips disable trigger trg_viewer_clips_updated")
    db.execute("update viewer_clips set updated_at = now() - interval '10 minutes'")
    db.execute("alter table viewer_clips enable trigger trg_viewer_clips_updated")
    later = world.run(NOW + timedelta(minutes=20))
    assert later.queued == 1
    closed = rows("select status, skip_reason from viewer_clips where id = %s", first["id"])[0]
    assert closed["status"] == "failed" and closed["skip_reason"].startswith("orphaned")
    # the row whose job is still queued was left alone
    assert rows("select status from viewer_clips where id = %s", second["id"])[0]["status"] == "new"
    assert event_types().count("viewer_clip.failed") == 1


def test_a_moment_we_already_clipped_from_the_broadcast_is_not_queued_again() -> None:
    world = World(clips=2)
    creator_id = add_creator("user9")
    add_clip(creator_id, source_id="video0", start_s=585.0, end_s=612.0, status="approved")
    add_clip(creator_id, source_id="video1", start_s=700.0, end_s=730.0, status="rejected")
    report = world.run()
    assert report.skipped == {select.SAME_MOMENT: 1}
    assert [row["source_id"] for row in rows("select source_id from viewer_clips")] == ["FakeClipSlug1"]


def test_defaults_in_code_match_the_migration() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for key, value in clips_config.DEFAULTS.items():
        assert f"('{key}', '{value}'::jsonb" in sql, key
    assert "('twitch_clips.enabled', 'true'::jsonb" in sql
    loaded = clips_config.ClipsConfig.load()
    assert loaded.enabled and (loaded.min_len_s, loaded.max_len_s) == (20.0, 60.0)  # gate.hourly
    assert loaded.source_pad_s == profiles.load().source_pad_s


# ── The job ──────────────────────────────────────────────────


def add_creator(channel: str = "user0", tier: str = "auto") -> str:
    row = db.fetch_one(
        "insert into creators (platform, channel_id, handle, tier, description_checked_at) "
        "values ('twitch', %s, %s, %s, now()) returning id::text as id",
        (channel, channel, tier),
    )
    assert row is not None
    return str(row["id"])


def add_clip(creator_id: str, **values: Any) -> str:
    row = db.fetch_one(
        """
        insert into clips (creator_id, source_platform, source_id, source_url, source_type, lane, start_s,
                           end_s, status)
        values (%s, 'twitch', %s, 'https://www.twitch.tv/videos/1', 'vod', 'hourly', %s, %s, %s)
        returning id::text as id
        """,
        (creator_id, values["source_id"], values["start_s"], values["end_s"], values["status"]),
    )
    assert row is not None
    return str(row["id"])


def queue_one(**changes: Any) -> tuple[str, jobs.Job]:
    """Store one viewer clip through the real discovery step and claim its job."""
    world = World(clips=0)
    world.twitch.clips = [fake_clip(0, **changes)]
    assert world.run().queued == 1
    job = jobs.claim("normal", "test-worker")
    assert job is not None and job.type == JOB_PROCESS
    return str(job.payload["viewer_clip_id"]), job


class Stage:
    """Stand-ins for yt-dlp, ffprobe, the factory and the gate, recording what they were asked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, duration_s: float = 30.0) -> None:
        self.commands: list[list[str]] = []
        self.rendered: list[RenderInput] = []
        self.gated: list[str] = []
        self.download_output = (0, "", "")
        self.render_error: Exception | None = None
        self.after_status = "approved"
        self.files_seen: list[bool] = []
        monkeypatch.setattr(
            tools,
            "probe",
            lambda path, ffprobe="ffprobe": tools.MediaInfo(1920, 1080, duration_s, True, True),
        )

    def runner(self, command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        self.commands.append(list(command))
        code, out, err = self.download_output
        if code == 0:
            template = Path(command[command.index("--output") + 1])
            template.with_name("source.mp4").write_bytes(b"not really a video")
        return code, out, err

    def render(self, item: RenderInput) -> str:
        self.rendered.append(item)
        self.files_seen.append(item.source_path.is_file())
        row = pipeline.claim_clip(item)
        if self.render_error is not None:
            raise self.render_error
        db.execute("update clips set status = 'gating', duration_s = 30 where id = %s", (row.id,))
        return row.id

    def after_render(self, clip_id: str, job: jobs.Job) -> None:
        self.gated.append(clip_id)
        db.execute("update clips set status = %s where id = %s", (self.after_status, clip_id))

    @property
    def deps(self) -> process.Deps:
        return process.Deps(runner=self.runner, render=self.render, after_render=self.after_render)


def viewer_clip(row_id: str) -> dict[str, Any]:
    return rows("select * from viewer_clips where id = %s", row_id)[0]


def test_the_job_downloads_renders_gates_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    row_id, job = queue_one()
    stage = Stage(monkeypatch)
    outcome = process.handle(job, stage.deps)

    assert (outcome.status, outcome.clip_status) == ("done", "approved")
    (command,) = stage.commands
    assert command[-1] == "https://www.twitch.tv/streamer0/clip/FakeClipSlug0"
    assert "--proxy" not in command and "--cookies" not in command
    (item,) = stage.rendered
    assert stage.files_seen == [True]
    # credited to the broadcaster, stored under the broadcast video on its clock, on the hourly lane
    assert (item.creator, item.lane, item.clip_id) == ("user0", "hourly", row_id)
    assert (item.source_id, item.source_offset_s, item.window_end_s) == ("video0", 600.0, 30.0)
    assert item.whole_window and item.moment_chosen is False  # the switch is off by default
    clip = rows("select * from clips where id = %s", row_id)[0]
    assert (clip["source_platform"], clip["source_id"], clip["lane"]) == ("twitch", "video0", "hourly")
    assert (float(clip["start_s"]), float(clip["end_s"])) == (600.0, 630.0)
    assert clip["source_url"].endswith("/clip/FakeClipSlug0") and clip["spike_id"] is None
    assert stage.gated == [row_id]
    row = viewer_clip(row_id)
    assert (row["status"], row["attempts"], row["skip_reason"]) == ("done", 1, None)
    assert event_types() == ["viewer_clip.queued", "viewer_clip.processing", "viewer_clip.done"]
    assert not store.download_dir(row_id).exists()  # the downloaded file is gone


def test_a_retried_job_does_not_render_twice_or_make_a_second_row(monkeypatch: pytest.MonkeyPatch) -> None:
    row_id, job = queue_one()
    stage = Stage(monkeypatch)
    process.handle(job, stage.deps)
    # the same job again (a released or recovered job): the row is closed
    assert process.handle(job, stage.deps).status == "ignored"
    # the worker died after the render and before the row was closed: no second download or render
    db.execute("update viewer_clips set status = 'processing' where id = %s", (row_id,))
    db.execute("update clips set status = 'gating' where id = %s", (row_id,))
    again = process.handle(job, stage.deps)
    assert (again.status, again.clip_status) == ("done", "approved")
    assert len(stage.commands) == 1 and len(stage.rendered) == 1 and stage.gated == [row_id, row_id]
    assert count("clips") == 1 and count("viewer_clips") == 1


def test_a_deleted_clip_is_skipped_at_once_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    row_id, job = queue_one()
    stage = Stage(monkeypatch)
    stage.download_output = (1, "", GONE)
    outcome = process.handle(job, stage.deps)  # does not raise: the job ends as done
    assert outcome.status == "skipped" and stage.rendered == []
    row = viewer_clip(row_id)
    assert row["status"] == "skipped" and row["skip_reason"].startswith("unavailable:")
    assert event_types()[-1] == "viewer_clip.skipped" and count("clips") == 0
    assert not store.download_dir(row_id).exists()
    assert process.handle(job, stage.deps).status == "ignored"


def test_a_failed_download_is_retried_and_closed_after_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_id, job = queue_one()
    stage = Stage(monkeypatch)
    stage.download_output = (1, "", "ERROR: HTTP Error 503: Service Unavailable")
    with pytest.raises(Exception, match="503"):
        process.handle(job, stage.deps)
    assert viewer_clip(row_id)["status"] == "processing"  # open: the queue runs the job again
    final = jobs.Job.from_row({**{name: getattr(job, name) for name in jobs.Job.__slots__}, "attempts": 3})
    with pytest.raises(Exception, match="503"):
        process.handle(final, stage.deps)
    row = viewer_clip(row_id)
    assert row["status"] == "failed" and "503" in row["skip_reason"] and row["attempts"] == 2
    assert event_types()[-1] == "viewer_clip.failed" and count("clips") == 0
    assert process.handle(final, stage.deps).status == "ignored"  # never again


def test_a_failed_render_keeps_the_clip_row_for_the_retry_and_always_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_id, job = queue_one()
    stage = Stage(monkeypatch)
    stage.render_error = RuntimeError("ffmpeg crashed")
    with pytest.raises(RuntimeError):
        process.handle(job, stage.deps)
    assert not store.download_dir(row_id).exists()
    assert rows("select status from clips where id = %s", row_id)[0]["status"] == "rendering"
    stage.render_error = None
    assert process.handle(job, stage.deps).clip_status == "approved"  # same clip row, rendered now
    assert count("clips") == 1 and len(stage.commands) == 2


def test_the_emergency_stop_defers_the_job_without_downloading(monkeypatch: pytest.MonkeyPatch) -> None:
    row_id, job = queue_one()
    stage = Stage(monkeypatch)
    settings.set("control.emergency_stop", True)
    settings.invalidate()
    outcome = process.handle(job, stage.deps)
    assert outcome.status == "deferred" and stage.commands == [] and stage.rendered == []
    assert viewer_clip(row_id)["status"] == "new"
    (again,) = rows("select * from jobs where type = %s and status = 'queued'", JOB_PROCESS)
    assert again["dedupe_key"] == f"viewer_clip:{row_id}:again" and again["attempts"] == 0
    assert again["run_after"] > job.run_after and again["payload"] == {"viewer_clip_id": row_id}


def test_a_used_up_ai_budget_defers_to_the_next_budget_day(monkeypatch: pytest.MonkeyPatch) -> None:
    row_id, job = queue_one()
    stage = Stage(monkeypatch)
    stage.render_error = BudgetExceededError("ai.daily_budget_usd is used up")
    assert process.handle(job, stage.deps).status == "deferred"
    assert viewer_clip(row_id)["status"] == "new" and event_types()[-1] == "viewer_clip.deferred"
    assert count("jobs", f"type = '{JOB_PROCESS}' and status = 'queued'") == 1
    assert not store.download_dir(row_id).exists()


def test_a_creator_who_became_forbidden_is_skipped_before_the_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_id, job = queue_one()
    db.execute("update creators set tier = 'restricted'")
    stage = Stage(monkeypatch)
    assert process.handle(job, stage.deps).status == "skipped" and stage.commands == []
    assert viewer_clip(row_id)["skip_reason"] == "creator forbids clipping"


def test_a_job_without_a_row_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _, job = queue_one()
    db.execute("delete from viewer_clips")
    assert process.handle(job, Stage(monkeypatch).deps).status == "ignored"


def test_the_worker_registers_the_handler() -> None:
    from sixstars import viewer_clips
    from sixstars.services import worker

    assert "sixstars.viewer_clips:register_handlers" in worker.HANDLER_MODULES
    viewer_clips.register_handlers()
    viewer_clips.register_handlers()  # safe to call twice
    assert worker.HANDLERS[JOB_PROCESS] is process.handle_job


# ── The gate sees it ─────────────────────────────────────────


def test_the_gate_rejects_a_viewer_clip_of_a_moment_the_vod_lane_already_clipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a viewer clipped seconds 2015 to 2045 of broadcast video0, and discovery queued it
    row_id, job = queue_one(vod_offset_s=2015.0)
    # meanwhile the VOD lane rendered seconds 2000 to 2030 of the same broadcast, and it was approved
    ours = add_clip(
        add_creator("user7"), source_id="video0", start_s=2000.0, end_s=2030.0, status="rendering"
    )
    db.execute(
        "update clips set status = 'approved', approved_at = now(), created_at = now() - interval '1 hour' "
        "where id = %s",
        (ours,),
    )
    stage = Stage(monkeypatch)
    stage.after_status = "gating"  # leave the clip for the real duplicate check below
    process.handle(job, stage.deps)

    row = gate_store.get_clip(row_id)
    assert row is not None
    gate_settings = profiles.load()
    recent = gate_store.recent_clips(row, gate_settings, NOW)
    result = checks.check_source_range(gate_store.clip_facts(row, None, None), recent, gate_settings)
    assert result.codes == (ReasonCode.DUPLICATE_SOURCE_RANGE,) and ours in result.reasons[0]
    # a viewer clip from another part of the same broadcast is not a duplicate
    db.execute("update clips set start_s = 9000, end_s = 9030 where id = %s", (row_id,))
    row = gate_store.get_clip(row_id)
    assert row is not None
    far = checks.check_source_range(gate_store.clip_facts(row, None, None), recent, gate_settings)
    assert far.codes == ()


def test_the_gate_reads_audience_evidence_only_for_a_viewer_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    row_id, job = queue_one()
    process.handle(job, Stage(monkeypatch).deps)
    evidence = gate_store.audience_row(row_id, None)
    assert evidence is not None and evidence["title"] == "GTA clip 0" and evidence["view_count"] == 1000
    assert evidence["clip_created_at"] == FIXED_TIME
    assert gate_store.audience_row(row_id, "some-spike") is None  # a spike clip is never asked about
    other = add_clip(add_creator("user8"), source_id="v", start_s=0.0, end_s=30.0, status="gating")
    assert gate_store.audience_row(other, None) is None

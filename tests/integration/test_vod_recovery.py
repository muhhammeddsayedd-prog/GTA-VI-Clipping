"""Integration tests for fix-stale-processing: a video left in `processing` by a restarted worker holds no
concurrency slot and is healed, the slot claim is atomic between two workers, a job that finds no slot
waits longer each time without using attempts, and the lane leaves one `normal` worker free for renders."""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from sixstars import db, jobs, settings
from sixstars.services import scheduler
from sixstars.vod import process, store
from sixstars.vod.common import Deps
from sixstars.vod.config import VodConfig

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)


class World:
    """A creator and its videos; nothing is downloaded (the videos have no chat and no fallback)."""

    def __init__(self) -> None:
        self.creator_id = store.ensure_creator("youtube", "UCvodrecovery", "recoverer", None)

    def video(self, source_id: str, status: str = "new") -> str:
        video_id = store.ensure_video(
            creator_id=self.creator_id,
            platform="youtube",
            source_id=source_id,
            url=f"https://www.youtube.com/watch?v={source_id}",
            title="GTA RP night",
            duration_s=3600,
            has_chat=False,
        )
        db.execute("update videos set status = %s where id = %s", (status, video_id))
        return video_id

    def cleanup(self) -> None:
        db.execute("delete from videos where creator_id = %s", (self.creator_id,))
        db.execute("delete from creators where id = %s", (self.creator_id,))


@pytest.fixture
def world(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    from sixstars import config

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.env.cache_clear()
    settings.set("vod.fallback_enabled", False)
    settings.set("vod.concurrency_retry_jitter", 0)
    made = World()
    yield made
    made.cleanup()


def deps() -> Deps:
    return Deps(chat_downloader=lambda platform, source_id, raw: None, now=lambda: NOW, rand=lambda: 1.0)


def job_for(video_id: str, status: str = "queued", job_type: str = "vod.process") -> int:
    job_id = jobs.enqueue("normal", job_type, {"video_id": video_id}, priority=30)
    assert job_id is not None
    db.execute("update jobs set status = %s where id = %s", (status, job_id))
    return job_id


def run(job_id: int) -> process.Outcome:
    """Run one job the way the worker does."""
    row = db.fetch_one(
        "update jobs set status = 'running', attempts = attempts + 1 where id = %s returning *", (job_id,)
    )
    assert row is not None
    outcome = process.handle(jobs.Job.from_row(row), deps())
    jobs.complete(job_id)
    return outcome


def status_of(video_id: str) -> str:
    row = db.fetch_one("select status from videos where id = %s", (video_id,))
    assert row is not None
    return str(row["status"])


def events_of(video_id: str, event_type: str) -> int:
    row = db.fetch_one(
        "select count(*) as n from events where type = %s and entity_id = %s", (event_type, video_id)
    )
    assert row is not None
    return int(row["n"])


def queued_jobs(video_id: str) -> list[dict[str, Any]]:
    return db.fetch_all(
        "select * from jobs where status = 'queued' and payload->>'video_id' = %s order by id", (video_id,)
    )


def workers(count: int, queue: str = "normal") -> None:
    for index in range(count):
        db.heartbeat(f"worker-{queue}-host-{index}", {"queue": queue})


# ── what counts as in work ───────────────────────────────────


def test_only_a_video_with_a_running_job_or_a_fresh_update_holds_a_slot(world: World) -> None:
    me = world.video("recover0000")
    zombie = world.video("recover0001", "processing")
    assert store.count_in_work(me, 300) == 1  # just updated: may be a run by hand, which has no job
    assert store.count_in_work(me, 0) == 0  # not updated for the grace and no job: an orphan

    job_id = job_for(zombie, "queued")  # released at shutdown, waiting to run again
    assert store.count_in_work(me, 0) == 0
    for ended in ("done", "failed", "cancelled"):
        db.execute("update jobs set status = %s where id = %s", (ended, job_id))
        assert store.count_in_work(me, 0) == 0
    db.execute("update jobs set status = 'running' where id = %s", (job_id,))
    assert store.count_in_work(me, 0) == 1
    assert store.count_in_work(zombie, 0) == 0  # a video never counts against itself


def test_live_workers_counts_fresh_heartbeats_of_one_queue() -> None:
    workers(2)
    workers(1, "express")
    db.heartbeat("worker-normal-host-dead", {"queue": "normal"})
    db.execute(
        "update heartbeats set last_seen = now() - interval '10 minutes' where name = %s",
        ("worker-normal-host-dead",),
    )
    assert jobs.live_workers("normal", 90) == 2
    assert jobs.live_workers("express", 90) == 1
    assert jobs.live_workers("normal", 3600) == 3


def test_the_lane_leaves_one_normal_worker_free_for_renders() -> None:
    settings.set("vod.max_concurrent", 3)
    assert process.concurrency_limit(VodConfig.load()) == 1  # no heartbeat at all (CLI, tests)
    workers(2)
    assert process.concurrency_limit(VodConfig.load()) == 1
    workers(4)
    assert process.concurrency_limit(VodConfig.load()) == 3
    settings.set("vod.max_concurrent", 2)
    assert process.concurrency_limit(VodConfig.load()) == 2


# ── the claim ────────────────────────────────────────────────


def test_two_workers_cannot_both_take_the_last_slot(world: World, test_database_url: str) -> None:
    first, second = world.video("recover0010"), world.video("recover0011")
    results: dict[str, str] = {}

    def take(video_id: str) -> None:
        results[video_id] = store.claim(video_id, 7200, limit=1, grace_s=300)

    threads = [threading.Thread(target=take, args=(video_id,)) for video_id in (first, second)]
    # Hold the slot lock on a third connection so both claims are waiting at the same point, then let go.
    with psycopg.connect(test_database_url) as holder:
        holder.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (store.SLOT_LOCK,))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert results == {} and all(thread.is_alive() for thread in threads)
        holder.rollback()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results.values()) == ["claimed", "full"]
    assert sorted([status_of(first), status_of(second)]) == ["new", "processing"]


def test_a_job_takes_over_its_own_video_left_in_processing(world: World) -> None:
    video_id = world.video("recover0020", "processing")
    assert store.claim(video_id, 7200) == "busy"  # no job named: only a stale row may be taken
    mine, other = job_for(video_id, "running"), job_for(video_id, "running", "replay.scan")
    assert store.claim(video_id, 7200, job_id=mine) == "busy"  # another job is running for it
    db.execute("update jobs set status = 'done' where id = %s", (other,))
    assert store.claim(video_id, 7200, job_id=mine) == "claimed"
    row = db.fetch_one(
        "select data from events where type = 'video.processing' and entity_id = %s", (video_id,)
    )
    assert row is not None and row["data"]["resumed"] is True and row["data"]["job_id"] == mine


# ── recovery ─────────────────────────────────────────────────


def test_orphans_are_reset_with_one_event_each(world: World) -> None:
    orphan, other_orphan = world.video("recover0030", "processing"), world.video("recover0031", "processing")
    working, waiting = world.video("recover0032", "processing"), world.video("recover0033")
    job_for(working, "running")

    assert store.reset_orphans(300) == []  # all updated a moment ago
    assert sorted(store.reset_orphans(0)) == sorted([orphan, other_orphan])
    assert store.reset_orphans(0) == []

    assert [status_of(v) for v in (orphan, other_orphan, working, waiting)] == [
        "new",
        "new",
        "processing",
        "new",
    ]
    assert [events_of(v, store.EVENT_RECOVERED) for v in (orphan, other_orphan, working, waiting)] == [
        1,
        1,
        0,
        0,
    ]


def test_recovery_gives_every_orphan_a_way_forward(world: World) -> None:
    settings.set("vod.orphan_grace_s", 0)
    no_job = world.video("recover0040", "processing")
    job_done = world.video("recover0041", "processing")
    job_queued = world.video("recover0042", "processing")
    job_failed = world.video("recover0043", "processing")
    job_for(job_done, "done")
    queued_id = job_for(job_queued, "queued")
    job_for(job_failed, "failed")

    assert sorted(process.recover_orphans()) == sorted([no_job, job_done, job_queued, job_failed])

    assert [status_of(v) for v in (no_job, job_done, job_queued)] == ["new", "new", "new"]
    assert status_of(job_failed) == "failed"  # out of attempts: closed, not queued for ever
    for video_id in (no_job, job_done):
        (job,) = queued_jobs(video_id)
        assert job["type"] == "vod.process" and job["attempts"] == 0
    assert [job["id"] for job in queued_jobs(job_queued)] == [queued_id]
    assert queued_jobs(job_failed) == []
    assert process.recover_orphans() == []


def test_a_zombie_no_longer_stalls_the_lane(world: World) -> None:
    """The staging incident: the workers were restarted, a video stayed in `processing`."""
    settings.set("vod.orphan_grace_s", 0)
    zombie, waiting = world.video("recover0050", "processing"), world.video("recover0051")
    job_id = job_for(waiting)

    assert run(job_id).status == "done"  # not deferred: the zombie holds no slot

    assert status_of(waiting) == "done" and status_of(zombie) == "new"
    assert events_of(zombie, store.EVENT_RECOVERED) == 1
    (again,) = queued_jobs(zombie)
    assert run(int(again["id"])).status == "done" and status_of(zombie) == "done"


def test_the_scheduler_task_runs_once_per_interval(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.set("vod.orphan_grace_s", 0)
    monkeypatch.setattr(scheduler, "_vod_recovery_at", None)
    orphan = world.video("recover0060", "processing")
    assert "vod_recovery" in [task.name for task in scheduler.tasks()]

    assert scheduler.recover_vod_orphans(clock=1000.0) == [orphan]
    db.execute("update videos set status = 'processing' where id = %s", (orphan,))
    assert scheduler.recover_vod_orphans(clock=1059.0) is None  # vod.recovery_interval_s is 60
    assert status_of(orphan) == "processing"
    assert scheduler.recover_vod_orphans(clock=1060.0) == [orphan]
    assert events_of(orphan, store.EVENT_RECOVERED) == 2


# ── waiting for a slot ───────────────────────────────────────


def test_a_job_without_a_slot_waits_longer_each_time_and_uses_no_attempt(world: World) -> None:
    busy, waiting = world.video("recover0070", "processing"), world.video("recover0071")
    job_for(busy, "running")
    job_id = job_for(waiting)
    waits: list[float] = []

    for deferrals in range(1, 6):
        with capture_logs() as logs:
            outcome = run(job_id)
        assert outcome.status == "deferred" and outcome.kind == "concurrency"
        assert outcome.run_after is not None
        waits.append((outcome.run_after - NOW).total_seconds())
        (deferred,) = [entry for entry in logs if entry["event"] == "vod_deferred"]
        assert deferred["log_level"] == "info"
        (job,) = queued_jobs(waiting)
        assert job["attempts"] == 0 and job["max_attempts"] == 3
        assert job["payload"] == {"video_id": waiting, "deferrals": deferrals}
        assert job["run_after"] == NOW + timedelta(seconds=waits[-1])
        job_id = int(job["id"])

    assert waits == [120, 240, 480, 600, 600]  # doubles up to vod.concurrency_retry_max_s
    row = db.fetch_one("select status, attempts from videos where id = %s", (waiting,))
    assert row == {"status": "new", "attempts": 0}
    assert status_of(busy) == "processing"

    db.execute("update jobs set status = 'done' where payload->>'video_id' = %s", (busy,))
    db.execute("update videos set status = 'done' where id = %s", (busy,))
    assert run(job_id).status == "done"

"""The worker, the scheduler and the live manager against Postgres: handler registration with a real
`post.hourly` job (fake Upload-Post client, nothing is uploaded), graceful shutdown, recovery of jobs
left behind by a dead worker, the ranking refresh and the stub services."""

import functools
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from posting_support import add_clip, add_creator, add_spike, go_live, posts, production, video

from sixstars import db, events, jobs, settings
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.posting import handlers as posting_handlers
from sixstars.posting.hourly import HOURLY_JOB_TYPE
from sixstars.posting.slots import slot_start
from sixstars.services import live_manager, scheduler, worker

__all__ = ["production", "video"]  # fixtures


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeUploadPostClient]:
    """An empty handler registry whose posting handlers get the fake Upload-Post client."""
    client = FakeUploadPostClient()
    monkeypatch.setattr(worker, "HANDLERS", {})
    monkeypatch.setattr(worker, "_registered_modules", set())
    monkeypatch.setattr(
        posting_handlers, "register", functools.partial(posting_handlers.register, lambda: client)
    )
    yield client


def wait_for(condition: Callable[[], bool], timeout_s: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def start_worker(queue: str, grace_s: float = 30.0) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(queue, stop, grace_s), daemon=True)
    thread.start()
    return thread, stop


def job_row(job_id: int) -> dict[str, Any]:
    row = db.fetch_one("select * from jobs where id = %s", (job_id,))
    assert row is not None
    return row


def status_of(job_id: int) -> str:
    return str(job_row(job_id)["status"])


# ── worker: registration and a real posting job ───────────────────────────────────────────────────────────


def test_the_worker_registers_the_handlers_and_runs_a_post_hourly_job_end_to_end(
    production: None, video: Path, registry: FakeUploadPostClient
) -> None:
    go_live()
    settings.set("launch.launch_at", "2026-01-01T00:00:00Z")
    db.execute("update platform_accounts set hourly_hours_utc = null")
    now = datetime.now(UTC)
    clip = add_clip(add_creator(), video, approved_at=now - timedelta(hours=1))
    job_id = jobs.enqueue("express", HOURLY_JOB_TYPE, {"slot_at": slot_start(now).isoformat()})
    assert job_id is not None

    thread, stop = start_worker("express")
    try:
        assert wait_for(lambda: status_of(job_id) == "done"), job_row(job_id)
    finally:
        stop.set()
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert {"post.hourly", "post.retry"} <= set(worker.HANDLERS)
    heartbeat = db.fetch_one("select name from heartbeats where name = %s", (jobs.worker_name("express"),))
    assert heartbeat is not None
    if slot_start(datetime.now(UTC)) == slot_start(now):  # not across the top of the hour
        assert {(row["clip"], row["status"]) for row in posts()} == {(clip, "published")}
        assert len(registry.calls_to("upload_video")) == 3  # the fake client, nothing left this machine


def test_a_job_of_a_type_nobody_registered_fails_without_retry(registry: FakeUploadPostClient) -> None:
    job_id = jobs.enqueue("normal", "nobody.knows")
    assert job_id is not None
    thread, stop = start_worker("normal")
    try:
        assert wait_for(lambda: status_of(job_id) == "failed")
    finally:
        stop.set()
        thread.join(timeout=60)
    assert "no handler" in job_row(job_id)["last_error"]


# ── worker: graceful shutdown ─────────────────────────────────────────────────────────────────────────────


def test_on_stop_the_worker_lets_the_current_job_finish_and_claims_no_more(
    registry: FakeUploadPostClient,
) -> None:
    started, finish = threading.Event(), threading.Event()

    def slow(job: jobs.Job) -> None:
        started.set()
        finish.wait(60)

    worker.register("test.slow", slow)
    first = jobs.enqueue("normal", "test.slow")
    second = jobs.enqueue("normal", "test.slow")
    assert first is not None and second is not None
    thread, stop = start_worker("normal", grace_s=60.0)
    assert started.wait(60)
    stop.set()
    time.sleep(0.5)
    assert thread.is_alive()  # still waiting for the job
    finish.set()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert status_of(first) == "done"
    assert status_of(second) == "queued"  # not claimed after the stop
    assert job_row(second)["attempts"] == 0


def test_when_the_grace_runs_out_the_job_goes_back_to_the_queue_and_is_not_completed_later(
    registry: FakeUploadPostClient,
) -> None:
    started, finish = threading.Event(), threading.Event()

    def stuck(job: jobs.Job) -> None:
        started.set()
        finish.wait(60)

    worker.register("test.stuck", stuck)
    job_id = jobs.enqueue("normal", "test.stuck")
    assert job_id is not None
    thread, stop = start_worker("normal", grace_s=0.5)
    assert started.wait(60)
    stop.set()
    thread.join(timeout=60)
    assert not thread.is_alive()  # the worker did not wait for the handler

    row = job_row(job_id)
    assert (row["status"], row["attempts"], row["locked_by"], row["locked_at"]) == ("queued", 0, None, None)
    assert row["last_error"] == "released at worker shutdown"
    assert row["run_after"] <= datetime.now(UTC) + timedelta(seconds=5)

    # Another worker takes it; the old handler ending now must not complete the new owner's job.
    claimed = jobs.claim("normal", "worker-normal-other-1")
    assert claimed is not None and claimed.id == job_id
    finish.set()
    time.sleep(1.0)
    row = job_row(job_id)
    assert (row["status"], row["locked_by"]) == ("running", "worker-normal-other-1")


def test_release_only_works_for_the_worker_that_holds_the_job() -> None:
    job_id = jobs.enqueue("normal", "t")
    assert job_id is not None
    assert jobs.release(job_id, "w1") is False  # not running
    assert jobs.claim("normal", "w1") is not None
    assert jobs.release(job_id, "w2") is False
    assert status_of(job_id) == "running"
    assert jobs.release(job_id, "w1") is True
    assert jobs.release(job_id, "w1") is False
    assert (status_of(job_id), job_row(job_id)["attempts"]) == ("queued", 0)


# ── stale job recovery ────────────────────────────────────────────────────────────────────────────────────


def running_job(worker_name: str, locked_ago_s: float, attempts: int = 1, max_attempts: int = 3) -> int:
    job_id = jobs.enqueue("normal", "t", max_attempts=max_attempts)
    assert job_id is not None
    db.execute(
        "update jobs set status = 'running', locked_by = %s, attempts = %s, "
        "locked_at = now() - make_interval(secs => %s) where id = %s",
        (worker_name, attempts, locked_ago_s, job_id),
    )
    return job_id


def beat(name: str, ago_s: float) -> None:
    db.execute(
        "insert into heartbeats (name, last_seen) values (%s, now() - make_interval(secs => %s))",
        (name, ago_s),
    )


def test_requeue_stale_takes_back_only_the_jobs_of_workers_without_a_fresh_heartbeat() -> None:
    beat("worker-dead", 900)
    beat("worker-alive", 10)
    dead = running_job("worker-dead", 1000)
    gone = running_job("worker-never-seen", 1000)  # no heartbeat row at all
    alive_long = running_job("worker-alive", 5000)  # a long render on a live worker
    fresh = running_job("worker-starting", 20)  # claimed just now, heartbeat not written yet
    queued = jobs.enqueue("normal", "t")
    assert queued is not None

    recovered = jobs.requeue_stale(300)

    assert [(job.id, job.status, job.attempts, job.locked_by) for job in recovered] == [
        (dead, "queued", 1, "worker-dead"),
        (gone, "queued", 1, "worker-never-seen"),
    ]
    for job_id in (dead, gone):
        row = job_row(job_id)
        assert (row["status"], row["locked_by"], row["locked_at"]) == ("queued", None, None)
        assert "heartbeat stale" in row["last_error"]
        assert row["run_after"] <= datetime.now(UTC) + timedelta(seconds=5)
    assert status_of(alive_long) == "running" and status_of(fresh) == "running"
    assert status_of(queued) == "queued"
    assert jobs.requeue_stale(300) == []
    claimed = [jobs.claim("normal", "worker-new") for _ in range(3)]
    assert {job.id: job.attempts for job in claimed if job is not None} == {queued: 1, dead: 2, gone: 2}


def test_a_job_that_lost_its_worker_on_the_last_attempt_fails_instead_of_looping() -> None:
    last = running_job("worker-dead", 1000, attempts=3, max_attempts=3)
    (recovered,) = jobs.requeue_stale(300)
    assert (recovered.id, recovered.status) == (last, "failed")
    assert status_of(last) == "failed"
    assert jobs.claim("normal", "worker-new") is None


def test_a_recovered_job_with_a_dedupe_key_keeps_it() -> None:
    job_id = jobs.enqueue("express", "post.hourly", dedupe_key="post.hourly:slot")
    assert job_id is not None
    db.execute(
        "update jobs set status = 'running', locked_by = 'worker-dead', attempts = 1, "
        "locked_at = now() - interval '1 hour' where id = %s",
        (job_id,),
    )
    assert [job.id for job in jobs.requeue_stale(300)] == [job_id]
    assert jobs.enqueue("express", "post.hourly", dedupe_key="post.hourly:slot") is None


def test_the_scheduler_recovers_stale_jobs_with_the_guardian_threshold_events_and_one_alert() -> None:
    settings.set("guardian.heartbeat_stale_s", 120)
    beat("worker-dead", 200)
    within = running_job("worker-slowish", 100)
    lost = running_job("worker-dead", 200)
    exhausted = running_job("worker-dead", 200, attempts=3)

    recovered = scheduler.recover_stale_jobs()

    assert [(job.id, job.status) for job in recovered] == [(lost, "queued"), (exhausted, "failed")]
    assert status_of(within) == "running"
    rows = db.fetch_all("select * from events where type = 'job.recovered' order by id")
    assert [(row["entity_type"], row["entity_id"], row["level"], row["service"]) for row in rows] == [
        ("job", str(lost), "warn", "scheduler"),
        ("job", str(exhausted), "error", "scheduler"),
    ]
    assert rows[0]["data"]["worker"] == "worker-dead" and rows[0]["data"]["stale_after_s"] == 120
    alerts = db.fetch_all("select severity, title, body from notify_outbox")
    assert [(a["severity"], a["title"]) for a in alerts] == [("warn", "Jobs recovered from a dead worker")]
    assert "2 job(s) taken back, 1 out of attempts." in alerts[0]["body"]
    assert scheduler.recover_stale_jobs() == []
    assert len(db.fetch_all("select 1 from notify_outbox")) == 1


# ── scheduler: ranking refresh, notification retry, the loop ──────────────────────────────────────────────


def rank_of(clip_id: str) -> float | None:
    row = db.fetch_one("select rank_score from clips where id = %s", (clip_id,))
    assert row is not None
    return None if row["rank_score"] is None else float(row["rank_score"])


def test_the_ranking_refresh_recomputes_approved_clips_and_leaves_the_rest(production: None) -> None:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    half_life = settings.get_dict("posting.freshness_half_life_h")
    creator = add_creator()
    preferred = add_creator("Preferred")
    db.execute("update creators set tier = 'preferred' where id = %s", (preferred,))

    vod = add_clip(creator, None, rank=1.0, approved_at=now - timedelta(hours=half_life["vod"]))
    by_preferred = add_clip(preferred, None, rank=1.0, approved_at=now)
    spike = add_spike(creator, now - timedelta(hours=half_life["live"]))
    live = add_clip(creator, None, lane="spike", rank=1.0, approved_at=now, spike_id=spike)
    demoted_spike = add_spike(creator, now)
    demoted = add_clip(creator, None, lane="hourly", rank=1.0, approved_at=now, spike_id=demoted_spike)
    db.execute("update clips set source_type = 'live' where id = %s", (demoted,))
    events.emit("clip.demoted", entity_type="clip", entity_id=demoted)
    no_score = add_clip(creator, None, rank=7.0, approved_at=now)
    posted = add_clip(creator, None, rank=7.0, approved_at=now, status="posted")
    db.execute(
        "update clips set score = 8 where id = any(%s::uuid[])", ([vod, by_preferred, live, demoted, posted],)
    )

    assert scheduler.refresh_ranking(now) == 4

    assert rank_of(vod) == pytest.approx(4.0)
    assert rank_of(by_preferred) == pytest.approx(8 * 1.1)
    assert rank_of(live) == pytest.approx(8 * 0.5 * 1.3)  # tier S, aged from the spike time
    assert rank_of(demoted) == pytest.approx(8 * 1.3 * 1.5)  # R7 boost kept
    assert rank_of(no_score) == 7.0  # nothing to compute from
    assert rank_of(posted) == 7.0  # only approved clips are ranked
    assert scheduler.refresh_ranking(now) == 0  # nothing changed, nothing written


def test_the_scheduler_delivers_notifications_the_webhook_refused_earlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sixstars import config, notify

    notify.send("info", "held back", "the webhook was not configured")
    assert db.fetch_one("select 1 as x from notify_outbox where sent_at is null") is not None
    monkeypatch.setenv("N8N_NOTIFY_WEBHOOK_URL", "https://n8n.example.test/webhook/notify")
    config.env.cache_clear()

    class Accepted:
        is_success = True
        status_code = 200

    posted: list[str] = []
    monkeypatch.setattr(
        notify.httpx, "post", lambda url, **kwargs: (posted.append(kwargs["json"]["title"]), Accepted())[1]
    )
    assert scheduler.retry_notifications() == 1
    assert posted == ["held back"]
    assert db.fetch_one("select 1 as x from notify_outbox where sent_at is null") is None


def run_service(target: Callable[..., None], name: str) -> None:
    stop = threading.Event()
    thread = threading.Thread(target=target, args=(stop, 0.05), daemon=True)
    thread.start()
    try:
        assert wait_for(
            lambda: db.fetch_one("select 1 as x from heartbeats where name = %s", (name,)) is not None
        )
        time.sleep(0.3)
        assert thread.is_alive()  # it keeps running: compose would restart a service that exits
    finally:
        stop.set()
        thread.join(timeout=60)
    assert not thread.is_alive()


def test_the_scheduler_runs_its_tasks_writes_its_heartbeat_and_stops_on_request(production: None) -> None:
    stale = running_job("worker-dead", 100000)
    run_service(scheduler.run, "scheduler")
    assert status_of(stale) == "queued"  # the first tick already recovered it


def test_the_scheduler_survives_a_task_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "refresh_ranking", broken)
    monkeypatch.setattr(scheduler, "recover_stale_jobs", broken)
    run_service(scheduler.run, "scheduler")


def test_the_live_manager_writes_its_heartbeat_keeps_running_and_stops_on_request() -> None:
    run_service(live_manager.run, "live-manager")
    row = db.fetch_one("select info from heartbeats where name = 'live-manager'")
    assert row is not None and row["info"]["started_at"] and row["info"]["active"] == 0

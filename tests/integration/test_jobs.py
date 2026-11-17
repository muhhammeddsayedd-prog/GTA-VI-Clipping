"""Integration tests for the Postgres job queue (`sixstars.jobs`)."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from sixstars import db, jobs


def _seconds_until_run(job_id: int) -> float:
    row = db.fetch_one("select extract(epoch from run_after - now()) as s from jobs where id = %s", (job_id,))
    assert row is not None
    return float(row["s"])


def _make_runnable(job_id: int) -> None:
    db.execute("update jobs set run_after = now() where id = %s", (job_id,))


def test_claim_in_priority_order() -> None:
    low = jobs.enqueue("normal", "t", {"n": "low"}, priority=1)
    high = jobs.enqueue("normal", "t", {"n": "high"}, priority=5)
    mid = jobs.enqueue("normal", "t", {"n": "mid"}, priority=3)
    claimed = [jobs.claim("normal", "w1") for _ in range(3)]
    assert [job.id for job in claimed if job is not None] == [high, mid, low]
    assert jobs.claim("normal", "w1") is None


def test_claim_same_priority_is_fifo_and_per_queue() -> None:
    first = jobs.enqueue("normal", "t")
    second = jobs.enqueue("normal", "t")
    express = jobs.enqueue("express", "t")
    job = jobs.claim("express", "w1")
    assert job is not None and job.id == express
    assert jobs.claim("express", "w1") is None
    a, b = jobs.claim("normal", "w1"), jobs.claim("normal", "w1")
    assert a is not None and b is not None
    assert (a.id, b.id) == (first, second)


def test_claimed_job_fields() -> None:
    job_id = jobs.enqueue("express", "render", {"spike_id": "abc"}, priority=1, dedupe_key="render:abc")
    job = jobs.claim("express", "worker-x")
    assert job is not None
    assert job.id == job_id
    assert (job.queue, job.type, job.payload) == ("express", "render", {"spike_id": "abc"})
    assert (job.status, job.attempts, job.max_attempts) == ("running", 1, 3)
    assert job.locked_by == "worker-x"
    assert job.locked_at is not None and job.locked_at.utcoffset() is not None
    assert job.dedupe_key == "render:abc"


def test_run_after_delays_a_job() -> None:
    job_id = jobs.enqueue("normal", "t", run_after=datetime.now(UTC) + timedelta(hours=1))
    assert job_id is not None
    assert jobs.claim("normal", "w1") is None
    _make_runnable(job_id)
    assert jobs.claim("normal", "w1") is not None


def test_concurrent_claimers_never_share_a_job() -> None:
    total = 40
    ids = {jobs.enqueue("normal", "t", {"i": i}) for i in range(total)}
    results: dict[str, list[int]] = {"a": [], "b": []}
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def claimer(name: str) -> None:
        try:
            barrier.wait(timeout=10)
            while (job := jobs.claim("normal", name)) is not None:
                results[name].append(job.id)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claimer, args=(name,)) for name in results]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert not set(results["a"]) & set(results["b"])
    assert len(results["a"]) + len(results["b"]) == total
    assert set(results["a"]) | set(results["b"]) == ids
    lockers = db.fetch_all("select id, locked_by from jobs")
    assert {row["id"] for row in lockers if row["locked_by"] == "a"} == set(results["a"])


def test_skip_locked_claim_does_not_wait_for_a_locked_row() -> None:
    first = jobs.enqueue("normal", "t", priority=9)
    second = jobs.enqueue("normal", "t", priority=1)
    with db.pool().connection() as other, other.transaction():
        other.execute("select id from jobs where id = %s for update", (first,))
        job = jobs.claim("normal", "w1")
        assert job is not None and job.id == second


def test_dedupe_key_blocks_until_first_job_finishes() -> None:
    first = jobs.enqueue("normal", "vod", dedupe_key="vod:1")
    assert first is not None
    assert jobs.enqueue("normal", "vod", dedupe_key="vod:1") is None
    job = jobs.claim("normal", "w1")
    assert job is not None
    assert jobs.enqueue("normal", "vod", dedupe_key="vod:1") is None
    jobs.complete(job.id)
    second = jobs.enqueue("normal", "vod", dedupe_key="vod:1")
    assert second is not None and second != first
    assert jobs.enqueue("normal", "vod", dedupe_key="vod:2") is not None
    assert jobs.enqueue("normal", "vod") is not None
    assert jobs.enqueue("normal", "vod") is not None


def test_complete_marks_done() -> None:
    job_id = jobs.enqueue("normal", "t")
    job = jobs.claim("normal", "w1")
    assert job is not None
    jobs.complete(job.id)
    done = jobs.get(job.id)
    assert done is not None and done.status == "done" and done.id == job_id


def test_fail_retries_with_backoff_then_fails() -> None:
    job_id = jobs.enqueue("normal", "t")
    assert job_id is not None

    assert jobs.claim("normal", "w1") is not None
    assert jobs.fail(job_id, "first") == "queued"
    assert _seconds_until_run(job_id) == pytest.approx(30, abs=3)
    assert jobs.claim("normal", "w1") is None

    _make_runnable(job_id)
    assert jobs.claim("normal", "w1") is not None
    assert jobs.fail(job_id, "second") == "queued"
    assert _seconds_until_run(job_id) == pytest.approx(120, abs=3)

    _make_runnable(job_id)
    job = jobs.claim("normal", "w1")
    assert job is not None and job.attempts == 3
    assert jobs.fail(job_id, "third") == "failed"
    final = jobs.get(job_id)
    assert final is not None
    assert (final.status, final.attempts, final.last_error, final.locked_by) == ("failed", 3, "third", None)
    assert jobs.claim("normal", "w1") is None


def test_backoff_reaches_600_s_with_more_attempts() -> None:
    job_id = jobs.enqueue("normal", "t", max_attempts=5)
    assert job_id is not None
    waits = []
    for _ in range(4):
        _make_runnable(job_id)
        assert jobs.claim("normal", "w1") is not None
        assert jobs.fail(job_id, "again") == "queued"
        waits.append(round(_seconds_until_run(job_id) / 10) * 10)
    assert waits == [30, 120, 600, 600]


def test_fail_without_retry_is_final() -> None:
    job_id = jobs.enqueue("normal", "t")
    assert job_id is not None
    assert jobs.claim("normal", "w1") is not None
    assert jobs.fail(job_id, "bad input", retry=False) == "failed"
    assert jobs.fail(job_id, "again") is None


def test_cancel_queued_jobs_only() -> None:
    job_id = jobs.enqueue("normal", "t", dedupe_key="render:1")
    assert jobs.cancel("render:1") == 1
    cancelled = jobs.get(job_id or 0)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert jobs.claim("normal", "w1") is None
    assert jobs.enqueue("normal", "t", dedupe_key="render:1") is not None
    running = jobs.claim("normal", "w1")
    assert running is not None
    assert jobs.cancel("render:1") == 0


def test_run_worker_dispatches_completes_and_fails() -> None:
    stop = threading.Event()
    seen: list[str] = []

    def ok(job: jobs.Job) -> None:
        seen.append(job.payload["n"])

    def boom(job: jobs.Job) -> None:
        raise RuntimeError("handler broke")

    def last(job: jobs.Job) -> None:
        seen.append("last")
        stop.set()

    ok_id = jobs.enqueue("express", "ok", {"n": "one"}, priority=4)
    boom_id = jobs.enqueue("express", "boom", priority=3)
    unknown_id = jobs.enqueue("express", "unknown", priority=2)
    last_id = jobs.enqueue("express", "last", priority=1)

    worker = threading.Thread(
        target=jobs.run_worker,
        args=("express", {"ok": ok, "boom": boom, "last": last}, stop),
        kwargs={"idle_sleep_s": 0.05, "heartbeat_interval_s": 0.05},
    )
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert seen == ["one", "last"]

    states = {row["id"]: row for row in db.fetch_all("select id, status, last_error, attempts from jobs")}
    assert states[ok_id]["status"] == "done"
    assert states[last_id]["status"] == "done"
    assert states[boom_id]["status"] == "queued"
    assert states[boom_id]["last_error"] == "RuntimeError: handler broke"
    assert states[unknown_id]["status"] == "failed"
    assert "no handler" in states[unknown_id]["last_error"]

    beat = db.fetch_one("select name, info from heartbeats")
    assert beat is not None
    assert beat["name"] == jobs.worker_name("express")
    assert beat["name"].startswith("worker-express-")
    assert beat["info"]["queue"] == "express"

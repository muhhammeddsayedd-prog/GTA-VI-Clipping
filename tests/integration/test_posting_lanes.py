"""The hourly and spike lanes against Postgres with the fake Upload-Post client: planning under the lock,
idempotency, dry run, the emergency stop, deferral, demotion and the job handlers."""

import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from posting_support import (
    IG,
    NOW,
    T0,
    TT,
    YT,
    add_clip,
    add_creator,
    add_post,
    add_spike_clip,
    clip_row,
    count,
    event_types,
    fixed,
    go_live,
    posts,
    production,
    video,
)

from sixstars import db, jobs, settings
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.posting.gate import Mode
from sixstars.posting.handlers import make_handlers
from sixstars.posting.hourly import HOURLY_JOB_TYPE, run_hourly_slot, schedule_hourly_slot
from sixstars.posting.models import Action, PostRecord, Reason
from sixstars.posting.publisher import RECOVERY_DELAY_S, Outcome, Publisher
from sixstars.posting.slots import fire_at, slot_start
from sixstars.posting.spike import try_post_spike
from sixstars.posting.store import RETRY_JOB_TYPE, DbStore

__all__ = ["production", "video"]  # fixtures

SPIKE_AT = NOW - timedelta(minutes=4)


def job_rows(job_type: str) -> list[dict[str, object]]:
    return db.fetch_all("select * from jobs where type = %s order by id", (job_type,))


def uploads(client: FakeUploadPostClient) -> list[tuple[str, str]]:
    return sorted((call["platform"], call["idempotency_key"]) for call in client.calls_to("upload_video"))


# ── hourly lane ───────────────────────────────────────────────────────────────────────────────────────────
def test_an_hourly_slot_posts_the_best_clip_to_every_hourly_account(production: None, video: Path) -> None:
    go_live()
    best = add_clip(add_creator("Best"), video, rank=8.0)
    add_clip(add_creator("Other"), video, rank=3.0)
    client = FakeUploadPostClient()

    run = run_hourly_slot(T0, client, clock=fixed())

    assert run.mode is Mode.LIVE and len(run.post_ids) == 3 and run.dry_run_ids == ()
    assert [r.outcome for r in run.results] == [Outcome.PUBLISHED] * 3
    rows = posts()
    assert [(r["account_id"], r["clip"], r["status"], r["dry_run"]) for r in rows] == [
        (IG, best, "published", False),
        (TT, best, "published", False),
        (YT, best, "published", False),
    ]
    assert all(r["slot_at"] == T0 and r["lane"] == "hourly" and r["published_at"] == NOW for r in rows)
    assert all(r["idempotency_key"] == f"post:{best}:{r['account_id']}" for r in rows)
    assert all(r["platform_post_id"] and r["post_url"] and r["attempts"] == 1 for r in rows)
    assert uploads(client) == [
        ("instagram", f"post:{best}:{IG}"),
        ("tiktok", f"post:{best}:{TT}"),
        ("youtube", f"post:{best}:{YT}"),
    ]
    clip = clip_row(best)
    assert clip["status"] == "posted" and clip["posted_at"] == NOW
    types = event_types()
    assert types.count("post.planned") == 3 and types.count("post.published") == 3
    assert types.count("clip.posted") == 1
    recovery = job_rows(RETRY_JOB_TYPE)
    assert len(recovery) == 3
    assert all(j["run_after"] == NOW + timedelta(seconds=RECOVERY_DELAY_S) for j in recovery)


def test_the_same_slot_triggered_twice_makes_one_row_per_account(production: None, video: Path) -> None:
    go_live()
    add_clip(add_creator("Best"), video, rank=8.0)
    add_clip(add_creator("Other"), video, rank=3.0)
    client = FakeUploadPostClient()

    run_hourly_slot(T0, client, clock=fixed())
    again = run_hourly_slot(T0, client, clock=fixed(NOW + timedelta(seconds=40)))

    assert again.post_ids == () and again.results == ()
    assert again.plan is not None
    assert {d.code for d in again.plan.decisions if d.account_id in (IG, TT, YT)} == {
        Reason.SLOT_ALREADY_FILLED
    }
    assert [r["account_id"] for r in posts()] == [IG, TT, YT]
    assert len(client.calls_to("upload_video")) == 3


def test_tiktok_only_posts_in_its_hours_and_the_clip_is_done_after_the_slot(
    production: None, video: Path
) -> None:
    go_live()
    slot = T0 - timedelta(hours=1)  # 13:00 UTC is not one of TikTok's hours (it is one of YouTube's)
    clip = add_clip(add_creator(), video, approved_at=slot - timedelta(minutes=30))

    run = run_hourly_slot(slot, FakeUploadPostClient(), clock=fixed(slot + timedelta(minutes=2)))

    assert run.plan is not None
    codes = {d.account_id: d.code for d in run.plan.decisions}
    assert codes[TT] is Reason.OUTSIDE_HOURLY_HOURS
    assert [r["account_id"] for r in posts()] == [IG, YT]
    assert clip_row(clip)["status"] == "posted"
    assert "posting.slot_skipped" in event_types()


def test_an_empty_queue_is_an_event_and_an_alert_on_the_second_empty_slot(production: None) -> None:
    go_live()
    client = FakeUploadPostClient()

    first = run_hourly_slot(T0, client, clock=fixed())
    assert first.plan is not None and first.plan.slot_empty
    assert event_types().count("posting.slot_empty") == 1 and count("notify_outbox") == 0

    later = T0 + timedelta(hours=1)
    run_hourly_slot(later, client, clock=fixed(later + timedelta(minutes=1)))

    alert = db.fetch_one("select * from notify_outbox")
    assert alert is not None and (alert["severity"], alert["title"]) == ("warn", "Hourly queue empty")
    assert count("posts") == 0 and client.calls == []


def test_a_slot_asked_for_after_its_hour_posts_nothing(production: None, video: Path) -> None:
    go_live()
    add_clip(add_creator(), video)
    client = FakeUploadPostClient()

    run = run_hourly_slot(T0, client, clock=fixed(T0 + timedelta(hours=1, seconds=5)))

    assert run.plan is not None
    assert {d.code for d in run.plan.decisions if d.account_id == IG} == {Reason.SLOT_EXPIRED}
    assert count("posts") == 0 and client.calls == []


def test_the_hourly_lane_yields_to_a_spike_clip_that_is_due(production: None, video: Path) -> None:
    go_live()
    add_clip(add_creator(), video)
    add_spike_clip(add_creator("Live"), video, spike_wall=SPIKE_AT)

    run = run_hourly_slot(T0, FakeUploadPostClient(), clock=fixed())

    assert run.plan is not None
    codes = {d.account_id: d.code for d in run.plan.decisions}
    assert codes[IG] is Reason.SPIKE_PENDING and codes[YT] is Reason.SPIKE_PENDING
    assert [r["account_id"] for r in posts()] == [TT]  # tiktok_main is not a spike account


# ── dry run, before launch, emergency stop ────────────────────────────────────────────────────────────────
def test_a_dry_run_records_rows_and_sends_nothing(production: None, video: Path) -> None:
    clip = add_clip(add_creator(), video)
    client = FakeUploadPostClient()

    run = run_hourly_slot(T0, client, clock=fixed())

    assert run.mode is Mode.DRY and run.post_ids == () and len(run.dry_run_ids) == 3
    rows = posts()
    assert [(r["account_id"], r["status"], r["dry_run"]) for r in rows] == [
        (IG, "skipped", True),
        (TT, "skipped", True),
        (YT, "skipped", True),
    ]
    assert all(r["idempotency_key"].startswith(f"dry:{clip}:") and r["attempts"] == 0 for r in rows)
    assert client.calls == []
    assert clip_row(clip)["status"] == "approved"
    event = db.fetch_one(
        "select * from events where type = 'post.dry_run' and data->>'account_id' = %s", (YT,)
    )
    assert event is not None and event["data"]["would_send"]["privacyStatus"] == "public"
    assert job_rows(RETRY_JOB_TYPE) == []


def test_a_dry_run_slot_triggered_twice_makes_one_dry_row_per_account(production: None, video: Path) -> None:
    add_clip(add_creator("A"), video, rank=8.0)
    add_clip(add_creator("B"), video, rank=3.0)

    run_hourly_slot(T0, FakeUploadPostClient(), clock=fixed())
    run_hourly_slot(T0, FakeUploadPostClient(), clock=fixed(NOW + timedelta(seconds=30)))

    assert [r["account_id"] for r in posts()] == [IG, TT, YT]


def test_before_launch_at_the_slot_is_a_dry_run_even_with_dry_run_false(
    production: None, video: Path
) -> None:
    go_live()
    slot = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)  # one hour before launch.launch_at
    add_clip(add_creator(), video, approved_at=slot - timedelta(hours=1))
    client = FakeUploadPostClient()

    run = run_hourly_slot(slot, client, clock=fixed(slot + timedelta(minutes=59, seconds=59)))

    assert run.mode is Mode.DRY and run.post_ids == ()
    assert {(r["status"], r["dry_run"]) for r in posts()} == {("skipped", True)}
    assert client.calls == []


def test_dry_run_rows_do_not_block_the_real_posts_later(production: None, video: Path) -> None:
    clip = add_clip(add_creator(), video)
    client = FakeUploadPostClient()
    run_hourly_slot(T0, client, clock=fixed())
    assert count("posts") == 3 and client.calls == []

    go_live()
    run = run_hourly_slot(T0, client, clock=fixed(NOW + timedelta(minutes=1)))

    assert len(run.post_ids) == 3 and [r.outcome for r in run.results] == [Outcome.PUBLISHED] * 3
    real = posts("not dry_run")
    assert [(r["account_id"], r["clip"], r["status"]) for r in real] == [
        (IG, clip, "published"),
        (TT, clip, "published"),
        (YT, clip, "published"),
    ]
    assert len(posts("dry_run")) == 3 and len(client.calls_to("upload_video")) == 3


def test_under_emergency_stop_nothing_is_planned_recorded_or_queued(production: None, video: Path) -> None:
    go_live()
    settings.set("control.emergency_stop", True)
    clip = add_clip(add_creator(), video)
    spike = add_spike_clip(add_creator("Live"), video, spike_wall=SPIKE_AT)
    client = FakeUploadPostClient()

    scheduled = schedule_hourly_slot(NOW)
    hourly = run_hourly_slot(T0, client, clock=fixed())
    spiked = try_post_spike(spike, client, clock=fixed())

    assert scheduled.status == "stopped" and scheduled.job_id is None
    assert hourly.mode is Mode.STOPPED and hourly.plan is None
    assert spiked.mode is Mode.STOPPED and spiked.decisions == () and not spiked.demoted
    assert count("posts") == 0 and count("jobs") == 0 and count("events") == 0
    assert client.calls == []
    assert clip_row(clip)["status"] == "approved" and clip_row(spike)["lane"] == "spike"


def test_emergency_stop_between_plan_and_send_cancels_the_row_and_frees_its_key(
    production: None, video: Path
) -> None:
    go_live()
    clip = add_clip(add_creator(), video)
    client = FakeUploadPostClient()

    class StopsAfterPlanning(DbStore):
        """The owner presses stop after the rows were planned and before the first upload."""

        def load_params(self):  # type: ignore[no-untyped-def]
            params = super().load_params()
            if count("posts") > 0:
                db.execute("update settings set value = 'true'::jsonb where key = 'control.emergency_stop'")
                return super().load_params()
            return params

    run = run_hourly_slot(T0, client, store=StopsAfterPlanning(), clock=fixed())

    assert [r.outcome for r in run.results] == [Outcome.CANCELLED] * 3
    assert client.calls == []
    rows = posts()
    assert {(r["status"], r["dry_run"]) for r in rows} == {("skipped", False)}
    assert all(
        r["idempotency_key"] == f"void:{r['post_id']}" and "emergency_stop" in r["error"] for r in rows
    )
    assert clip_row(clip)["status"] == "approved"

    settings.set("control.emergency_stop", False)
    later = run_hourly_slot(T0, client, clock=fixed(NOW + timedelta(minutes=2)))
    assert [r.outcome for r in later.results] == [Outcome.PUBLISHED] * 3


# ── the trigger and the job handlers ──────────────────────────────────────────────────────────────────────
def test_the_trigger_queues_one_job_at_the_jittered_time_and_records_the_heartbeat(production: None) -> None:
    first = schedule_hourly_slot(NOW)
    second = schedule_hourly_slot(NOW + timedelta(seconds=20))

    assert (first.status, second.status) == ("scheduled", "already_scheduled")
    assert first.slot_at == T0 and first.fire_at == fire_at(T0, 300)
    assert T0 <= first.fire_at <= T0 + timedelta(seconds=300)
    (job,) = job_rows(HOURLY_JOB_TYPE)
    assert job["id"] == first.job_id and job["queue"] == "express" and job["run_after"] == first.fire_at
    assert job["payload"] == {"slot_at": T0.isoformat()}
    heartbeat = db.fetch_one("select * from heartbeats where name = 'trigger:hourly'")
    assert heartbeat is not None and heartbeat["info"] == {"slot_at": T0.isoformat()}


def launched_now() -> datetime:
    """Make posting live at the real current time, for handlers that read the real clock."""
    go_live()
    settings.set("launch.launch_at", "2026-01-01T00:00:00Z")
    db.execute("update platform_accounts set hourly_hours_utc = null")
    return datetime.now(UTC)


def test_the_hourly_job_handler_runs_the_slot_with_the_given_client(production: None, video: Path) -> None:
    now = launched_now()
    clip = add_clip(add_creator(), video, approved_at=now - timedelta(hours=1))
    client = FakeUploadPostClient()
    handlers = make_handlers(lambda: client)
    job_id = jobs.enqueue("express", HOURLY_JOB_TYPE, {"slot_at": slot_start(now).isoformat()})
    assert job_id is not None
    job = jobs.get(job_id)
    assert job is not None

    handlers[HOURLY_JOB_TYPE](job)

    if slot_start(datetime.now(UTC)) == slot_start(now):  # not across the top of the hour
        assert {(r["clip"], r["status"]) for r in posts()} == {(clip, "published")}
        assert len(client.calls_to("upload_video")) == 3


def test_the_retry_handler_sends_a_planned_post_whose_worker_died(production: None, video: Path) -> None:
    now = launched_now()
    clip = add_clip(add_creator(), video, approved_at=now - timedelta(hours=1))
    post_id = add_post(clip, YT, now, status="pending", slot_at=slot_start(now), key=f"post:{clip}:{YT}")
    client = FakeUploadPostClient()
    handlers = make_handlers(lambda: client)
    job_id = jobs.enqueue("express", RETRY_JOB_TYPE, {"kind": "publish", "post_id": post_id, "clip_id": clip})
    assert job_id is not None
    job = jobs.get(job_id)
    assert job is not None

    handlers[RETRY_JOB_TYPE](job)
    handlers[RETRY_JOB_TYPE](job)

    if slot_start(datetime.now(UTC)) == slot_start(now):
        (row,) = posts()
        assert row["status"] == "published" and row["upload_request_id"] == f"post:{clip}:{YT}"
        assert len(client.calls_to("upload_video")) == 1
        assert clip_row(clip)["status"] == "posted"


def test_the_retry_handler_refuses_an_unknown_kind(production: None) -> None:
    handlers = make_handlers(FakeUploadPostClient)
    job_id = jobs.enqueue("express", RETRY_JOB_TYPE, {"kind": "something"})
    assert job_id is not None
    job = jobs.get(job_id)
    assert job is not None

    with pytest.raises(ValueError, match="unknown post.retry kind"):
        handlers[RETRY_JOB_TYPE](job)


# ── spike lane ────────────────────────────────────────────────────────────────────────────────────────────
def test_a_spike_clip_posts_at_once_on_every_spike_account(production: None, video: Path) -> None:
    go_live()
    spike = add_spike_clip(add_creator("Live"), video, spike_wall=SPIKE_AT)
    client = FakeUploadPostClient()

    run = try_post_spike(spike, client, clock=fixed())

    assert [r.outcome for r in run.results] == [Outcome.PUBLISHED] * 2 and run.posted
    rows = posts()
    assert [(r["account_id"], r["lane"], r["status"], r["slot_at"]) for r in rows] == [
        (IG, "spike", "published", None),
        (YT, "spike", "published", None),
    ]
    codes = {d.account_id: d.code for d in run.decisions}
    assert codes[TT] is Reason.LANE_NOT_ALLOWED
    assert clip_row(spike)["status"] == "posted"
    spike_row = db.fetch_one("select spike_to_post_s from spikes")
    assert spike_row == {"spike_to_post_s": 240}

    again = try_post_spike(spike, client, clock=fixed(NOW + timedelta(seconds=30)))
    assert again.post_ids == () and len(client.calls_to("upload_video")) == 2
    assert count("posts") == 2 and clip_row(spike)["lane"] == "spike"


def test_a_spike_waits_for_the_minimum_gap_through_a_retry_job(production: None, video: Path) -> None:
    go_live()
    old = add_clip(add_creator("Earlier"), video, status="posted")
    add_post(old, IG, NOW - timedelta(minutes=5))
    spike = add_spike_clip(add_creator("Live"), video, spike_wall=SPIKE_AT)
    client = FakeUploadPostClient()
    gap_end = NOW + timedelta(minutes=10)

    run = try_post_spike(spike, client, clock=fixed())

    by_account = {d.account_id: d for d in run.decisions}
    assert by_account[IG].action is Action.DEFER and by_account[IG].code is Reason.MIN_GAP_WAIT
    assert run.deferred_until == gap_end and not run.posted and not run.demoted
    assert [(r["account_id"], r["status"]) for r in posts("lane = 'spike'")] == [(YT, "published")]
    assert clip_row(spike)["status"] == "approved"  # still waiting for instagram
    (job,) = [j for j in job_rows(RETRY_JOB_TYPE) if j["payload"]["kind"] == "spike"]  # type: ignore[index]
    assert (
        job["run_after"] == gap_end + timedelta(seconds=1)
        and job["queue"] == "express"
        and job["priority"] == 90
    )
    assert job["payload"] == {"kind": "spike", "clip_id": spike}

    db.execute("update jobs set status = 'running' where id = %s", (job["id"],))
    retried = try_post_spike(spike, client, clock=fixed(gap_end), job_id=int(str(job["id"])))

    assert [r.outcome for r in retried.results] == [Outcome.PUBLISHED] and retried.posted
    assert [(r["account_id"], r["status"]) for r in posts("lane = 'spike'")] == [
        (IG, "published"),
        (YT, "published"),
    ]
    assert clip_row(spike)["status"] == "posted"
    assert db.fetch_one("select spike_to_post_s from spikes") == {"spike_to_post_s": 240}


def test_a_spike_that_is_too_old_moves_to_the_hourly_lane(production: None, video: Path) -> None:
    go_live()
    spike = add_spike_clip(add_creator("Live"), video, spike_wall=NOW - timedelta(minutes=31), rank=6.0)
    client = FakeUploadPostClient()

    run = try_post_spike(spike, client, clock=fixed())

    assert run.demoted and run.post_ids == () and client.calls == []
    assert {d.code for d in run.decisions if d.account_id in (IG, YT)} == {Reason.SPIKE_TOO_OLD}
    clip = clip_row(spike)
    assert (clip["lane"], float(clip["rank_score"]), clip["status"]) == ("hourly", 9.0, "approved")
    assert "clip.demoted" in event_types() and count("posts") == 0


def test_a_spike_in_dry_run_is_recorded_once_and_not_demoted(production: None, video: Path) -> None:
    spike = add_spike_clip(add_creator("Live"), video, spike_wall=SPIKE_AT)
    client = FakeUploadPostClient()

    first = try_post_spike(spike, client, clock=fixed())
    second = try_post_spike(spike, client, clock=fixed(NOW + timedelta(seconds=30)))

    assert first.mode is Mode.DRY and len(first.dry_run_ids) == 2 and second.dry_run_ids == ()
    assert [(r["account_id"], r["status"], r["dry_run"]) for r in posts()] == [
        (IG, "skipped", True),
        (YT, "skipped", True),
    ]
    assert client.calls == [] and not second.demoted
    assert clip_row(spike)["lane"] == "spike" and clip_row(spike)["status"] == "approved"
    assert db.fetch_one("select spike_to_post_s from spikes") == {"spike_to_post_s": None}


def test_two_spikes_ready_together_post_in_rank_order_and_the_second_waits(
    production: None, video: Path
) -> None:
    go_live()
    low = add_spike_clip(add_creator("Low"), video, spike_wall=SPIKE_AT, rank=5.0)
    high = add_spike_clip(add_creator("High"), video, spike_wall=SPIKE_AT, rank=9.0)
    client = FakeUploadPostClient()

    run = try_post_spike(low, client, clock=fixed())  # the call for the weaker clip arrives first

    assert {r["clip"] for r in posts()} == {high}
    assert {d.action for d in run.decisions if d.account_id in (IG, YT)} == {Action.DEFER}
    assert clip_row(high)["status"] == "posted" and clip_row(low)["status"] == "approved"
    waiting = [j for j in job_rows(RETRY_JOB_TYPE) if j["payload"]["kind"] == "spike"]  # type: ignore[index]
    assert [j["payload"]["clip_id"] for j in waiting] == [low]  # type: ignore[index]


def test_the_holding_window_posts_the_best_spike_of_the_batch(production: None, video: Path) -> None:
    # R12: a 7 is approved first; a 10 is approved 5 minutes later, inside the 7-minute hold. Nothing
    # posts before the hold ends; then the 10 takes the spike post and the 7 moves to the hourly queue.
    go_live()
    settings.set("posting.spike_hold_s", 420)
    hold_end = NOW + timedelta(minutes=7)
    early = add_spike_clip(add_creator("Early"), video, spike_wall=SPIKE_AT, rank=7.0)
    client = FakeUploadPostClient()

    held = try_post_spike(early, client, clock=fixed())

    assert held.post_ids == () and client.calls == [] and not held.demoted
    assert {d.code for d in held.decisions if d.account_id in (IG, YT)} == {Reason.SPIKE_HOLD}
    assert held.deferred_until == hold_end
    (job,) = [j for j in job_rows(RETRY_JOB_TYPE) if j["payload"]["kind"] == "spike"]  # type: ignore[index]
    assert job["run_after"] == hold_end + timedelta(seconds=1)

    late = add_spike_clip(add_creator("Late"), video, spike_wall=NOW + timedelta(minutes=1), rank=10.0)
    db.execute("update clips set approved_at = %s where id = %s", (NOW + timedelta(minutes=5), late))
    assert try_post_spike(late, client, clock=fixed(NOW + timedelta(minutes=5))).post_ids == ()

    released = try_post_spike(early, client, clock=fixed(hold_end))

    assert {r["clip"] for r in posts("lane = 'spike'")} == {late}
    assert released.demoted and clip_row(early)["lane"] == "hourly"
    # The 10's own retry job (queued for the end of the hold) finds it published and settles it.
    (late_job,) = [
        j
        for j in job_rows(RETRY_JOB_TYPE)
        if j["payload"].get("kind") == "spike"  # type: ignore[attr-defined]
        and j["payload"].get("clip_id") == late  # type: ignore[attr-defined]
        and j["status"] == "queued"
    ]
    db.execute("update jobs set status = 'running' where id = %s", (late_job["id"],))
    settled = try_post_spike(late, client, clock=fixed(hold_end), job_id=int(str(late_job["id"])))
    assert settled.posted and clip_row(late)["status"] == "posted"
    assert len(client.calls_to("upload_video")) == 2  # the 10 went out once per spike account


def test_an_unknown_spike_clip_is_reported_and_nothing_happens(production: None) -> None:
    go_live()
    run = try_post_spike("00000000-0000-0000-0000-000000000000", FakeUploadPostClient(), clock=fixed())

    assert run.known is False and count("posts") == 0


# ── the lock ──────────────────────────────────────────────────────────────────────────────────────────────
class PausesWhilePlanning(DbStore):
    """Stops inside the planning lock, after the history was read, until the test lets it go on."""

    def __init__(self) -> None:
        self.loaded = threading.Event()
        self.go_on = threading.Event()

    def load_history(self, now: datetime, clip_ids: Iterable[str] = ()) -> list[PostRecord]:
        history = super().load_history(now, clip_ids)
        self.loaded.set()
        assert self.go_on.wait(timeout=60)
        return history


def test_two_planners_at_once_cannot_both_take_the_last_free_post(production: None, video: Path) -> None:
    """Instagram has one free post left. The hourly planner has read the history (no spike in sight) when
    a spike clip is approved and the spike planner starts. Without the lock both would see a free post."""
    go_live()
    db.execute("update platform_accounts set enabled = (id = %s), cap_24h = 1, spike_reserve = 0", (IG,))
    hourly_clip = add_clip(add_creator("Hourly"), video)
    slow_store = PausesWhilePlanning()
    client = FakeUploadPostClient()
    errors: list[BaseException] = []
    spike_done = threading.Event()

    def guarded(work):  # type: ignore[no-untyped-def]
        def run() -> None:
            try:
                work()
            except BaseException as exc:  # reported by the main thread
                errors.append(exc)

        return run

    hourly_thread = threading.Thread(
        target=guarded(lambda: run_hourly_slot(T0, client, store=slow_store, clock=fixed()))
    )
    hourly_thread.start()
    assert slow_store.loaded.wait(timeout=60)

    spike = add_spike_clip(add_creator("Live"), video, spike_wall=SPIKE_AT)

    def spike_work() -> None:
        try:
            try_post_spike(spike, client, clock=fixed(NOW + timedelta(seconds=1)))
        finally:
            spike_done.set()

    spike_thread = threading.Thread(target=guarded(spike_work))
    spike_thread.start()
    blocked = not spike_done.wait(timeout=3)  # the spike planner must be waiting for the lock
    slow_store.go_on.set()
    hourly_thread.join(timeout=120)
    spike_thread.join(timeout=120)

    assert errors == []
    assert blocked, "the spike planner did not wait for the hourly planner"
    counting = posts("not dry_run and status <> 'skipped'")
    assert [(r["account_id"], r["clip"], r["lane"]) for r in counting] == [(IG, hourly_clip, "hourly")]
    assert len(client.calls_to("upload_video")) == 1
    skipped = db.fetch_one("select data from events where type = 'posting.spike_skipped'")
    assert skipped is not None and skipped["data"]["code"] == "at_cap"


def test_a_planner_that_fails_leaves_no_rows_behind(production: None, video: Path) -> None:
    go_live()
    add_clip(add_creator(), video)

    class Breaks(DbStore):
        def schedule_publish_retry(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_hourly_slot(T0, FakeUploadPostClient(), store=Breaks(), clock=fixed())

    assert count("posts") == 0 and count("events") == 0 and count("jobs") == 0


# ── publisher on the database ─────────────────────────────────────────────────────────────────────────────
def test_publishing_the_same_post_twice_creates_one_post(production: None, video: Path) -> None:
    go_live()
    clip = add_clip(add_creator(), video)
    post_id = add_post(clip, YT, NOW, status="pending", slot_at=T0, key=f"post:{clip}:{YT}")
    client = FakeUploadPostClient()
    publisher = Publisher(client, DbStore(), fixed())

    assert publisher.publish(post_id).outcome is Outcome.PUBLISHED
    assert publisher.publish(post_id).outcome is Outcome.NOOP

    assert len(client.calls_to("upload_video")) == 1 and len(client.uploads) == 1
    (row,) = posts()
    assert row["status"] == "published" and row["attempts"] == 1

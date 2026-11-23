"""Health checks, failover and housekeeping against Postgres: one alert per incident and one per
recovery, the scheduler running a missing n8n trigger itself, clip expiry, stuck jobs, the spend pause."""

from datetime import UTC, datetime, timedelta
from typing import Any

from guardian_support import creator, event_types, outbox, row, setting, titles, world
from guardian_support import db_now as now
from posting_support import add_clip
from psycopg.types.json import Jsonb

from sixstars import db, jobs, settings, usage
from sixstars.guardian import config as guardian_config
from sixstars.guardian import failover, health, incidents
from sixstars.services import scheduler

__all__ = ["world"]  # fixture

EXPECTED = ["scheduler", "worker-express", "worker-normal", "live-manager"]


def beat(name: str, age_s: float = 0, info: dict[str, Any] | None = None) -> None:
    db.execute(
        """
        insert into heartbeats (name, last_seen, info) values (%s, now() - make_interval(secs => %s), %s)
        on conflict (name) do update set last_seen = excluded.last_seen, info = excluded.info
        """,
        (name, age_s, Jsonb(info) if info is not None else None),
    )


def all_alive() -> None:
    for name in ("scheduler", "worker-express-host-1", "worker-normal-host-2", "live-manager"):
        beat(name)


def test_a_stale_service_alerts_once_and_its_recovery_once(world: None) -> None:
    all_alive()
    beat("worker-normal-host-2", 301)
    open_now = incidents.open_keys()
    for _ in range(3):  # three health passes while it is down
        health.check_services(now(), open_now)
    assert titles() == ["Service not responding: worker-normal"]
    assert incidents.open_keys() == {"heartbeat:worker-normal"}

    beat("worker-normal-host-9")  # a new process of the same service
    for _ in range(3):
        health.check_services(now(), incidents.open_keys())
    assert titles() == ["Service not responding: worker-normal", "Service is back: worker-normal"]
    assert incidents.open_keys() == set()
    assert event_types("health.") == ["health.incident", "health.recovered"]

    beat("worker-normal-host-9", 5000)
    beat("worker-normal-host-2", 5000)
    health.check_services(now(), incidents.open_keys())
    assert event_types("health.") == ["health.incident", "health.recovered", "health.incident"]


def test_a_service_that_never_started_is_reported(world: None) -> None:
    beat("scheduler")
    health.check_services(now(), incidents.open_keys())
    assert incidents.open_keys() == {
        "heartbeat:worker-express",
        "heartbeat:worker-normal",
        "heartbeat:live-manager",
    }
    assert setting("guardian.expected_services") == EXPECTED


def test_a_restart_storm_is_critical(world: None) -> None:
    all_alive()
    for index, age in enumerate([400, 900, 1500]):
        beat(f"worker-express-crashed-{index}", age)
    health.check_services(now(), incidents.open_keys())
    assert ("critical", "Restart storm: worker-express") in [(level, title) for level, title, _ in outbox()]
    assert "3 restarts" in outbox()[0][2]


def test_the_whole_health_pass_survives_a_check_that_fails(world: None, monkeypatch: Any) -> None:
    all_alive()

    def broken(open_now: set[str]) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(health, "check_queue", broken)
    summary = health.run_checks(now())
    assert "queue" not in summary and summary["services"] == [] and "spend_over" in summary


def test_a_low_queue_and_slow_spike_posts_warn_once(world: None) -> None:
    someone = creator("Someone")
    for index, latency in enumerate([500, 400, 361]):
        db.execute(
            "insert into spikes (mode, creator_id, tier, spike_to_post_s, detected_at) "
            "values ('live', %s, 'S', %s, now() - make_interval(mins => %s))",
            (someone, latency, index),
        )
    for _ in range(2):
        open_now = incidents.open_keys()
        assert health.check_queue(open_now) == 0
        assert health.check_express(open_now) is True
    assert titles("warn") == ["Clip queue is low", "Spike posts are slow"]

    for _ in range(12):
        add_clip(someone, None, status="approved")
    db.execute(
        "insert into spikes (mode, creator_id, tier, spike_to_post_s, detected_at) "
        "values ('live', %s, 'S', 120, now())",
        (someone,),
    )
    open_now = incidents.open_keys()
    assert health.check_queue(open_now) == 12 and health.check_express(open_now) is False
    assert titles("info") == ["Clip queue is filled again", "Spike posts are fast again"]


def test_over_budget_pauses_the_vod_lane_until_the_spend_is_back_under(world: None) -> None:
    usage.record("anthropic", "clip_writer", cost_usd=24.0)
    at = datetime.now(UTC)
    assert health.check_spend(at, incidents.open_keys()) == {
        "anthropic": False,
        "transcription": False,
        "apify": False,
        "proxy": False,
    }
    assert setting("vod.paused_until") is None

    usage.record("anthropic", "clip_writer", cost_usd=1.5)
    usage.record("apify", "youtube_search", cost_usd=0.5)
    over = health.check_spend(at, incidents.open_keys())
    assert over == {"anthropic": True, "transcription": False, "apify": True, "proxy": False}
    paused_until = settings.get_dt("vod.paused_until")
    assert paused_until is not None and at < paused_until <= at + timedelta(hours=1)
    assert sorted(titles("warn")) == ["Daily budget reached: anthropic", "Daily budget reached: apify"]
    assert "VOD lane is paused" in outbox()[0][2] and event_types("vod.") == ["vod.paused"]

    health.check_spend(at, incidents.open_keys())  # still over: no second alert, no second event
    assert len(titles("warn")) == 2 and event_types("vod.") == ["vod.paused"]


def test_a_proxy_budget_of_zero_means_no_limit(world: None) -> None:
    """`vod/proxy.budget_reached` reads 0 as no limit; the guardian raises no daily incident for it."""
    settings.set("proxy.daily_budget_usd", 0)
    usage.record("DataImpulse", "download", cost_usd=4.0)
    over = health.check_spend(datetime.now(UTC), incidents.open_keys())
    assert over["proxy"] is False
    assert titles("warn") == []
    settings.set("proxy.daily_budget_usd", 3)
    assert health.check_spend(datetime.now(UTC), incidents.open_keys())["proxy"] is True


def test_the_emergency_stop_is_not_touched_by_health_checks(world: None) -> None:
    settings.set("control.emergency_stop", True)
    all_alive()
    health.run_checks(now())
    assert setting("control.emergency_stop") is True


# ── failover ──────────────────────────────────────────────────────────────────────────────────────────────


def runner_log(log: list[str], name: str) -> failover.Runner:
    def run(at: datetime) -> dict[str, Any] | None:
        log.append(name)
        return None

    return run


def check(runner: failover.Failover, at: datetime | None = None) -> list[str]:
    return runner.check(
        at or now(),
        guardian_config.seconds_map("guardian.failover_grace_s"),
        guardian_config.seconds_map("guardian.failover_interval_s"),
        settings.get_bool("guardian.failover_enabled"),
        wait=True,
    )


def test_the_failover_settings_cover_all_four_triggers(world: None) -> None:
    assert guardian_config.seconds_map("guardian.failover_grace_s") == {
        "hourly": 4200.0,
        "discovery": 1500.0,
        "guardian": 2700.0,
        "stats": 14400.0,
    }


def test_when_n8n_misses_the_hourly_trigger_the_scheduler_posts_the_slot_itself(world: None) -> None:
    for name in ("discovery", "guardian", "stats"):
        beat(f"trigger:{name}")
    beat("trigger:hourly", 75 * 60, {"slot_at": "old"})  # WF02 stopped 75 minutes ago
    runner = failover.Failover({"hourly": scheduler._failover_hourly}, now())

    assert check(runner) == ["hourly"]
    queued = db.fetch_all("select type, queue, payload from jobs")
    assert [(job["type"], job["queue"]) for job in queued] == [("post.hourly", "express")]
    assert titles("critical") == ["n8n trigger missing: hourly"]
    assert "the scheduler runs it instead" in outbox()[0][2]
    stamp = row_of_trigger("hourly")
    assert (
        stamp["info"]["source"] == "failover" and stamp["info"]["slot_at"] == queued[0]["payload"]["slot_at"]
    )

    # Later passes in the same slot: no second job, no second alert.
    assert check(runner) == [] and check(runner) == []
    assert len(db.fetch_all("select 1 from jobs")) == 1 and len(titles("critical")) == 1

    # n8n calls again: one recovery message, and the scheduler stands down.
    beat("trigger:hourly", 0, {"slot_at": "from n8n"})
    assert check(runner) == []
    assert titles("critical") == ["n8n trigger missing: hourly", "n8n trigger is back: hourly"]
    assert incidents.open_keys() == set()


def test_the_other_triggers_are_run_by_the_scheduler_and_repeated_while_n8n_is_silent(world: None) -> None:
    beat("trigger:hourly")
    beat("trigger:discovery", 1501)
    beat("trigger:guardian", 2701)
    beat("trigger:stats", 100)
    log: list[str] = []
    runner = failover.Failover(
        {name: runner_log(log, name) for name in ("discovery", "guardian", "stats")}, now()
    )

    assert check(runner) == ["discovery", "guardian"] and log == ["discovery", "guardian"]
    assert check(runner) == []  # just ran
    assert check(runner, now() + timedelta(seconds=601)) == ["discovery"]  # the discovery interval is 600 s
    assert sorted(titles("critical")) == ["n8n trigger missing: discovery", "n8n trigger missing: guardian"]


def test_failover_can_be_switched_off_and_then_only_alerts(world: None) -> None:
    settings.set("guardian.failover_enabled", False)
    for name in ("discovery", "guardian", "stats"):
        beat(f"trigger:{name}")
    beat("trigger:hourly", 5000)
    log: list[str] = []
    runner = failover.Failover({"hourly": runner_log(log, "hourly")}, now())
    assert check(runner) == [] and log == []
    assert titles("critical") == ["n8n trigger missing: hourly"] and "nothing runs it" in outbox()[0][2]


def test_a_failing_failover_job_is_tried_again_and_does_not_raise(world: None) -> None:
    for name in ("hourly", "guardian", "stats"):
        beat(f"trigger:{name}")
    beat("trigger:discovery", 2000)

    def broken(at: datetime) -> None:
        raise RuntimeError("discovery exploded")

    runner = failover.Failover({"discovery": broken}, now())
    assert check(runner) == ["discovery"]
    assert check(runner, now() + timedelta(seconds=700)) == ["discovery"]


def row_of_trigger(name: str) -> dict[str, Any]:
    found = db.fetch_one("select * from heartbeats where name = %s", (f"trigger:{name}",))
    assert found is not None
    return found


# ── housekeeping ──────────────────────────────────────────────────────────────────────────────────────────


def test_approved_clips_older_than_the_limit_expire(world: None) -> None:
    someone = creator("Someone")
    at = now()
    old = add_clip(someone, None, status="approved", approved_at=at - timedelta(hours=72, minutes=1))
    fresh = add_clip(someone, None, status="approved", approved_at=at - timedelta(hours=71, minutes=59))
    posted = add_clip(someone, None, status="posted", approved_at=at - timedelta(hours=200))

    assert health.expire_clips(at) == 1
    assert [row("clips", clip)["status"] for clip in (old, fresh, posted)] == [
        "expired",
        "approved",
        "posted",
    ]
    assert event_types("clip.") == ["clip.expired"]
    assert health.expire_clips(at) == 0


def test_a_job_that_hangs_on_a_live_worker_is_reset_and_fails_when_out_of_attempts(world: None) -> None:
    def running(age_s: int, attempts: int) -> int:
        job_id = jobs.enqueue("normal", "vod.process", {"n": age_s + attempts})
        assert job_id is not None
        db.execute(
            "update jobs set status = 'running', locked_by = 'worker-normal-alive-1', attempts = %s, "
            "locked_at = now() - make_interval(secs => %s) where id = %s",
            (attempts, age_s, job_id),
        )
        return job_id

    beat("worker-normal-alive-1")  # the worker is alive, so `requeue_stale` leaves these alone
    hung = running(7300, 1)
    hopeless = running(7300, 3)
    busy = running(7100, 1)
    assert jobs.requeue_stale(300) == []

    assert health.reset_stuck_jobs() == 2
    assert [row("jobs", str(job))["status"] for job in (hung, hopeless, busy)] == [
        "queued",
        "failed",
        "running",
    ]
    assert event_types("job.") == ["job.stuck_reset", "job.stuck_reset"]
    assert titles("warn") == ["Stuck jobs reset"]


def test_old_events_and_candidates_are_pruned_but_incident_state_is_kept(world: None) -> None:
    someone = creator("Someone")
    db.execute(
        "insert into events (type, created_at, entity_type, entity_id) values "
        "('post.published', now() - interval '31 days', null, null), "
        "('post.published', now() - interval '29 days', null, null), "
        "('health.incident', now() - interval '40 days', 'incident', 'disk')"
    )
    db.execute(
        "insert into candidates "
        "(creator_id, source_type, platform, source_id, url, trend_score, checked_at) values "
        "(%s, 'vod', 'youtube', 'a', 'https://x.test/a', 1, now() - interval '31 days'), "
        "(%s, 'vod', 'youtube', 'b', 'https://x.test/b', 1, now())",
        (someone, someone),
    )
    assert health.prune(now()) == {"events": 1, "candidates": 1}
    assert event_types() == ["post.published", "health.incident"] and incidents.open_keys() == {"disk"}


def test_the_launch_summary_is_sent_once_in_the_last_minutes_before_launch(world: None) -> None:
    launch_at = settings.get_dt("launch.launch_at")
    assert launch_at is not None
    assert scheduler.launch_summary(launch_at - timedelta(minutes=16)) is False
    assert scheduler.launch_summary(launch_at - timedelta(minutes=14)) is True
    assert scheduler.launch_summary(launch_at - timedelta(minutes=10)) is False  # only once
    assert scheduler.launch_summary(launch_at + timedelta(minutes=1)) is False
    (message,) = outbox()
    assert message[0] == "info" and message[1].startswith("Health summary before launch")
    assert "Posting:" in message[2] and "<b>" not in message[2]

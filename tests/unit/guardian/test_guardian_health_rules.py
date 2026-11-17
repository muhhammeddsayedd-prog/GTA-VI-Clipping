"""Health and failover rules as pure functions: stale heartbeats, restart storms, the once-per-incident
rule, the express latency rule, the spend pause, and when the scheduler runs a missing n8n trigger."""

from datetime import UTC, datetime, timedelta

import pytest
from guardian_helpers import NOW

from sixstars.guardian import failover, health, incidents
from sixstars.guardian.failover import TriggerState
from sixstars.guardian.health import Beat

STARTED = NOW - timedelta(hours=6)


def beat(name: str, age_s: float) -> Beat:
    return Beat(name, NOW - timedelta(seconds=age_s))


# ── heartbeats ────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "service"),
    [
        ("worker-express-host1-123", "worker-express"),
        ("worker-normal-9f3c2a-77", "worker-normal"),
        ("worker-express", "worker-express"),
        ("worker-expressway-1", "worker-expressway-1"),  # not the express queue
        ("scheduler", "scheduler"),
        ("live-manager", "live-manager"),
    ],
)
def test_the_service_of_a_heartbeat(name: str, service: str) -> None:
    assert health.service_of(name) == service


@pytest.mark.parametrize(
    ("age_s", "stale"),
    [(0, False), (299, False), (300, False), (300.5, True), (86_400, True), (None, True)],
)
def test_a_heartbeat_is_stale_after_the_limit_or_when_it_was_never_seen(
    age_s: float | None, stale: bool
) -> None:
    assert health.is_stale(age_s, 300) is stale


def test_a_worker_service_is_alive_when_any_of_its_processes_is() -> None:
    beats = [
        beat("worker-express-old-1", 5000),  # a process of an earlier deploy
        beat("worker-express-new-2", 20),
        beat("worker-normal-old-1", 5000),
        beat("scheduler", 10),
    ]
    expected = ["scheduler", "worker-express", "worker-normal", "live-manager"]
    result = {item.service: item for item in health.service_health(beats, expected, NOW, 300, 3600)}
    assert [name for name, item in result.items() if item.stale] == ["worker-normal", "live-manager"]
    assert result["worker-express"].age_s == 20
    assert result["live-manager"].age_s is None
    assert result["worker-normal"].age_s == 5000


@pytest.mark.parametrize(
    ("ages", "restarts"),
    [
        ([10], 0),  # one process, running
        ([10, 20], 0),  # two replicas, both running
        ([10, 400, 900, 1500], 3),  # one running, three died within the hour
        ([10, 20, 400], 1),
        ([400, 900, 1500], 2),  # all dead: the first start is not a restart
        ([10, 4000, 5000], 0),  # older than the window
        ([], 0),
    ],
)
def test_restarts_are_the_processes_seen_in_the_window_beyond_those_alive(
    ages: list[float], restarts: int
) -> None:
    beats = [beat(f"worker-express-h-{index}", age) for index, age in enumerate(ages)]
    (result,) = health.service_health(beats, ["worker-express"], NOW, 300, 3600)
    assert result.restarts == restarts


# ── once per incident ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("was_open", "failing", "change"),
    [(False, True, "opened"), (True, True, "none"), (True, False, "closed"), (False, False, "none")],
)
def test_an_alert_is_sent_only_when_the_state_changes(was_open: bool, failing: bool, change: str) -> None:
    assert incidents.transition(was_open, failing) == change


def test_a_flapping_check_alerts_once_per_incident_and_once_per_recovery() -> None:
    open_now = False
    changes = []
    for failing in [False, True, True, True, False, False, True, False]:
        change = incidents.transition(open_now, failing)
        open_now = failing if change != "none" else open_now
        changes.append(change)
    assert changes == ["none", "opened", "none", "none", "closed", "none", "opened", "closed"]


# ── other health rules ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("latencies", "slow"),
    [
        ([400, 500, 361], True),  # the last three all over 360 s
        ([400, 500, 360], False),  # exactly the limit is not slow
        ([400, 100, 500], False),
        ([400, 500], False),  # fewer than three posts so far
        ([], False),
        ([100, 500, 500, 500], False),  # newest first: the newest was fast
        ([500, 500, 500, 100], True),
    ],
)
def test_the_express_latency_rule(latencies: list[float], slow: bool) -> None:
    assert health.express_slow(latencies, 360, 3) is slow


def test_a_spend_pause_never_runs_past_midnight_utc() -> None:
    assert health.pause_until(NOW, 3600) == NOW + timedelta(hours=1)
    late = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)
    assert health.pause_until(late, 3600) == datetime(2026, 11, 21, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(("total", "used", "pct"), [(100, 81, 81.0), (0, 0, 0.0), (200, 50, 25.0)])
def test_disk_percentage(total: int, used: int, pct: float) -> None:
    assert health.disk_used_pct(total, used) == pct


# ── failover ──────────────────────────────────────────────────────────────────────────────────────────────


def state(name: str, age_s: float | None, **info: object) -> TriggerState:
    last_seen = None if age_s is None else NOW - timedelta(seconds=age_s)
    return TriggerState(name, last_seen, dict(info))


@pytest.mark.parametrize(
    ("trigger", "missing"),
    [
        (state("hourly", 60), False),
        (state("hourly", 4200), False),  # exactly the grace time: not yet
        (state("hourly", 4201), True),
        (state("hourly", 10, source="failover"), True),  # fresh, but written by the failover itself
        (state("hourly", None), True),  # never fired, and the scheduler is up for 6 hours
    ],
)
def test_when_an_n8n_trigger_counts_as_missing(trigger: TriggerState, missing: bool) -> None:
    assert failover.trigger_missing(trigger, NOW, 4200, STARTED) is missing


def test_a_trigger_that_never_fired_gets_the_grace_time_from_the_scheduler_start() -> None:
    never = state("hourly", None)
    assert failover.trigger_missing(never, NOW, 4200, NOW - timedelta(seconds=4200)) is False
    assert failover.trigger_missing(never, NOW, 4200, NOW - timedelta(seconds=4201)) is True


def test_stopping_n8n_for_75_minutes_makes_the_scheduler_run_the_hourly_slot() -> None:
    # n8n last called at 12:00; 70 minutes later nothing has happened yet, after 75 minutes it has.
    last_call = datetime(2026, 11, 20, 12, 0, 5, tzinfo=UTC)
    n8n = TriggerState("hourly", last_call, {"slot_at": "2026-11-20T12:00:00+00:00"})
    at_70 = last_call + timedelta(minutes=69, seconds=59)
    at_75 = last_call + timedelta(minutes=75)
    assert failover.failover_due(n8n, at_70, 4200, 3600, STARTED) is False
    assert failover.failover_due(n8n, at_75, 4200, 3600, STARTED) is True


def test_in_failover_the_hourly_slot_runs_once_per_slot() -> None:
    now = datetime(2026, 11, 20, 13, 20, tzinfo=UTC)
    ran_this_slot = TriggerState(
        "hourly", now - timedelta(minutes=5), {"source": "failover", "slot_at": "2026-11-20T13:00:00+00:00"}
    )
    assert failover.failover_due(ran_this_slot, now, 4200, 3600, STARTED) is False
    next_slot = datetime(2026, 11, 20, 14, 0, 30, tzinfo=UTC)
    assert failover.failover_due(ran_this_slot, next_slot, 4200, 3600, STARTED) is True


def test_when_n8n_calls_again_the_failover_stops_at_once() -> None:
    now = datetime(2026, 11, 20, 14, 0, 30, tzinfo=UTC)
    back = TriggerState("hourly", now - timedelta(seconds=20), {"slot_at": "2026-11-20T14:00:00+00:00"})
    assert failover.trigger_missing(back, now, 4200, STARTED) is False
    assert failover.failover_due(back, now, 4200, 3600, STARTED) is False


@pytest.mark.parametrize(
    ("trigger", "due"),
    [
        (state("discovery", 1501), True),  # n8n silent for longer than the grace time: run now
        (state("discovery", 1500), False),
        (state("discovery", 599, source="failover"), False),  # our own run was under 10 minutes ago
        (state("discovery", 600, source="failover"), True),
        (state("guardian", 30, source="failover"), False),
        (state("guardian", 5000, source="failover"), True),
        (state("stats", None), True),
    ],
)
def test_the_other_triggers_are_repeated_every_interval_while_n8n_is_missing(
    trigger: TriggerState, due: bool
) -> None:
    assert failover.failover_due(trigger, NOW, 1500, 600, STARTED) is due

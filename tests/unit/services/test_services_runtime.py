"""The periodic task loop, handler registration and the ranking formula of the scheduler (no database)."""

import sys
import threading
import types
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sixstars import jobs, notify
from sixstars.logging import get_logger
from sixstars.posting.params import PostingParams
from sixstars.services import _runtime, scheduler, worker
from sixstars.services._runtime import PeriodicTask, run_due, run_loop

LOG = get_logger("test")

# ── periodic tasks ────────────────────────────────────────────────────────────────────────────────────────


def test_a_task_runs_on_the_first_tick_and_then_once_per_interval() -> None:
    calls: list[float] = []
    now = [100.0]
    task = PeriodicTask("t", 10.0, lambda: calls.append(now[0]))
    for moment in (100.0, 105.0, 109.9, 110.0, 119.0, 121.0):
        now[0] = moment
        run_due([task], moment, LOG)
    assert calls == [100.0, 110.0, 121.0]


def test_a_failing_task_does_not_stop_the_others_and_is_tried_again() -> None:
    ran: list[str] = []

    def broken() -> None:
        ran.append("broken")
        raise RuntimeError("no database")

    tasks = [PeriodicTask("broken", 5.0, broken), PeriodicTask("fine", 5.0, lambda: ran.append("fine"))]
    assert run_due(tasks, 0.0, LOG) == ["broken", "fine"]
    assert run_due(tasks, 1.0, LOG) == []
    assert run_due(tasks, 5.0, LOG) == ["broken", "fine"]
    assert ran == ["broken", "fine", "broken", "fine"]


def test_the_loop_ends_as_soon_as_stop_is_set() -> None:
    stop = threading.Event()
    count = [0]

    def tick() -> None:
        count[0] += 1
        if count[0] == 3:
            stop.set()

    run_loop([PeriodicTask("tick", 0.0, tick)], stop, LOG, tick_s=0.001)
    assert count[0] == 3


def test_the_loop_does_nothing_when_stop_is_already_set() -> None:
    stop = threading.Event()
    stop.set()
    ran: list[int] = []
    run_loop([PeriodicTask("t", 0.0, lambda: ran.append(1))], stop, LOG, tick_s=60)
    assert ran == []


def test_stop_signals_are_only_installed_in_the_main_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[int] = []
    monkeypatch.setattr(_runtime.signal, "signal", lambda signum, handler: installed.append(signum))
    thread = threading.Thread(target=_runtime.install_stop_signals, args=(threading.Event(), LOG))
    thread.start()
    thread.join()
    assert installed == []
    stop = threading.Event()
    _runtime.install_stop_signals(stop, LOG)
    assert sorted(installed) == sorted([_runtime.signal.SIGINT, _runtime.signal.SIGTERM])


def test_the_scheduler_has_the_m11_and_m13_tasks_with_their_intervals() -> None:
    assert {task.name: task.interval_s for task in scheduler.tasks()} == {
        "heartbeat": 30.0,
        "notify_retry": 60.0,
        "stale_jobs": 60.0,
        "ranking_refresh": 600.0,
        "guardian_health": 60.0,
        "stuck_jobs": 300.0,
        "clip_expiry": 600.0,
        "prune": 3600.0,
        "launch_summary": 60.0,
    }


# ── handler registration ──────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str, str]]]:
    """An empty handler registry, and the alerts that registration sent."""
    monkeypatch.setattr(worker, "HANDLERS", {})
    monkeypatch.setattr(worker, "_registered_modules", set())
    alerts: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        notify, "send", lambda severity, title, body="": alerts.append((severity, title, body))
    )
    yield alerts


def fake_module(monkeypatch: pytest.MonkeyPatch, name: str, **attributes: Any) -> None:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def noop(job: jobs.Job) -> None:
    return None


def test_register_all_registers_every_module_that_has_handlers_and_skips_the_rest(
    clean_registry: list[tuple[str, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_module(monkeypatch, "fake_pkg_a.handlers", register=lambda: worker.register("a.job", noop))
    fake_module(monkeypatch, "fake_pkg_b.handlers")  # exists, no register() yet
    modules = ("fake_pkg_a.handlers", "fake_pkg_b.handlers", "sixstars.no_such_package.handlers")
    assert worker.register_all(modules) == ["fake_pkg_a.handlers"]
    assert set(worker.HANDLERS) == {"a.job"}
    assert clean_registry == []  # a package without handlers is not an error


def test_an_entry_can_name_its_registration_function(
    clean_registry: list[tuple[str, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_module(monkeypatch, "fake_pkg_c.pipeline", register_handlers=lambda: worker.register("c.job", noop))
    entries = ("fake_pkg_c.pipeline:register_handlers", "fake_pkg_c.pipeline:not_there")
    assert worker.register_all(entries) == ["fake_pkg_c.pipeline:register_handlers"]
    assert set(worker.HANDLERS) == {"c.job"} and clean_registry == []


def test_register_all_twice_registers_once(
    clean_registry: list[tuple[str, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_module(monkeypatch, "fake_pkg_a.handlers", register=lambda: worker.register("a.job", noop))
    assert worker.register_all(("fake_pkg_a.handlers",)) == ["fake_pkg_a.handlers"]
    assert worker.register_all(("fake_pkg_a.handlers",)) == []
    assert set(worker.HANDLERS) == {"a.job"}


def test_a_broken_handlers_module_is_reported_and_the_others_still_register(
    clean_registry: list[tuple[str, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode() -> None:
        raise RuntimeError("bad wiring")

    fake_module(monkeypatch, "fake_pkg_bad.handlers", register=explode)
    fake_module(monkeypatch, "fake_pkg_a.handlers", register=lambda: worker.register("a.job", noop))
    assert worker.register_all(("fake_pkg_bad.handlers", "fake_pkg_a.handlers")) == ["fake_pkg_a.handlers"]
    assert set(worker.HANDLERS) == {"a.job"}
    assert [(severity, title) for severity, title, _ in clean_registry] == [
        ("critical", "Worker could not load job handlers")
    ]
    assert "fake_pkg_bad.handlers: RuntimeError" in clean_registry[0][2]


def test_a_module_that_imports_something_missing_is_broken_not_absent() -> None:
    absent = ModuleNotFoundError("No module named 'sixstars.vod.handlers'", name="sixstars.vod.handlers")
    parent = ModuleNotFoundError("No module named 'sixstars.vod'", name="sixstars.vod")
    dependency = ModuleNotFoundError("No module named 'torch'", name="torch")
    assert worker._is_missing("sixstars.vod.handlers", absent) is True
    assert worker._is_missing("sixstars.vod.handlers", parent) is True
    assert worker._is_missing("sixstars.vod.handlers", dependency) is False
    assert worker._is_missing("sixstars.vod.handlers", ModuleNotFoundError("?")) is False


def test_the_real_module_list_registers_the_posting_handlers(
    clean_registry: list[tuple[str, str, str]],
) -> None:
    registered = worker.register_all()
    assert "sixstars.posting.handlers" in registered
    assert {"post.hourly", "post.retry"} <= set(worker.HANDLERS)
    assert clean_registry == []  # packages without a handlers module are skipped quietly


def test_registering_one_job_type_twice_is_an_error(clean_registry: list[tuple[str, str, str]]) -> None:
    worker.register("x", noop)
    with pytest.raises(ValueError, match="already registered"):
        worker.register("x", noop)


def test_an_unknown_queue_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown queue"):
        worker.run("slow")


# ── ranking refresh formula ───────────────────────────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def params() -> PostingParams:
    return PostingParams.from_settings(
        {
            "control.emergency_stop": False,
            "launch.dry_run": False,
            "launch.launch_at": "2026-11-18T11:00:00Z",
            "posting.jitter_max_s": 0,
            "posting.min_gap_s": 900,
            "posting.spike_max_per_hour": 3,
            "posting.skip_hourly_after_spike_s": 600,
            "posting.spike_max_wait_s": 600,
            "posting.spike_max_age_s": 1800,
            "posting.freshness_half_life_h": {"live": 2, "vod": 24},
            "posting.max_clip_age_h": 72,
            "posting.first_comment": "",
            "posting.base_hashtags": [],
            "posting.youtube_extra_hashtags": [],
            "posting.platform_max_24h": {"tiktok": 15, "instagram": 25, "youtube": 30},
        },
        "production",
    )


def row(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "score": 8,
        "source_type": "vod",
        "approved_at": NOW - timedelta(hours=24),
        "spike_wall": None,
        "spike_tier": None,
        "creator_tier": "auto",
        "demoted": False,
    }
    base.update(changes)
    return base


def test_rank_of_a_vod_clip_halves_after_one_half_life() -> None:
    assert scheduler._new_rank(row(), NOW, params()) == pytest.approx(4.0)


def test_rank_of_a_live_clip_ages_from_the_spike_time_with_its_bonuses() -> None:
    live = row(
        source_type="live",
        spike_wall=NOW - timedelta(hours=2),
        approved_at=NOW - timedelta(minutes=5),
        spike_tier="S",
        creator_tier="preferred",
    )
    assert scheduler._new_rank(live, NOW, params()) == pytest.approx(8 * 0.5 * 1.3 * 1.1)


def test_rank_of_a_live_clip_without_a_spike_time_ages_from_its_approval() -> None:
    live = row(source_type="live", approved_at=NOW - timedelta(hours=4), spike_tier="A")
    assert scheduler._new_rank(live, NOW, params()) == pytest.approx(8 * 0.25 * 1.15)


def test_a_demoted_spike_clip_keeps_its_boost() -> None:
    assert scheduler._new_rank(row(demoted=True), NOW, params()) == pytest.approx(4.0 * 1.5)

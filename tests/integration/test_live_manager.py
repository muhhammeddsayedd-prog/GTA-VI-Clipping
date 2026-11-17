"""The live lane against Postgres: the manager's handling of `live_sessions`, the blocked-source back-off,
the stream end and its replay hand-off, the spike path up to the render job, and the worker-side hook.

Nothing is recorded: the recorder's processes are replaced by a fake that writes a segment file (or dies
with the message of a refused or offline stream), the chat by an empty stream, Claude by a fake.
"""

import asyncio
import threading
from collections.abc import AsyncGenerator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sixstars import config, db, jobs, settings
from sixstars.ai import calls
from sixstars.chat.models import ChatMessage, Platform
from sixstars.cli import app
from sixstars.clients.anthropic_client import AnthropicError
from sixstars.clients.fakes import FakeAnthropicClient, FakeTwitchClient, fake_video
from sixstars.factory import pipeline
from sixstars.live import buffer, handlers, health, store
from sixstars.live.config import LiveConfig
from sixstars.live.manager import LiveManager
from sixstars.live.recorder import RunHandle
from sixstars.live.session import LiveSession, SessionDeps
from sixstars.services import live_manager, worker
from sixstars.spikes import DetectedSpike

BOT_CHECK = (
    "[plugins.youtube][error] Could not get video info - LOGIN_REQUIRED: Sign in to confirm you're not "
    "a bot\n"
    "error: No playable streams found on this URL"
)
OFFLINE = "error: No playable streams found on this URL"
MOMENT = {"is_moment": True, "confidence": 0.9, "moment_type": "funny", "one_line": "He crashed the car"}
NOISE = {
    "is_moment": False,
    "confidence": 0.9,
    "moment_type": "other",
    "one_line": "Chat answers a question",
    "noise_reason": "chat answers the streamer",
}


class FakeHandle:
    """One recorder run without processes. In mode `ok` it writes a segment and stays alive."""

    def __init__(self, mode: str, pattern: Path) -> None:
        self.running = mode == "ok"
        self.text = {"ok": "", "blocked": BOT_CHECK, "offline": OFFLINE}[mode]
        if self.running:
            pattern.parent.mkdir(parents=True, exist_ok=True)
            (pattern.parent / datetime.now(UTC).strftime(pattern.name)).write_bytes(b"x" * 2048)

    def alive(self) -> bool:
        return self.running

    def output(self) -> str:
        return self.text

    def filtering(self) -> bool:
        return False

    def stop(self, timeout_s: float) -> None:
        self.running = False

    def kill(self) -> None:
        self.running = False


class Streams:
    """What each platform answers to a recorder start, and every run that was started."""

    def __init__(self) -> None:
        self.mode: dict[str, str] = {"youtube": "ok", "twitch": "ok"}
        self.runs: list[tuple[str, FakeHandle]] = []

    def spawn(self, streamlink: Sequence[str], ffmpeg: Sequence[str], env: dict[str, str]) -> RunHandle:
        platform = "twitch" if "twitch.tv" in streamlink[-2] else "youtube"
        handle = FakeHandle(self.mode[platform], Path(ffmpeg[-1]))
        self.runs.append((platform, handle))
        return handle  # type: ignore[return-value]

    def count(self, platform: str) -> int:
        return sum(1 for name, _ in self.runs if name == platform)


async def silent_chat(platform: Platform, channel: str) -> AsyncGenerator[ChatMessage, None]:
    while True:
        await asyncio.sleep(3600)
        yield  # type: ignore[misc]  # pragma: no cover - never reached


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def forward(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class World:
    """A manager with fake streams, plus helpers to write and read the rows it works on."""

    def __init__(self) -> None:
        self.streams = Streams()
        self.clock = Clock()
        self.ai = FakeAnthropicClient({calls.P1: MOMENT})
        self.managers: list[LiveManager] = []
        self.manager = self.new_manager()

    def deps(self) -> SessionDeps:
        return SessionDeps(open_chat=silent_chat, ai_client=self.ai, spawn=self.streams.spawn)

    def new_manager(self) -> LiveManager:
        manager = LiveManager(deps=self.deps(), clock=self.clock, free_bytes=lambda: 10**12)
        self.managers.append(manager)
        self.manager = manager
        return manager

    def creator(self, handle: str, platform: str = "youtube", tier: str = "auto") -> str:
        row = db.fetch_one(
            "insert into creators (platform, channel_id, handle, tier) values (%s, %s, %s, %s) "
            "returning id::text as id",
            (platform, f"chan-{handle}", handle, tier),
        )
        assert row is not None
        return str(row["id"])

    def session(
        self,
        handle: str,
        platform: str = "youtube",
        score: float = 50,
        status: str = "assigned",
        **columns: Any,
    ) -> str:
        creator = self.creator(handle, platform, columns.pop("tier", "auto"))
        url = (
            f"https://www.twitch.tv/{handle}"
            if platform == "twitch"
            else f"https://www.youtube.com/watch?v={handle}"
        )
        row = db.fetch_one(
            "insert into live_sessions "
            "(creator_id, platform, source_id, channel_login, url, status, trend_score, started_at) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s) returning id::text as id",
            (creator, platform, f"src-{handle}", handle, url, status, score, columns.get("started_at")),
        )
        assert row is not None
        return str(row["id"])

    def pass_(self) -> None:
        """One look at the table, then one poll of every session."""
        self.manager.reconcile(LiveConfig.load())
        self.manager._poll_sessions()

    def status(self, session_id: str) -> str:
        row = db.fetch_one("select status from live_sessions where id = %s", (session_id,))
        assert row is not None
        return str(row["status"])

    def row(self, session_id: str) -> dict[str, Any]:
        row = db.fetch_one("select * from live_sessions where id = %s", (session_id,))
        assert row is not None
        return row

    def close(self) -> None:
        for manager in self.managers:
            manager.shutdown(join_s=10)


def events_of(event_type: str) -> list[dict[str, Any]]:
    return db.fetch_all("select * from events where type = %s order by id", (event_type,))


def queued(job_type: str) -> list[dict[str, Any]]:
    return db.fetch_all("select * from jobs where type = %s order by id", (job_type,))


def clean() -> None:
    db.execute("truncate posts, clips, spikes, videos, live_sessions, creators restart identity cascade")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.env.cache_clear()
    clean()
    settings.set("live.max_slots", 3)
    settings.set("live.reconnect_backoff_s", 0)
    settings.set("live.max_reconnects", 1)
    settings.set("live.source_health", None)
    world = World()
    yield world
    world.close()
    clean()


# ── starting and stopping ────────────────────────────────────


def test_an_assigned_session_starts_and_becomes_recording_with_the_first_data(world: World) -> None:
    session = world.session("alpha")
    world.manager.reconcile(LiveConfig.load())
    assert world.status(session) == "assigned"  # the slot is held, but nothing was recorded yet
    world.manager._poll_sessions()
    row = world.row(session)
    assert row["status"] == "recording" and row["started_at"] is not None
    assert row["buffer_dir"] == str(store.buffer_dir(session))
    assert len(buffer.scan(store.buffer_dir(session))) == 1
    assert [event["entity_id"] for event in events_of("live.recording")] == [session]
    info = world.manager.info()
    assert info["active"] == 1 and info["started_at"] and info["sessions"][0]["recording"] is True
    world.pass_()
    assert len(events_of("live.recording")) == 1 and world.streams.count("youtube") == 1  # nothing twice


def test_the_stream_start_and_title_come_from_what_discovery_saw(world: World) -> None:
    session = world.session("alpha")
    began = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    db.execute(
        "insert into candidates (creator_id, source_type, platform, source_id, url, title, started_at, "
        "trend_score) select creator_id, 'live', platform, source_id, url, 'Bank job night', %s, 70 "
        "from live_sessions where id = %s",
        (began, session),
    )
    world.pass_()
    # two hours into the stream: `started_at` is the start of the stream, so the replay pass can turn
    # wall-clock times into positions in the video
    assert world.row(session)["started_at"] == began
    live = world.manager.sessions[session]
    assert live.row.title == "Bank job night" and live.stream_start == began.timestamp()


def test_only_max_slots_sessions_run_and_the_best_are_taken_first(world: World) -> None:
    settings.set("live.max_slots", 2)
    low, top, mid = (
        world.session("low", score=10),
        world.session("top", score=90),
        world.session("mid", score=50),
    )
    world.pass_()
    assert (world.status(top), world.status(mid), world.status(low)) == ("recording", "recording", "assigned")
    assert set(world.manager.sessions) == {top, mid}


def test_with_zero_slots_the_lane_idles_and_running_sessions_are_closed(world: World) -> None:
    settings.set("live.max_slots", 0)
    waiting = world.session("alpha")
    world.pass_()
    assert world.status(waiting) == "assigned" and world.streams.runs == []
    settings.set("live.max_slots", 3)
    world.pass_()
    assert world.status(waiting) == "recording"
    settings.set("live.max_slots", 0)  # the kill switch at run time
    world.pass_()
    assert world.manager.sessions == {} and world.status(waiting) == "replay_queued"
    assert len(queued("replay.scan")) == 1


def test_a_released_slot_ends_the_session_and_queues_its_replay_scan(world: World) -> None:
    session = world.session("alpha")
    world.pass_()
    directory = store.buffer_dir(session)
    assert buffer.scan(directory)
    db.execute("update live_sessions set status = 'released' where id = %s", (session,))
    before = datetime.now(UTC)
    world.pass_()
    row = world.row(session)
    assert row["status"] == "replay_queued" and row["ended_at"] is not None and row["buffer_dir"] is None
    assert row["vod_source_id"] == "src-alpha"  # on YouTube the same video becomes the replay
    (job,) = queued("replay.scan")
    assert job["payload"] == {"live_session_id": session} and job["queue"] == "normal"
    assert job["dedupe_key"] == f"replay:{session}"
    assert job["run_after"] >= before + timedelta(
        seconds=800
    )  # live.replay_delay_s: the chat replay needs time
    assert not directory.exists()  # segments deleted, empty directory removed
    assert [event["entity_id"] for event in events_of("live.ended")] == [session]
    assert world.manager.sessions == {}


def test_a_slot_released_before_anything_was_recorded_is_just_dropped(world: World) -> None:
    world.streams.mode["youtube"] = "offline"
    settings.set("live.max_reconnects", 5)
    session = world.session("alpha")
    world.manager.reconcile(LiveConfig.load())
    db.execute("update live_sessions set status = 'released' where id = %s", (session,))
    world.pass_()
    assert (
        world.status(session) == "released" and queued("replay.scan") == [] and world.manager.sessions == {}
    )


def test_a_twitch_session_that_ends_looks_for_its_archive_first(world: World) -> None:
    session = world.session("kiwo", platform="twitch")
    world.pass_()
    db.execute("update live_sessions set status = 'released' where id = %s", (session,))
    world.pass_()
    assert world.status(session) == "ended" and queued("replay.scan") == []
    (job,) = queued("replay.find")
    assert job["payload"]["live_session_id"] == session and job["dedupe_key"] == f"replay.find:{session}"


# ── the stream ends, or never was live ───────────────────────


def test_a_stream_that_goes_offline_is_retried_then_ended_with_a_replay_scan(world: World) -> None:
    session = world.session("alpha")
    world.pass_()
    world.streams.mode["youtube"] = "offline"
    world.streams.runs[-1][1].kill()
    world.manager._poll_sessions()  # run 1 is dead: one reconnect is allowed
    assert world.status(session) == "recording" and world.row(session)["reconnects"] == 1
    world.manager._poll_sessions()  # the reconnect starts ...
    world.manager._poll_sessions()  # ... and finds nothing live
    assert world.status(session) == "replay_queued" and world.streams.count("youtube") == 2
    (ended,) = events_of("live.ended")
    assert "the stream ended" in ended["message"] and ended["data"]["kind"] == "not_live"
    assert [event["level"] for event in events_of("live.reconnect")] == ["warn"]


def test_a_dropped_stream_that_comes_back_keeps_recording_in_a_new_run(world: World) -> None:
    session = world.session("alpha")
    world.pass_()
    world.streams.runs[-1][1].kill()
    for _ in range(3):
        world.manager._poll_sessions()
    assert world.status(session) == "recording" and world.row(session)["reconnects"] == 1
    assert {segment.run for segment in buffer.scan(store.buffer_dir(session))} == {1, 2}
    assert world.manager.sessions[session].info()["run"] == 2


def test_a_stream_that_never_was_live_fails_without_a_replay_scan(world: World) -> None:
    world.streams.mode["youtube"] = "offline"
    session = world.session("alpha")
    world.manager.reconcile(LiveConfig.load())
    for _ in range(4):
        world.manager._poll_sessions()
    row = world.row(session)
    assert row["status"] == "failed" and row["started_at"] is None
    assert queued("replay.scan") == [] and events_of("live.failed")[0]["level"] == "warn"
    world.pass_()
    assert world.streams.count("youtube") == 2  # a failed session is not picked up again


def test_a_forbidden_creator_never_gets_a_recorder(world: World) -> None:
    session = world.session("alpha", tier="blocked")
    world.pass_()
    assert world.status(session) == "released" and world.streams.runs == []


# ── a platform that refuses this machine ─────────────────────


def test_a_blocked_platform_is_backed_off_reported_once_and_recovers(world: World) -> None:
    world.streams.mode["youtube"] = "blocked"
    first, twitch = world.session("alpha", score=90), world.session("kiwo", platform="twitch", score=10)
    world.pass_()
    # The slot is given back at once, the other platform records, nobody is told yet.
    assert world.status(first) == "released" and world.status(twitch) == "recording"
    assert health.load()["youtube"]["blocks"] == 1
    assert db.fetch_all("select 1 from notify_outbox") == []

    # Discovery assigns YouTube streams again: they are released without a start while it backs off.
    second = world.session("beta", score=80)
    db.execute("update live_sessions set status = 'assigned' where id = %s", (first,))
    world.pass_()
    world.pass_()
    assert world.status(first) == world.status(second) == "released" and world.streams.count("youtube") == 1

    # After the short first back-off one stream probes; refused again: one warning.
    world.clock.forward(minutes=6)
    db.execute("update live_sessions set status = 'assigned' where id = %s", (first,))
    world.pass_()
    assert world.streams.count("youtube") == 2 and world.status(first) == "released"
    (warning,) = db.fetch_all("select severity, title, body from notify_outbox")
    assert (warning["severity"], warning["title"]) == ("warn", "Live source youtube is blocked")
    assert "not a bot" in warning["body"] and "PROXY_URL" in warning["body"]
    assert len(events_of("live.source_blocked")) == 1

    # Hours of refused probes: one start per back-off, no further message.
    for _ in range(4):
        world.clock.forward(minutes=31)
        db.execute("update live_sessions set status = 'assigned' where id = %s", (first,))
        world.pass_()
        world.pass_()
    assert world.streams.count("youtube") == 6
    assert len(db.fetch_all("select 1 from notify_outbox")) == 1
    assert world.status(twitch) == "recording"  # untouched all along

    # YouTube works again: the probe records and one recovery message goes out.
    world.streams.mode["youtube"] = "ok"
    world.clock.forward(minutes=31)
    db.execute("update live_sessions set status = 'assigned' where id = %s", (first,))
    world.pass_()
    assert world.status(first) == "recording" and health.load() == {}
    messages = db.fetch_all("select severity, title from notify_outbox order by id")
    assert [(row["severity"], row["title"]) for row in messages] == [
        ("warn", "Live source youtube is blocked"),
        ("info", "Live source youtube recovered"),
    ]
    assert len(events_of("live.source_recovered")) == 1


def test_a_low_disk_stops_new_sessions_but_not_running_ones(world: World) -> None:
    running = world.session("alpha", score=90)
    world.pass_()
    world.manager._free_bytes = lambda: 1_000  # type: ignore[method-assign]
    waiting = world.session("beta", score=10)
    world.pass_()
    assert (world.status(running), world.status(waiting)) == ("recording", "assigned")
    assert db.fetch_all("select title from notify_outbox")[0]["title"].startswith("Live lane paused")


# ── restart and shutdown ─────────────────────────────────────


def test_a_restarted_manager_resumes_recording_sessions_and_keeps_their_start_time(world: World) -> None:
    session = world.session("alpha")
    world.pass_()
    started = world.row(session)["started_at"]
    world.manager.shutdown(join_s=10)
    assert world.status(session) == "recording"  # shutdown leaves the row for the next start
    assert buffer.scan(store.buffer_dir(session))  # and the buffer on disk
    world.new_manager()
    world.pass_()
    assert world.status(session) == "recording" and world.row(session)["started_at"] == started
    assert [event["entity_id"] for event in events_of("live.resumed")] == [session]
    assert {segment.run for segment in buffer.scan(store.buffer_dir(session))} == {1, 2}


def test_a_session_released_while_the_manager_was_down_is_closed_on_the_next_start(world: World) -> None:
    session = world.session("alpha", status="released", started_at=datetime.now(UTC) - timedelta(hours=1))
    never = world.session("beta", status="released")
    world.pass_()
    assert world.status(session) == "replay_queued" and world.status(never) == "released"
    assert len(queued("replay.scan")) == 1 and world.streams.runs == []


def test_orphaned_buffers_are_cleaned(world: World) -> None:
    directory = store.buffer_dir("00000000-0000-0000-0000-00000000dead")
    directory.mkdir(parents=True)
    (directory / buffer.segment_name(1, datetime.now(UTC))).write_bytes(b"x")
    world.manager.sweep_orphans(LiveConfig.load())
    assert not directory.exists()


def test_a_cut_is_deleted_as_soon_as_the_render_of_its_spike_is_over(world: World) -> None:
    session = world.session("alpha", status="recording", started_at=datetime.now(UTC))
    creator = world.row(session)["creator_id"]
    ids = {}
    for status in ("rendering", "clipped", "rejected"):
        row = db.fetch_one(
            "insert into spikes (mode, live_session_id, creator_id, status) values ('live', %s, %s, %s) "
            "returning id::text as id",
            (session, creator, status),
        )
        assert row is not None
        ids[status] = row["id"]
    ids["unknown"] = "99999999-9999-9999-9999-999999999999"
    directory = store.buffer_dir(session)
    for spike_id in ids.values():
        path = buffer.cut_path(directory, spike_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cut")
    manual = buffer.cut_path(directory, "manual-1")
    manual.write_bytes(b"cut")
    world.manager.sessions[session] = None  # type: ignore[assignment]  # a running session: segments stay
    try:
        world.manager.sweep_orphans(LiveConfig.load())
    finally:
        world.manager.sessions.clear()
    left = sorted(path.stem for path in (directory / "cuts").iterdir())
    assert left == sorted([ids["rendering"], "manual-1"])  # still needed, or not a spike cut


def test_the_service_stops_all_sessions_within_the_grace_period(world: World) -> None:
    session = world.session("alpha")
    stop = threading.Event()
    thread = threading.Thread(target=live_manager.run, args=(stop, 0.05, world.manager), daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if world.status(session) == "recording":
                break
            stop.wait(0.05)
        assert world.status(session) == "recording"
    finally:
        started = datetime.now(UTC)
        stop.set()
        thread.join(30)
    assert not thread.is_alive() and (datetime.now(UTC) - started).total_seconds() < 20
    assert all(not handle.running for _, handle in world.streams.runs)  # every recorder was stopped


# ── the spike path ───────────────────────────────────────────

T = datetime.now(UTC).timestamp() - 60


def spike(tier: str = "S") -> DetectedSpike:
    return DetectedSpike(
        mode="live",
        peak_t=T + 30,
        first_t=T + 25,
        score_z=6.5,
        parts={"msgs_z": 10.0, "uniq_z": 10.0, "react_z": 3.0, "clipreq_z": 1.0, "audio_z": 0.0},
        msgs=120,
        uniq=80,
        reaction_count=90,
        clip_requests=3,
        tier=tier,  # type: ignore[arg-type]
        window_start=T + 5,
        window_end=T + 45,
        spike_floor=T + 25,
        pad_start=T - 10,
        pad_end=T + 60,
        chat_sample=[
            {"t_rel": 0.5, "user": "a", "text": "LMAO"},
            {"t_rel": 1.0, "user": "b", "text": "CLIP IT"},
        ],
    )


@pytest.fixture
def live_session(world: World, monkeypatch: pytest.MonkeyPatch) -> LiveSession:
    """A session that is not started: only its spike handling is used. The buffer cut is faked."""
    settings.set("live.cut_settle_s", 0)
    session_id = world.session("alpha", status="recording", started_at=datetime.fromtimestamp(T - 600, UTC))
    row = store.get_session(session_id)
    assert row is not None

    def fake_extract(directory: Path, start: float, end: float, out_path: Path, **_: Any) -> buffer.Cut:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"cut")
        return buffer.Cut(out_path, start - 3.0, end - start + 3.0, start, end, ("seg",), 3)

    monkeypatch.setattr(buffer, "extract", fake_extract)
    return LiveSession(row, LiveConfig.load(), world.deps())


def test_an_s_tier_spike_is_confirmed_cut_and_queued_for_the_express_worker(
    world: World, live_session: LiveSession
) -> None:
    job_id = live_session.handle_spike(spike())
    assert job_id is not None
    (job,) = queued("express.render")
    assert (job["queue"], job["priority"], job["id"]) == ("express", 100, job_id)
    stored = db.fetch_one("select * from spikes")
    assert stored is not None and (stored["status"], stored["tier"]) == ("confirmed", "S")
    assert job["dedupe_key"] == f"express.render:{stored['id']}"
    item = pipeline.RenderInput.from_payload(job["payload"], default_mode="express")
    assert (item.lane, item.mode, item.source_type, item.creator) == ("spike", "express", "live", "alpha")
    assert (
        item.source_path == buffer.cut_path(live_session.directory, str(stored["id"]))
        and item.source_path.is_file()
    )
    # the cut starts 3 s before the padded window (T - 10): the window is at 18..58 in the file
    assert (item.window_start_s, item.window_end_s, item.spike_floor_s) == (18.0, 58.0, 38.0)
    assert item.verdict is not None and item.verdict.one_line == "He crashed the car"
    live = job["payload"]["live"]
    assert (
        live["session_id"] == live_session.row.id
        and live["start_wall"] == T + 5
        and live["end_wall"] == T + 45
    )
    assert set(live["timings_ms"]) == {"detect_lag_ms", "confirm_ms", "cut_wait_ms", "cut_ms"}
    (event,) = events_of("live.spike_queued")
    assert event["data"]["spike_id"] == str(stored["id"]) and event["data"]["lane"] == "spike"
    # P1 saw the chat sample only, in the express budget lane (one request, no transcript)
    (call,) = world.ai.calls_to("complete_json")
    assert call["prompt_id"] == calls.P1 and "(not available)" in call["user_content"]


def test_the_express_path_is_never_stopped_by_the_daily_ai_budget(
    world: World, live_session: LiveSession
) -> None:
    settings.set("ai.daily_budget_usd", 0)
    assert live_session.handle_spike(spike()) is not None


def test_a_spike_claude_calls_noise_is_rejected_without_a_cut_or_a_job(
    world: World, live_session: LiveSession
) -> None:
    world.ai.responses[calls.P1] = NOISE
    assert live_session.handle_spike(spike()) is None
    stored = db.fetch_one("select status, tier from spikes")
    assert stored == {"status": "rejected", "tier": "rejected"}
    assert db.fetch_all("select 1 from jobs") == [] and not (live_session.directory / "cuts").exists()


@pytest.mark.parametrize("tier", ["blocked", "restricted"])
def test_a_forbidden_creators_spike_is_dropped_before_any_spend(
    world: World, live_session: LiveSession, tier: str
) -> None:
    db.execute("update creators set tier = %s where id = %s", (tier, live_session.row.creator_id))
    assert live_session.handle_spike(spike()) is None
    assert world.ai.calls == [] and db.fetch_all("select 1 from spikes") == []
    assert db.fetch_all("select 1 from jobs") == []
    assert events_of("live.spike_dropped")[0]["data"]["tier"] == tier


def test_under_the_emergency_stop_an_s_spike_takes_the_hourly_route(
    world: World, live_session: LiveSession
) -> None:
    settings.set("control.emergency_stop", True)
    assert live_session.handle_spike(spike()) is not None
    assert queued("express.render") == []
    (job,) = queued("clip.render")
    assert (job["queue"], job["priority"], job["payload"]["lane"], job["payload"]["mode"]) == (
        "normal",
        60,
        "hourly",
        "normal",
    )


def test_a_and_b_spikes_go_to_the_normal_queue_and_the_hourly_lane(
    world: World, live_session: LiveSession
) -> None:
    assert live_session.handle_spike(spike("B")) is not None
    (job,) = queued("clip.render")
    assert (job["queue"], job["priority"], job["payload"]["lane"]) == ("normal", 40, "hourly")
    # the whole context pad is cut for a normal render: T-10 .. T+60, plus the 3 s lead-in of the fake
    assert job["payload"]["pad_end_s"] == 73.0


def test_a_provisional_s_that_claude_is_less_sure_about_becomes_a(
    world: World, live_session: LiveSession
) -> None:
    world.ai.responses[calls.P1] = {**MOMENT, "confidence": 0.7}
    assert live_session.handle_spike(spike()) is not None
    assert queued("express.render") == [] and len(queued("clip.render")) == 1
    assert db.fetch_one("select tier from spikes") == {"tier": "A"}


def test_a_spike_that_cannot_be_confirmed_or_cut_is_closed_as_failed(
    world: World, live_session: LiveSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.ai.responses[calls.P1] = AnthropicError("boom")
    with pytest.raises(AnthropicError):
        live_session.handle_spike(spike())
    assert db.fetch_one("select status from spikes") == {"status": "failed"}

    def no_buffer(*_: Any, **__: Any) -> buffer.Cut:
        raise buffer.BufferCutError("the buffer holds nothing of this window")

    world.ai.responses[calls.P1] = MOMENT
    monkeypatch.setattr(buffer, "extract", no_buffer)
    other = spike()
    other.peak_t += 300
    with pytest.raises(buffer.BufferCutError):
        live_session.handle_spike(other)
    assert [row["status"] for row in db.fetch_all("select status from spikes order by detected_at")] == [
        "failed",
        "failed",
    ]
    assert db.fetch_all("select 1 from jobs") == [] and len(events_of("live.spike_failed")) == 2


def test_the_same_spike_reported_twice_is_queued_once(world: World, live_session: LiveSession) -> None:
    assert live_session.handle_spike(spike()) is not None
    assert live_session.handle_spike(spike()) is None  # the stored spike is past `detected`
    assert len(queued("express.render")) == 1 and len(db.fetch_all("select 1 from spikes")) == 1
    assert len(world.ai.calls) == 1 and len(events_of("live.spike_queued")) == 1  # no second spend


# ── the worker side ──────────────────────────────────────────


def claimed(queue: jobs.Queue, job_type: str, payload: dict[str, Any]) -> jobs.Job:
    jobs.enqueue(queue, job_type, payload)
    job = jobs.claim(queue, "worker-test")
    assert job is not None
    return job


def test_after_a_live_clip_renders_its_range_is_noted_and_the_latency_recorded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sixstars.gate import gate

    session = world.session("alpha", status="recording", started_at=datetime.now(UTC))
    gated: list[str] = []
    monkeypatch.setattr(gate, "after_render", lambda clip_id, job: gated.append(clip_id))
    clip_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    live = {
        "session_id": session,
        "spike_wall": T + 30,
        "start_wall": T + 5,
        "end_wall": T + 45,
        "queued_at": T + 70,
        "timings_ms": {"detect_lag_ms": 15000, "confirm_ms": 2000, "cut_wait_ms": 5000, "cut_ms": 1000},
    }
    job = claimed("express", "express.render", {"spike_id": "s1", "live": live})
    handlers.after_render(clip_id, job)
    handlers.after_render(clip_id, job)  # a retried job must not add the range twice
    assert gated == [clip_id, clip_id]
    (entry,) = world.row(session)["clipped_ranges"]
    assert entry["clip_id"] == clip_id
    assert datetime.fromisoformat(entry["start_wall"]).timestamp() == pytest.approx(T + 5)
    assert datetime.fromisoformat(entry["end_wall"]).timestamp() == pytest.approx(T + 45)
    done = events_of("live.express_done")
    assert len(done) == 2 and done[0]["entity_id"] == clip_id
    stages = done[0]["data"]["stages"]
    assert stages["detect_lag_ms"] == 15000 and stages["spike_to_decision_s"] > 0
    assert {"queue_wait_ms", "render_ms", "gate_post_ms"} <= set(stages)


def test_a_clip_that_did_not_come_from_a_live_session_only_passes_through_the_gate(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sixstars.gate import gate

    gated: list[str] = []
    monkeypatch.setattr(gate, "after_render", lambda clip_id, job: gated.append(clip_id))
    handlers.after_render("clip-1", claimed("normal", "clip.render", {"spike_id": "s1"}))
    assert gated == ["clip-1"] and events_of("live.express_done") == events_of("live.clip_done") == []


def test_registering_wraps_the_gate_hook_and_adds_replay_find(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "HANDLERS", {})
    monkeypatch.setattr(pipeline, "_after_render", {})
    handlers.register()
    handlers.register()  # safe to call twice
    assert worker.HANDLERS["replay.find"] is handlers.handle_replay_find
    assert pipeline._after_render == {
        "clip.render": handlers.after_render,
        "express.render": handlers.after_render,
    }
    assert "clip.gate" in worker.HANDLERS  # the gate's own handlers are installed with it


@pytest.fixture
def twitch(world: World) -> Iterator[tuple[FakeTwitchClient, str]]:
    session = world.session("kiwo", platform="twitch", status="ended", started_at=datetime.now(UTC))
    client = FakeTwitchClient()
    client.videos = []
    handlers.set_twitch_factory(lambda: client)
    yield client, session
    handlers.set_twitch_factory(None)


def find_job(session: str, minutes_left: float = 60) -> jobs.Job:
    deadline = datetime.now(UTC) + timedelta(minutes=minutes_left)
    return claimed("normal", "replay.find", {"live_session_id": session, "deadline": deadline.isoformat()})


def test_replay_find_queues_the_scan_once_the_archive_of_the_stream_exists(
    world: World, twitch: tuple[FakeTwitchClient, str]
) -> None:
    client, session = twitch
    other = fake_video(1, user_id="chan-kiwo")
    client.videos = [other]  # an older archive of another stream
    handlers.handle_replay_find(find_job(session))
    assert world.status(session) == "ended" and queued("replay.scan") == []
    (again,) = [job for job in queued("replay.find") if job["status"] == "queued"]
    assert again["payload"]["attempt"] == 1 and again["run_after"] > datetime.now(UTC) + timedelta(minutes=9)

    from dataclasses import replace

    client.videos = [replace(fake_video(2, user_id="chan-kiwo"), id="v-2914", stream_id="src-kiwo"), other]
    handlers.handle_replay_find(find_job(session))
    row = world.row(session)
    assert (row["status"], row["vod_source_id"]) == ("replay_queued", "v-2914")
    (scan,) = queued("replay.scan")
    assert scan["payload"] == {"live_session_id": session}


def test_replay_find_gives_up_at_the_deadline_or_without_twitch_keys(
    world: World, twitch: tuple[FakeTwitchClient, str]
) -> None:
    client, session = twitch
    handlers.handle_replay_find(find_job(session, minutes_left=1))
    assert [job["status"] for job in queued("replay.find")] == ["running"]  # nothing queued again
    client.configured = False
    handlers.handle_replay_find(find_job(session))
    missing = events_of("live.replay_missing")
    assert len(missing) == 2 and "Twitch keys" in missing[1]["message"]
    assert world.status(session) == "ended" and queued("replay.scan") == []


def test_live_status_prints_the_sessions_and_the_source_health(world: World) -> None:
    session = world.session("alpha")
    world.pass_()
    health.save({"twitch": {"blocks": 2, "skip_until": "2026-11-17T13:00:00+00:00"}})
    result = CliRunner().invoke(app, ["live", "status"])
    assert result.exit_code == 0
    assert (
        "recording" in result.output
        and session in result.output
        and "source twitch: blocked x2" in result.output
    )
    as_json = CliRunner().invoke(app, ["live", "status", "--json"])
    assert as_json.exit_code == 0 and '"max_slots": 3' in as_json.output

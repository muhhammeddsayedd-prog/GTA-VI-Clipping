"""Live manager: starts and stops the sessions discovery assigned in `live_sessions`, at most
`live.max_slots` at a time, and closes them properly when their stream ends.

`LiveManager.tick()` runs every second; every `live.poll_s` it reads the table:

1. `released` rows: the session is stopped. If anything was recorded the row becomes `ended` and the
   replay scan is queued (D55); a row that never recorded stays `released`.
2. `recording` rows without a running session (the service was restarted): recording resumes.
3. `assigned` rows, best trend score first, while slots are free: the session starts. The row becomes
   `recording` (and gets `started_at`) when the first stream data arrives, not before.
4. Sessions that finished by themselves (`LiveSession.poll`) are closed: `ended` + replay scan, `failed`,
   or `released` when the platform refused this machine.

**Degraded states**, all quiet (events, at most one warning each):

- `live.max_slots = 0`: nothing starts and running sessions are stopped. The service only heartbeats.
- A platform that refuses this machine (`blocked`): `health.py` backs it off; sessions of that platform
  are released without being tried until the back-off ends, then one start probes again.
- Less than `live.min_free_bytes` free on the data volume: no new session starts.

**Disk.** Every minute `sweep_orphans` removes the segments of sessions that are not running, the cut of
every spike whose render is over, and cuts older than `live.cut_keep_s`. A running session sweeps its own
buffer every ten seconds (`LiveSession.sweep`).

**Shutdown.** `shutdown()` stops every recorder and chat reader in parallel and leaves the rows in
`recording`, so the next start resumes them. Buffers stay on disk for that.
"""

import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sixstars import db, jobs, notify, settings
from sixstars.live import buffer, health, store
from sixstars.live.config import LiveConfig
from sixstars.live.session import LiveSession, Outcome, SessionDeps
from sixstars.logging import get_logger
from sixstars.vod.ytdlp import access_for

SERVICE: Final = "live-manager"
JOB_REPLAY_SCAN: Final = "replay.scan"
JOB_REPLAY_FIND: Final = "replay.find"
REPLAY_PRIORITY: Final = 25
CAMPAIGN_VOD_PRIORITY_KEY: Final = "campaigns.vod_priority"
CAMPAIGN_VOD_PRIORITY_DEFAULT: Final = 35
ORPHAN_SWEEP_S: Final = 60.0
SHUTDOWN_JOIN_S: Final = 20.0
DISK_WARN_TITLE: Final = "Live lane paused: data volume almost full"

SessionFactory = Callable[[store.SessionRow, LiveConfig, SessionDeps], LiveSession]


def utcnow() -> datetime:
    return datetime.now(UTC)


def replay_priority(session_id: str) -> int:
    """D118: the replay of a campaign session runs at campaign VOD priority, others at REPLAY_PRIORITY."""
    row = db.fetch_one("select campaign_id from live_sessions where id = %s", (session_id,))
    if row is None or row["campaign_id"] is None:
        return REPLAY_PRIORITY
    return settings.get_int(CAMPAIGN_VOD_PRIORITY_KEY, CAMPAIGN_VOD_PRIORITY_DEFAULT)


def queue_replay(row: store.SessionRow, cfg: LiveConfig, now: datetime) -> str:
    """Hand a finished session's replay to the VOD lane. YouTube: the same video id becomes the replay,
    so `replay.scan` is queued for a little later (the chat replay needs time to appear). Twitch: the
    archive has its own id, which `replay.find` looks up first. Returns the job type queued."""
    if row.platform == "twitch":
        deadline = now + timedelta(seconds=cfg.replay_find_max_s)
        jobs.enqueue(
            "normal",
            JOB_REPLAY_FIND,
            {"live_session_id": row.id, "deadline": deadline.isoformat()},
            priority=replay_priority(row.id),
            run_after=now + timedelta(seconds=cfg.replay_find_interval_s),
            dedupe_key=f"replay.find:{row.id}",
        )
        store.emit("live.replay_find_queued", row.id, "looking for the Twitch archive of the stream")
        return JOB_REPLAY_FIND
    queue_replay_scan(row.id, row.source_id, now + timedelta(seconds=cfg.replay_delay_s))
    return JOB_REPLAY_SCAN


def queue_replay_scan(session_id: str, vod_source_id: str, run_after: datetime) -> int | None:
    """Queue the `replay.scan` job the VOD lane handles and mark the session `replay_queued`."""
    job_id = jobs.enqueue(
        "normal",
        JOB_REPLAY_SCAN,
        {"live_session_id": session_id},
        priority=replay_priority(session_id),
        run_after=run_after,
        dedupe_key=f"replay:{session_id}",
    )
    store.set_replay_queued(session_id, vod_source_id, job_id, run_after)
    return job_id


class LiveManager:
    """The supervisor of all live sessions of this process."""

    def __init__(
        self,
        *,
        deps: SessionDeps | None = None,
        session_factory: SessionFactory | None = None,
        clock: Callable[[], datetime] = utcnow,
        free_bytes: Callable[[], int] | None = None,
    ) -> None:
        self.sessions: dict[str, LiveSession] = {}
        self.started_at = clock()
        self._deps = deps or SessionDeps()
        self._factory: SessionFactory = session_factory or LiveSession
        self._clock = clock
        self._free_bytes = free_bytes or self._disk_free
        self._next_poll = 0.0
        self._next_orphan_sweep = 0.0
        self._cfg: LiveConfig | None = None
        self._marked: set[str] = set()
        self._closing: dict[str, LiveSession] = {}
        self._lock = threading.Lock()
        self._idle_logged = False
        self._log = get_logger("live")

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _disk_free() -> int:
        root = store.live_root()
        probe = root if root.exists() else root.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return 1 << 62  # cannot tell: do not block recording on that

    def _session_deps(self) -> SessionDeps:
        deps = self._deps
        return SessionDeps(
            open_chat=deps.open_chat,
            ai_client=deps.ai_client,
            spawn=deps.spawn,
            ffmpeg=deps.ffmpeg,
            ffprobe=deps.ffprobe,
            clock=deps.clock,
            on_data=self._on_data,
        )

    def _on_data(self, session: LiveSession) -> None:
        """First stream data of a recorder run: the row becomes `recording`, the platform is healthy."""
        row = session.row
        if row.id not in self._marked:
            started = datetime.fromtimestamp(session.stream_start, UTC)
            if store.mark_recording(row, started, session.directory, resumed=session.resumed):
                self._marked.add(row.id)
        health.record_ok(row.platform, self._clock())

    def info(self) -> dict[str, Any]:
        """Heartbeat payload. `started_at` lets the guardian count restarts of this service."""
        cfg = self._cfg
        return {
            "pid": os.getpid(),
            "started_at": self.started_at.isoformat(),
            "max_slots": cfg.max_slots if cfg else None,
            "active": len(self.sessions),
            "sessions": [session.info() for session in self.sessions.values()],
            "blocked": {
                name: entry.get("skip_until") for name, entry in health.load().items() if entry.get("blocks")
            },
        }

    # ── one pass over the table ──────────────────────────────

    def tick(self) -> None:
        """Poll the running sessions; every `live.poll_s` also reconcile with `live_sessions`."""
        now_mono = time.monotonic()
        self._poll_sessions()
        if now_mono >= self._next_poll:
            cfg = self._cfg = LiveConfig.load()
            self._next_poll = now_mono + cfg.poll_s
            self.reconcile(cfg)
        if now_mono >= self._next_orphan_sweep and self._cfg is not None:
            self._next_orphan_sweep = now_mono + ORPHAN_SWEEP_S
            self.sweep_orphans(self._cfg)

    def _poll_sessions(self) -> None:
        for session_id, session in list(self.sessions.items()):
            try:
                outcome = session.poll()
            except Exception as exc:  # noqa: BLE001 - one broken session must not stop the others
                self._log.error(
                    "live_session_poll_failed", session_id=session_id, error=type(exc).__name__, exc_info=True
                )
                outcome = Outcome("ended" if session.ever_recorded else "failed", f"internal error: {exc}")
            if outcome is not None:
                self._finish(session, outcome)

    def reconcile(self, cfg: LiveConfig) -> None:
        """Bring the running sessions in line with the `live_sessions` table."""
        now = self._clock()
        rows = store.managed_sessions()
        by_id = {row.id: row for row in rows}
        if cfg.max_slots == 0 and not self._idle_logged:
            self._idle_logged = True
            self._log.info("live_lane_idle", reason="live.max_slots is 0")
        elif cfg.max_slots > 0:
            self._idle_logged = False

        # 1. slots released by discovery, sessions whose row vanished, and the kill switch
        for session_id, session in list(self.sessions.items()):
            row = by_id.get(session_id)
            if row is None or row.status == "released" or cfg.max_slots == 0:
                reason = "live.max_slots is 0" if cfg.max_slots == 0 else "the slot was released"
                self._finish(session, Outcome("ended" if session.ever_recorded else "released", reason))
        for row in rows:
            if row.status == "released" and row.started_at is not None and row.id not in self.sessions:
                # Released while this service was not running it: close it and scan its replay.
                self._close_row(row, Outcome("ended", "the slot was released"), now, cfg)

        if cfg.max_slots == 0:
            return
        # 2. resume, 3. start
        state = health.load()
        low_disk = self._free_bytes() < cfg.min_free_bytes
        waiting = [row for row in rows if row.status == "recording"] + [
            row for row in rows if row.status == "assigned"
        ]
        if low_disk and any(row.id not in self.sessions for row in waiting):
            self._warn_disk(cfg)  # only when a session is actually kept from starting
        for row in waiting:
            if row.id in self.sessions:
                continue
            if row.forbidden:
                self._close_row(row, Outcome("released", f"creator tier is {row.tier}"), now, cfg)
                continue
            until = health.backing_off(state, row.platform, now, access_for(row.platform).described)
            if until is not None:
                reason = f"{row.platform} is blocked; next probe at {until:%H:%M} UTC"
                self._close_row(
                    row, Outcome("ended" if row.status == "recording" else "released", reason), now, cfg
                )
                continue
            if len(self.sessions) >= cfg.max_slots or low_disk:
                continue
            self._start(row, cfg)

    def _start(self, row: store.SessionRow, cfg: LiveConfig) -> None:
        session = self._factory(row, cfg, self._session_deps())
        self.sessions[row.id] = session
        try:
            session.start()
        except Exception as exc:  # noqa: BLE001 - reported as a failed session below
            self._log.error(
                "live_session_start_failed", session_id=row.id, error=type(exc).__name__, exc_info=True
            )
            self._finish(
                session, Outcome("ended" if session.ever_recorded else "failed", f"start failed: {exc}")
            )
            return
        self._log.info(
            "live_session_started",
            session_id=row.id,
            platform=row.platform,
            creator=row.handle,
            resumed=session.resumed,
        )

    def _finish(self, session: LiveSession, outcome: Outcome) -> None:
        """Stop a session and write how it ended."""
        self.sessions.pop(session.row.id, None)
        cfg = self._cfg or session.cfg
        session.close()
        self._marked.discard(session.row.id)
        now = self._clock()
        if outcome.blocked:
            health.record_blocked(
                session.row.platform,
                now,
                outcome.reason,
                notify_after=cfg.blocked_notify_after,
                backoff_s=cfg.blocked_backoff_s,
                access=access_for(session.row.platform).described,
            )
        self._close_row(session.row, outcome, now, cfg)
        if session.busy():
            self._closing[session.row.id] = session  # a spike is still being cut from this buffer
        else:
            buffer.remove_segments(session.directory)
            buffer.remove_if_empty(session.directory)
        self._log.info(
            "live_session_finished",
            session_id=session.row.id,
            status=outcome.status,
            reason=outcome.reason[:200],
        )

    def _close_row(self, row: store.SessionRow, outcome: Outcome, now: datetime, cfg: LiveConfig) -> None:
        data = {"kind": outcome.kind, "blocked": outcome.blocked, "platform": row.platform}
        if not store.finish(row.id, outcome.status, now, outcome.reason, data):
            return
        if outcome.status == "ended":
            try:
                queue_replay(row, cfg, now)
            except Exception as exc:  # noqa: BLE001 - the session is closed either way
                self._log.error("live_replay_queue_failed", session_id=row.id, error=type(exc).__name__)

    def _warn_disk(self, cfg: LiveConfig) -> None:
        free_gb = self._free_bytes() / 1e9
        self._log.warning("live_disk_low", free_gb=round(free_gb, 1))
        notify.send(
            "warn",
            DISK_WARN_TITLE,
            f"{free_gb:.1f} GB free under the data directory, below live.min_free_bytes "
            f"({cfg.min_free_bytes / 1e9:.1f} GB). Running recordings continue inside their buffers; no "
            "new stream is started until space is freed.",
        )

    def sweep_orphans(self, cfg: LiveConfig) -> None:
        """Clean the directories of sessions that are not running: segments go at once (nothing reads
        them any more), cuts when they are older than `live.cut_keep_s`."""
        root = store.live_root()
        now = time.time()
        try:
            directories = [path for path in root.iterdir() if path.is_dir()]
        except OSError:
            return
        self._closing = {key: session for key, session in self._closing.items() if session.busy()}
        for directory in directories:
            self._drop_finished_cuts(directory)
        for directory in directories:
            if directory.name in self.sessions or directory.name in self._closing:
                continue
            buffer.remove_segments(directory)
            buffer.sweep_cuts(directory, now=now, keep_s=cfg.cut_keep_s)
            buffer.remove_if_empty(directory)

    def _drop_finished_cuts(self, directory: Path) -> None:
        """Delete the cut of every spike whose render is over (clipped, rejected or failed): the file is
        only needed until then, so a slot holds a couple of cuts at most."""
        cuts = directory / buffer.CUTS_DIR
        try:
            names = {path.stem: path for path in cuts.iterdir() if path.suffix == ".mp4"}
        except OSError:
            return
        spike_ids = [name for name in names if len(name) == 36]
        if not spike_ids:
            return
        try:
            done = store.finished_spikes(spike_ids)
        except Exception as exc:  # noqa: BLE001 - the age rule still bounds the cuts
            self._log.warning("live_cut_cleanup_failed", error=type(exc).__name__)
            return
        for spike_id in done:
            try:
                names[spike_id].unlink()
            except OSError:
                continue

    # ── shutdown ─────────────────────────────────────────────

    def shutdown(self, join_s: float = SHUTDOWN_JOIN_S) -> None:
        """Stop every session at once and wait at most `join_s` in all. Rows stay `recording` and the
        buffers stay on disk, so the next start resumes the sessions."""
        sessions = list(self.sessions.values())
        self.sessions.clear()
        threads = [
            threading.Thread(target=session.close, name=f"live-stop-{session.row.id[:8]}", daemon=True)
            for session in sessions
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + join_s
        for thread in threads:
            thread.join(max(0.05, deadline - time.monotonic()))
        self._log.info(
            "live_manager_shutdown", sessions=len(sessions), still_stopping=sum(t.is_alive() for t in threads)
        )

"""One live stream being followed: its recorder, its chat reader, its spike detector and what happens when
a spike closes.

Threads of a session (the manager polls it from its own loop):

- the recorder is two child processes, watched by `LiveSession.poll`;
- one chat thread runs an asyncio loop that reads the live chat into a `LiveSpikeDetector` and ticks the
  detector every `spikes.live_step_s`;
- each spike gets a short-lived thread (`_handle_spike`): tier check, P1 confirmation in the express
  budget lane, the cut from the buffer, and the render job.

**Reconnect policy** (`after_failure`, pure). A run that was refused by the platform (`blocked`) is never
retried by the session: the manager puts the platform into its back-off. The same happens when runs that
arrive slower than real time (`throttled`) use up the reconnects. Any other end of a run is
retried after `live.reconnect_backoff_s`, up to `live.max_reconnects` times in a row; a run that then
records for `live.reconnect_reset_s` clears the count. When the retries are used up, a session that
recorded anything ends as `ended` (its replay is scanned), one that never recorded as `failed`.

**Clocks.** Chat times are unix seconds; segments are indexed by wall clock; the detector already moved
the window start by `live.chat_offset_s` for the platform. Positions given to the factory are seconds in
the cut file. The host clock must be synchronised (NTP): chat times come from the platform, segment times
from this host. The stream's own start (`store.stream_origin`) anchors the detector's warm-up and
`live_sessions.started_at`.
"""

import asyncio
import contextlib
import threading
import time
from collections import Counter
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import structlog

from sixstars import chat, jobs
from sixstars.ai import calls, spike_confirm
from sixstars.ai.calls import ChatLine, SpikeContext
from sixstars.ai.schemas import SpikeVerdict
from sixstars.chat.models import ChatMessage, ChatSourceError, Platform
from sixstars.chat.net import cookies_file, proxy_for, proxy_refusal
from sixstars.clients.anthropic_client import AnthropicClient
from sixstars.factory.pipeline import JOB_CLIP_RENDER, JOB_EXPRESS_RENDER, RenderInput
from sixstars.live import buffer, store
from sixstars.live.config import LiveConfig
from sixstars.live.recorder import Failure, Recorder, Spawn, stream_url
from sixstars.logging import get_logger
from sixstars.scoring import config as scoring_config
from sixstars.scoring.percentile import Strength, spike_percentile
from sixstars.spikes import DetectedSpike, LiveSpikeDetector, SpikeConfig, persist_spike
from sixstars.spikes.baseline import Baseline, median_baseline
from sixstars.spikes.baseline import to_json as baseline_json
from sixstars.spikes.buckets import tokenise
from sixstars.vod.ytdlp import BLOCKED_MARKERS, normalise

OutcomeStatus = Literal["ended", "failed", "released"]
OpenChat = Callable[[Platform, str], AsyncGenerator[ChatMessage, None]]

# YouTube delivers chat in batches up to its poll interval late; Twitch IRC is immediate.
CHAT_LAG_S: Final[dict[str, float]] = {"youtube": 5.0, "twitch": 1.0}
CHAT_RETRY_S: Final = 30.0
CHAT_GIVE_UP_S: Final = 300.0  # a session whose chat never opened within this time is closed
LOOP_TICK_S: Final = 0.5
SWEEP_EVERY_S: Final = 10.0
TOP_TOKENS: Final = 6
SPIKE_JOIN_S: Final = 5.0
ENDED_KINDS: Final[frozenset[str]] = frozenset({"ended", "not_live"})


@dataclass(frozen=True, slots=True)
class Outcome:
    """How a session finished. `status` is the `live_sessions` status to store; `blocked` says that the
    platform refused this machine (the manager then backs the platform off)."""

    status: OutcomeStatus
    reason: str
    kind: str = ""
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class Route:
    """Where a confirmed live spike goes."""

    queue: jobs.Queue
    job_type: str
    priority: int
    lane: str
    mode: str


@dataclass(slots=True)
class SessionDeps:
    """Replaceable collaborators (tests pass fakes)."""

    open_chat: OpenChat | None = None
    ai_client: AnthropicClient | None = None
    spawn: Spawn | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    clock: Callable[[], float] = time.time
    on_data: Callable[["LiveSession"], None] | None = None  # first stream data of a recorder run


@dataclass(slots=True)
class SpikeTimings:
    """Wall time of each stage of one live spike before the render job, in milliseconds."""

    detect_lag_ms: int = 0  # from the chat peak to the detector reporting the spike
    confirm_ms: int = 0
    cut_wait_ms: int = 0
    cut_ms: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "detect_lag_ms": self.detect_lag_ms,
            "confirm_ms": self.confirm_ms,
            "cut_wait_ms": self.cut_wait_ms,
            "cut_ms": self.cut_ms,
        }


# ── pure rules ───────────────────────────────────────────────


def after_failure(
    failure: Failure, *, ever_recorded: bool, failures: int, max_reconnects: int
) -> Outcome | None:
    """What to do after a recorder run ended. `failures` counts the failed runs in a row including this
    one. Returns None to try again after the back-off, or the outcome that closes the session."""
    status: OutcomeStatus = "ended" if ever_recorded else "released"
    if failure.kind == "blocked":
        return Outcome(status, f"the platform refused this machine: {failure.detail}", failure.kind, True)
    if failures <= max_reconnects:
        return None
    if failure.kind == "throttled":
        # A slow spell can pass (seen: three minutes); only one that outlasts the reconnects is treated
        # as the platform throttling this machine.
        return Outcome(status, f"the platform throttles this machine: {failure.detail}", failure.kind, True)
    if ever_recorded:
        what = "the stream ended" if failure.kind in ENDED_KINDS else "recording could not be resumed"
        return Outcome("ended", f"{what} ({failure.kind}: {failure.detail})", failure.kind)
    return Outcome(
        "failed", f"the stream could not be recorded ({failure.kind}: {failure.detail})", failure.kind
    )


def route_for(tier: str, cfg: LiveConfig, *, emergency_stop: bool) -> Route | None:
    """The job a confirmed live spike becomes. S goes to the express queue and the spike lane; under the
    emergency stop nothing can be posted now, so an S spike takes the hourly route instead of spending on
    the express path. A and B go to the normal queue and the hourly lane (None when `live.ab_clips` is
    off)."""
    if tier == "S" and not emergency_stop:
        return Route("express", JOB_EXPRESS_RENDER, cfg.priority_express, "spike", "express")
    if tier not in ("S", "A", "B") or not cfg.ab_clips:
        return None
    priority = cfg.priority_b if tier == "B" else cfg.priority_a
    return Route("normal", JOB_CLIP_RENDER, priority, "hourly", "normal")


def top_tokens(sample: Iterable[Mapping[str, Any]], count: int = TOP_TOKENS) -> list[str]:
    """The most common words of a chat sample, for the confirmation prompt."""
    words: Counter[str] = Counter()
    for entry in sample:
        words.update(set(tokenise(str(entry.get("text", "")))))
    return [word for word, _ in words.most_common(count)]


def spike_context(
    spike_id: str, spike: DetectedSpike, creator: str, platform: str, stream_title: str = ""
) -> SpikeContext:
    """What P1 sees about a live spike: the chat sample only (no transcript, for speed)."""
    lines = tuple(
        ChatLine(t_s=float(entry["t_rel"]), text=str(entry.get("text", "")))
        for entry in spike.chat_sample
        if isinstance(entry.get("t_rel"), int | float)
    )
    return SpikeContext(
        spike_id=spike_id,
        creator=creator,
        platform=platform,
        stream_title=stream_title,
        score_z=spike.score_z,
        msgs=spike.msgs,
        uniq=spike.uniq,
        top_tokens=top_tokens(spike.chat_sample),
        chat=lines,
        transcript=None,
    )


def cut_range(spike: DetectedSpike, route: Route, express_pad_wait_s: float) -> tuple[float, float]:
    """The wall-clock range to cut for a spike: its padded window. On the express route the context
    after the window is limited to `live.express_pad_wait_s`, because every second of it is waited for."""
    end = spike.pad_end
    if route.mode == "express":
        end = min(end, spike.window_end + max(express_pad_wait_s, 0.0))
    return spike.pad_start, max(end, spike.window_end)


def render_input(
    spike: DetectedSpike,
    spike_id: str,
    cut: buffer.Cut,
    session: store.SessionRow,
    route: Route,
    verdict: SpikeVerdict | None,
    *,
    stream_start: float,
    chat_offset_s: float,
) -> RenderInput:
    """The factory input for a live spike: every position is converted from wall clock to seconds in the
    cut file. Raises `buffer.BufferCutError` when the cut holds none of the window."""
    start, end = cut.offset(spike.window_start), cut.offset(spike.window_end)
    if end - start <= 0:
        raise buffer.BufferCutError("the cut holds none of the spike window")
    floor = min(max(cut.offset(spike.spike_floor - chat_offset_s), start), end)
    lines = tuple(
        ChatLine(
            t_s=round(cut.offset(spike.peak_t - chat_offset_s + float(entry["t_rel"])), 2),
            text=str(entry.get("text", "")),
            user=str(entry["user"]) if entry.get("user") else None,
        )
        for entry in spike.chat_sample
        if isinstance(entry.get("t_rel"), int | float)
    )
    return RenderInput(
        source_path=cut.path,
        window_start_s=round(start, 3),
        window_end_s=round(end, 3),
        creator=session.handle,
        spike_floor_s=round(floor, 3),
        creator_id=session.creator_id,
        platform=session.platform,
        source_id=session.source_id,
        source_url=session.url,
        source_type="live",
        lane=route.lane,
        mode=route.mode,
        stream_title=session.title,
        spike_id=spike_id,
        pad_start_s=round(cut.offset(spike.pad_start), 3),
        pad_end_s=round(cut.offset(spike.pad_end), 3),
        source_offset_s=round(max(cut.file_start - stream_start, 0.0), 3),
        verdict=verdict,
        chat=lines,
    )


def prior_baseline(row: store.SessionRow) -> Baseline | None:
    """The creator's stored chat baseline (median of recent sessions) when `warmup.v2` is on."""
    flag = scoring_config.flag("warmup.v2")
    if flag is None or not row.creator_id:
        return None
    try:
        stored = store.prior_chat_baselines(
            row.creator_id, row.platform, row.id, int(scoring_config.number(flag, "prior_sessions", 5.0))
        )
        return median_baseline(stored, SpikeConfig.from_settings().min_std)
    except Exception as exc:  # noqa: BLE001 - without a prior the stream waits for its own baseline
        get_logger("live").warning("live_prior_baseline_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return None


def looks_blocked(text: str) -> bool:
    lowered = normalise(text)
    return any(marker in lowered for marker in BLOCKED_MARKERS) or proxy_refusal(text) is not None


@dataclass(slots=True)
class _SpikeHandle:
    """A live spike being handled, so a stronger one inside the merge gap can cancel it
    (`merge.keep_stronger`)."""

    spike_id: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    job_id: int | None = None
    merged_into: str | None = None


def fast_path_reason(spike: DetectedSpike, strength: Strength, flag: Mapping[str, Any] | None) -> str | None:
    """Why a live spike takes the fast path (`fastpath.live`), or None. Pure."""
    if flag is None:
        return None
    percentile = scoring_config.number(flag, "percentile", 97.0)
    if strength.at_least(percentile):
        return f"p{strength.percentile:g} of {strength.basis}"
    if flag.get("include_s_tier") is True and spike.tier == "S":
        return "provisional tier S"
    return None


# ── the session ──────────────────────────────────────────────


@dataclass(slots=True)
class _Counters:
    chat_messages: int = 0
    spikes: int = 0
    queued: int = 0
    last_chat_at: float = 0.0
    chat_opened: bool = False
    chat_error: str = ""
    extra: dict[str, int] = field(default_factory=dict)


class LiveSession:
    """Follows one stream. `start()`, then `poll()` about once a second until it returns an `Outcome`,
    then `close()`. `stop()` ends it early (slot released, or the service is shutting down)."""

    def __init__(self, row: store.SessionRow, cfg: LiveConfig, deps: SessionDeps | None = None) -> None:
        self.row = row
        self.cfg = cfg
        self.deps = deps or SessionDeps()
        self.directory = store.buffer_dir(row.id)
        self.ever_recorded = row.status == "recording"
        self.resumed = row.status == "recording"
        self.outcome: Outcome | None = None
        self._log = get_logger("live").bind(session_id=row.id, platform=row.platform)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._failures = 0
        self._retry_at: float | None = None
        self._run_started = 0.0
        self._run_has_data = False
        self._last_sweep = 0.0
        self._created = self.deps.clock()
        self._counters = _Counters()
        self._bots_saved = False
        self._spike_threads: list[threading.Thread] = []
        self._chat_thread: threading.Thread | None = None
        started = store.stream_origin(row, self._created)
        self.stream_start = started
        self.recorder = Recorder(
            stream_url(row.platform, row.channel_login, row.source_id, row.url),
            row.platform,
            self.directory,
            segment_s=cfg.segment_s,
            max_height=cfg.max_height,
            start_timeout_s=cfg.start_timeout_s,
            stall_timeout_s=cfg.stall_timeout_s,
            ad_break_max_s=cfg.ad_break_max_s,
            max_lag_s=cfg.max_lag_s,
            proxy=proxy_for(self._platform, "live"),
            cookies_file=cookies_file() if row.platform == "youtube" else None,
            ffmpeg=self.deps.ffmpeg,
            spawn=self.deps.spawn,
        )
        self.detector = LiveSpikeDetector(
            self._platform, started, bot_names=row.bot_names, prior=prior_baseline(row)
        )
        self._baseline_saved_at = self._created
        self._recent_spikes: dict[float, _SpikeHandle] = {}

    @property
    def _platform(self) -> Platform:
        return "twitch" if self.row.platform == "twitch" else "youtube"

    # ── lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        self._start_run()
        self._chat_thread = threading.Thread(
            target=self._chat_main, name=f"live-chat-{self.row.id[:8]}", daemon=True
        )
        self._chat_thread.start()

    def _start_run(self) -> None:
        self._retry_at = None
        self._run_has_data = False
        self._run_started = self.deps.clock()
        try:
            self.recorder.start()
        except OSError as exc:
            self._log.error("recorder_start_failed", error=type(exc).__name__)
            self._on_failure(Failure("error", f"the recorder could not start: {type(exc).__name__}"))

    def _on_failure(self, failure: Failure) -> None:
        self._failures += 1
        outcome = after_failure(
            failure,
            ever_recorded=self.ever_recorded,
            failures=self._failures,
            max_reconnects=self.cfg.max_reconnects,
        )
        if outcome is not None:
            self.outcome = outcome
            return
        store.add_reconnect(self.row.id, failure.kind, failure.detail, self._failures)
        self._retry_at = self.deps.clock() + self.cfg.reconnect_backoff_s

    def poll(self) -> Outcome | None:
        """Advance the session: watch the recorder, restart it after the back-off, sweep the buffer.
        Returns the outcome once the session is over (the caller then calls `close`)."""
        if self.outcome is not None or self._stop.is_set():
            return self.outcome
        now = self.deps.clock()
        status = self.recorder.poll()
        if status.running:
            if status.has_data and not self._run_has_data:
                self._run_has_data = True
                self.ever_recorded = True
                if self.deps.on_data is not None:
                    self.deps.on_data(self)
            if self._failures and status.has_data and now - self._run_started > self.cfg.reconnect_reset_s:
                self._failures = 0
            self._check_chat(now)
        elif status.failure is not None:
            self._on_failure(status.failure)
        elif self._retry_at is not None and now >= self._retry_at:
            self._start_run()
        if now - self._last_sweep >= SWEEP_EVERY_S:
            self._last_sweep = now
            self.sweep(now)
        return self.outcome

    def _check_chat(self, now: float) -> None:
        """Close a session whose chat cannot be read: without chat there is nothing to detect."""
        counters = self._counters
        if counters.chat_opened or now - self._created < CHAT_GIVE_UP_S or not counters.chat_error:
            return
        blocked = looks_blocked(counters.chat_error)
        status: OutcomeStatus = "ended" if self.ever_recorded else ("released" if blocked else "failed")
        self.outcome = Outcome(
            status, f"the live chat cannot be read: {counters.chat_error}"[:300], "chat", blocked
        )

    def sweep(self, now: float) -> None:
        """Keep the buffer inside `live.buffer_s` and `live.max_buffer_bytes`; drop old cuts."""
        buffer.sweep(
            self.directory,
            now=now,
            buffer_s=self.cfg.buffer_s,
            max_bytes=self.cfg.max_buffer_bytes,
            pin_max_age_s=self.cfg.cut_wait_max_s + buffer.CUT_TIMEOUT_S,
        )
        buffer.sweep_cuts(self.directory, now=now, keep_s=self.cfg.cut_keep_s)

    def stop(self, timeout_s: float | None = None) -> None:
        """Stop recording and chat reading. Spikes already being handled are given a moment to finish."""
        self._stop.set()
        self.recorder.stop(self.cfg.stop_timeout_s if timeout_s is None else timeout_s)

    def close(self, wait_s: float = SPIKE_JOIN_S) -> None:
        """Stop (if not stopped yet) and wait for the session's threads, at most `wait_s` in all."""
        self.stop()
        deadline = time.monotonic() + max(wait_s, 0.0)
        threads = [*self._spike_threads, *([self._chat_thread] if self._chat_thread else [])]
        for thread in threads:
            thread.join(max(0.05, deadline - time.monotonic()))

    def busy(self) -> bool:
        """True while a spike of this session is still being handled (its cut needs the buffer)."""
        return any(thread.is_alive() for thread in self._spike_threads)

    def info(self) -> dict[str, Any]:
        """The session's state for the heartbeat and `sixstars live status`."""
        counters = self._counters
        return {
            "id": self.row.id,
            "platform": self.row.platform,
            "creator": self.row.handle,
            "recording": self._run_has_data and self._retry_at is None,
            "run": self.recorder.run,
            "failures": self._failures,
            "chat_messages": counters.chat_messages,
            "chat_open": counters.chat_opened,
            "spikes": counters.spikes,
            "queued": counters.queued,
        }

    # ── chat and detection ───────────────────────────────────

    def inject(self, messages: Sequence[ChatMessage]) -> None:
        """Feed messages into the detector as if the chat had delivered them (checks and tests)."""
        with self._lock:
            for message in messages:
                self.detector.feed(message)
            self._counters.chat_messages += len(messages)

    def _chat_main(self) -> None:
        try:
            asyncio.run(self._chat_loop())
        except Exception as exc:  # noqa: BLE001 - a chat thread must never take the service down
            self._log.error("live_chat_thread_failed", error=type(exc).__name__, exc_info=True)

    async def _chat_loop(self) -> None:
        reader = asyncio.create_task(self._read_chat())
        try:
            next_tick = 0.0
            while not self._stop.is_set():
                await asyncio.sleep(LOOP_TICK_S)
                now = self.deps.clock()
                if now < next_tick:
                    continue
                step = self._tick(now)
                next_tick = now + step
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader

    def _tick(self, now: float) -> float:
        """One detector step. Returns the seconds until the next one (`spikes.live_step_s`)."""
        step = 5.0
        try:
            with self._lock:
                spikes = self.detector.tick(now - CHAT_LAG_S.get(self.row.platform, 5.0))
                learned = self.detector.bots_learned
                bots = self.detector.bot_names
            step = float(SpikeConfig.from_settings().live_step_s)
            if learned and not self._bots_saved:
                self._bots_saved = True
                store.save_bots(self.row.id, bots)
            self._maybe_save_baseline(now)
        except Exception as exc:  # noqa: BLE001 - one bad step (database hiccup) must not end detection
            self._log.error("live_tick_failed", error=type(exc).__name__, exc_info=True)
            return step
        for spike in spikes:
            self.handle_spike_async(spike)
        return max(step, 1.0)

    def _maybe_save_baseline(self, now: float) -> None:
        """Store the session's chat baseline every `warmup.v2.save_every_s` (default 300 s), so the next
        stream of this creator can start with it. Saved whatever the flag: it is data, not behaviour."""
        every = scoring_config.number(scoring_config.section("warmup.v2"), "save_every_s", 300.0)
        if now - self._baseline_saved_at < max(every, 30.0):
            return
        self._baseline_saved_at = now
        try:
            with self._lock:
                baseline = self.detector.current_baseline(now - CHAT_LAG_S.get(self.row.platform, 5.0))
            if baseline is not None:
                store.save_chat_baseline(self.row.id, baseline_json(baseline, now))
        except Exception as exc:  # noqa: BLE001 - never lose the tick's spikes over the stored baseline
            self._log.warning("live_baseline_save_failed", error=f"{type(exc).__name__}: {exc}"[:200])

    async def _read_chat(self) -> None:
        open_chat: OpenChat = self.deps.open_chat or (
            lambda platform, channel: chat.open_live(platform, channel)
        )
        channel = self.row.channel_login if self.row.platform == "twitch" else self.row.source_id
        counters = self._counters
        while not self._stop.is_set():
            stream = open_chat(self._platform, channel)
            try:
                async for message in stream:
                    with self._lock:
                        self.detector.feed(message)
                    counters.chat_messages += 1
                    counters.chat_opened = True
                    counters.last_chat_at = self.deps.clock()
                self._log.info("live_chat_ended")
            except ChatSourceError as exc:
                counters.chat_error = str(exc)[:300]
                self._log.warning("live_chat_unavailable", error=str(exc)[:300])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retried below
                counters.chat_error = f"{type(exc).__name__}: {exc}"[:300]
                self._log.warning("live_chat_failed", error=type(exc).__name__)
            finally:
                with contextlib.suppress(Exception):
                    await stream.aclose()
            await asyncio.sleep(CHAT_RETRY_S)

    # ── spikes ───────────────────────────────────────────────

    def handle_spike_async(self, spike: DetectedSpike) -> threading.Thread:
        """Handle one closed spike in its own thread, so chat reading and detection go on."""
        self._counters.spikes += 1
        self._spike_threads = [thread for thread in self._spike_threads if thread.is_alive()]
        thread = threading.Thread(
            target=self._handle_spike_safely, args=(spike,), name=f"live-spike-{self.row.id[:8]}", daemon=True
        )
        self._spike_threads.append(thread)
        thread.start()
        return thread

    def _handle_spike_safely(self, spike: DetectedSpike) -> None:
        with structlog.contextvars.bound_contextvars(session_id=self.row.id):
            try:
                self.handle_spike(spike)
            except Exception as exc:  # noqa: BLE001 - a spike that cannot be handled is logged, never fatal
                self._log.error(
                    "live_spike_failed", error=f"{type(exc).__name__}: {exc}"[:300], exc_info=True
                )

    def handle_spike(self, spike: DetectedSpike) -> int | None:
        """Persist, confirm, cut and queue one live spike. Returns the render job id, or None when the
        spike was dropped, rejected, or could not be cut."""
        detected_at = self.deps.clock()
        timings = SpikeTimings(detect_lag_ms=int(max(detected_at - spike.peak_t, 0.0) * 1000))
        tier = store.creator_tier(self.row.creator_id)
        if tier is None or tier in store.FORBIDDEN_TIERS:
            store.emit(
                "live.spike_dropped",
                self.row.id,
                f"spike dropped before any spend: creator tier is {tier}",
                {"tier": tier, "peak": spike.peak_t, "score_z": spike.score_z},
            )
            return None
        spike_id = persist_spike(spike, creator_id=self.row.creator_id, live_session_id=self.row.id)
        with structlog.contextvars.bound_contextvars(spike_id=spike_id):
            status = store.spike_status(spike_id)
            if status != "detected":
                # The same spike again (a restarted detector re-reads the chat backlog): it was handled.
                self._log.info("live_spike_already_handled", status=status)
                return None
            try:
                return self._confirm_cut_queue(spike, spike_id, timings, detected_at)
            except Exception as exc:
                store.close_spike(
                    spike_id,
                    "failed",
                    f"{type(exc).__name__}: {exc}",
                    event="live.spike_failed",
                    level="warn",
                )
                raise

    def _supersede(self, spike: DetectedSpike, spike_id: str) -> None:
        """`merge.keep_stronger`: this confirmed spike replaces a weaker one inside the merge gap. The weaker
        one is cancelled if its render has not started; otherwise both go on and the gate's duplicate
        checks decide."""
        for peak in [peak for peak in self._recent_spikes if peak < spike.peak_t - 600]:
            del self._recent_spikes[peak]
        if spike.supersedes_peak_t is None:
            return
        earlier = self._recent_spikes.get(spike.supersedes_peak_t)
        if earlier is None or earlier.spike_id == spike_id:
            return
        earlier.merged_into = spike_id
        earlier.cancelled.set()
        if earlier.job_id is not None and store.cancel_queued_job(earlier.job_id):
            store.merge_spike(earlier.spike_id, spike_id, "replaced by a stronger spike inside the merge gap")
        self._log.info("live_spike_superseded", earlier=earlier.spike_id, job_id=earlier.job_id)

    def _confirm_cut_queue(
        self, spike: DetectedSpike, spike_id: str, timings: SpikeTimings, detected_at: float
    ) -> int | None:
        handle = _SpikeHandle(spike_id)
        self._recent_spikes[spike.peak_t] = handle
        started = time.monotonic()
        strength = spike_percentile(self.row.creator_id, spike.score_z, spike_id)
        fast_flag = scoring_config.flag("fastpath.live")
        fast_why = fast_path_reason(spike, strength, fast_flag)
        context = spike_context(spike_id, spike, self.row.handle, self.row.platform, self.row.title)
        verdict = calls.confirm_spikes([context], lane="express", client=self.deps.ai_client)[0]
        timings.confirm_ms = int((time.monotonic() - started) * 1000)
        decision = spike_confirm.apply(
            spike_id,
            spike.tier,
            verdict,
            fast=None
            if fast_why is None or fast_flag is None
            else (scoring_config.strings(fast_flag, "keep_reject_terms"), fast_why),
            creator_id=self.row.creator_id,
            sub_scores={
                "mode": "live",
                "score_z": spike.score_z,
                "parts": spike.parts,
                "msgs": spike.msgs,
                "uniq": spike.uniq,
                "reaction_count": spike.reaction_count,
                "percentile": strength.percentile,
                "percentile_basis": strength.basis,
                "percentile_history": strength.history,
                "prior_baseline_steps": self.detector.prior_used,
                "supersedes": spike.supersedes_peak_t is not None,
            },
        )
        self._log.info(
            "live_spike_judged",
            status=decision.status,
            tier=decision.tier,
            confidence=decision.confidence,
            fast_path=fast_why,
        )
        if decision.status != "confirmed":
            return None
        self._supersede(spike, spike_id)
        route = route_for(decision.tier, self.cfg, emergency_stop=store.emergency_stop())
        if route is None:
            store.close_spike(
                spike_id,
                "rejected",
                f"tier {decision.tier} live spikes are not clipped (live.ab_clips)",
                event="live.spike_skipped",
            )
            return None
        start_wall, end_wall = cut_range(spike, route, self.cfg.express_pad_wait_s)
        wait_s = buffer.wait_seconds(
            end_wall, self.deps.clock(), self.cfg.cut_settle_s, self.cfg.cut_wait_max_s
        )
        waited = time.monotonic()
        if wait_s > 0:
            self._stop.wait(wait_s)
        timings.cut_wait_ms = int((time.monotonic() - waited) * 1000)
        cutting = time.monotonic()
        cut = buffer.extract(
            self.directory,
            start_wall,
            end_wall,
            buffer.cut_path(self.directory, spike_id),
            anchor_span_s=self.cfg.anchor_span_s,
            ffmpeg=self.deps.ffmpeg,
            ffprobe=self.deps.ffprobe,
            pin_key=spike_id,
        )
        timings.cut_ms = int((time.monotonic() - cutting) * 1000)
        spike_cfg = SpikeConfig.from_settings()
        item = render_input(
            spike,
            spike_id,
            cut,
            self.row,
            route,
            verdict,
            stream_start=self.stream_start,
            chat_offset_s=spike_cfg.offset_for(self._platform),
        )
        if item.window_end_s - item.window_start_s < spike_cfg.min_len_s:
            raise buffer.BufferCutError(
                f"the buffer holds only {item.window_end_s - item.window_start_s:.1f} s of the window"
            )
        if handle.cancelled.is_set() and handle.merged_into:
            store.merge_spike(
                spike_id, handle.merged_into, "replaced by a stronger spike inside the merge gap"
            )
            return None
        queued_at = self.deps.clock()
        live = {
            "session_id": self.row.id,
            "spike_wall": spike.peak_t,
            "start_wall": spike.window_start,
            "end_wall": spike.window_end,
            "cut_start_wall": cut.file_start,
            "detected_at": detected_at,
            "queued_at": queued_at,
            "tier": decision.tier,
            "timings_ms": timings.as_dict(),
        }
        job_id = jobs.enqueue(
            route.queue,
            route.job_type,
            {**item.to_payload(), "live": live},
            priority=route.priority,
            dedupe_key=f"{route.job_type}:{spike_id}",
        )
        handle.job_id = job_id
        self._counters.queued += 1
        store.emit(
            "live.spike_queued",
            self.row.id,
            f"tier {decision.tier} spike queued as {route.job_type}",
            {
                "spike_id": spike_id,
                "job_id": job_id,
                "lane": route.lane,
                "tier": decision.tier,
                "spike_wall": datetime.fromtimestamp(spike.peak_t, UTC).isoformat(),
                "spike_to_queue_s": round(queued_at - spike.peak_t, 1),
                "cut_bytes": cut.size_bytes,
                "cut_s": round(cut.duration_s, 1),
                "timings_ms": timings.as_dict(),
            },
        )
        self._log.info(
            "live_spike_queued",
            job_id=job_id,
            job_type=route.job_type,
            tier=decision.tier,
            **timings.as_dict(),
        )
        return job_id


def session_dir_ids(root: Path) -> list[str]:
    """Names of the session directories under the live root."""
    try:
        return sorted(path.name for path in root.iterdir() if path.is_dir())
    except OSError:
        return []

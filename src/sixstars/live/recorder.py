"""Stream recorder: captures a live stream with streamlink into segment files on the data volume.

One recorder run is two processes joined by a pipe, without a shell:

    streamlink --stdout <url> best  |  ffmpeg -i pipe:0 -c copy -f segment ... seg_<run>_<UTC time>.ts

The video and audio are copied, never encoded, so a slot costs almost no CPU. Each segment file name
carries the run number and the wall-clock second (UTC) at which ffmpeg opened it; timestamps inside the
files continue across the segments of a run, so a run can be joined byte for byte (see `buffer.py`).

A run that ends is classified from what streamlink printed (`classify_failure`): `blocked` (the platform
refuses this machine: bot check, rate limit), `ended` / `not_live` (nothing is live at the URL any more),
`network`, or `error`. The session decides what to do with that; this module only reports it.

A platform can also serve a stream without refusing it and still be unusable: YouTube sometimes throttles
the segment downloads, so the stream arrives at a fraction of real time. The run looks alive, but the
buffer falls further behind the wall clock every minute and a window cut from it would show the wrong
moment. `Recorder.poll` therefore compares, per closed segment, the wall clock with the stream's own
timestamps (`delivery_lag`) and ends a run that is more than `live.max_lag_s` behind as `throttled`.

Every run is bounded: streamlink's own HTTP and stream timeouts, a start timeout and a stall timeout
watched by `Recorder.poll`, and bounded waits when stopping.
"""

import contextlib
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final, Literal

from sixstars.chat.net import proxy_refusal, redact_proxy
from sixstars.live import buffer
from sixstars.logging import get_logger
from sixstars.vod.ytdlp import BLOCKED_MARKERS, normalise

FailureKind = Literal[
    "blocked", "throttled", "ended", "not_live", "network", "error", "stalled", "start_timeout"
]

STDERR_KEEP_LINES: Final = 40
DETAIL_CHARS: Final = 300
HTTP_TIMEOUT_S: Final = 20
STREAM_TIMEOUT_S: Final = 30
RETRY_OPEN: Final = 2
KILL_WAIT_S: Final = 3.0
ARRIVALS_KEEP: Final = 64  # closed segments whose first timestamp is remembered for the lag watch

# Matched against streamlink's lower-cased output. The bot check is tested first.
LIVE_BLOCKED_MARKERS: Final[tuple[str, ...]] = (
    *BLOCKED_MARKERS,
    "login_required",
    "429 client error",
    "captcha",
)
ENDED_MARKERS: Final[tuple[str, ...]] = (
    "stream ended",
    "this live event has ended",
    "live stream recording is not available",
)
NOT_LIVE_MARKERS: Final[tuple[str, ...]] = (
    "no playable streams found",
    "is offline",
    "not currently live",
    "this live event will begin",
    "video unavailable",
    "private video",
    "members-only",
    "could not find a stream",
    "the specified stream(s)",
)
NETWORK_MARKERS: Final[tuple[str, ...]] = (
    "unable to open url",
    "failed to establish a new connection",
    "connection aborted",
    "connection reset",
    "remote end closed connection",
    "read timeout",
    "read timed out",
    "timed out",
    "name or service not known",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "failed to open segment",
    "failed to reload playlist",
    "sslerror",
    "proxyerror",
)


# Log lines of streamlink (stream/hls/hls.py, plugins/twitch.py) around a Twitch ad break. With
# --twitch-disable-ads it writes nothing while ads run, which must not be taken for a stalled stream.
AD_START_MARKERS: Final[tuple[str, ...]] = (
    "filtering out segments and pausing stream output",
    "waiting for pre-roll ads to finish",
)
AD_END_MARKERS: Final[tuple[str, ...]] = ("resuming stream output",)


@dataclass(frozen=True, slots=True)
class Failure:
    """Why a recorder run ended. `detail` is the last error line of streamlink (proxy scrubbed)."""

    kind: FailureKind
    detail: str


def last_error_line(text: str) -> str:
    """The last line of the output that reports an error, else its last non-empty line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line.lower()]
    chosen = errors[-1] if errors else (lines[-1] if lines else "")
    return chosen[-DETAIL_CHARS:]


def classify_failure(output: str, *, had_data: bool) -> Failure:
    """Turn the output of a recorder run that ended into a `Failure`. Pure.

    `had_data` says whether the run wrote any stream data. A run that recorded and then stopped without a
    recognisable error is `ended` (the playlist finished); one that never produced data is `error`.
    """
    text = normalise(output)
    detail = last_error_line(output)
    refusal = proxy_refusal(output)
    if refusal is not None and (refusal.status is not None or not had_data):
        # The proxy itself refused (or was unreachable before any data came): the platform's blocked
        # state, not a crash. A proxy connection lost in the middle of a recording stays `network`.
        return Failure("blocked", refusal.described)
    if any(marker in text for marker in LIVE_BLOCKED_MARKERS):
        # Name the refusal itself, not the "no playable streams" line streamlink prints after it.
        refusals = [
            line.strip()
            for line in output.splitlines()
            if any(marker in normalise(line) for marker in LIVE_BLOCKED_MARKERS)
        ]
        return Failure("blocked", refusals[-1][-DETAIL_CHARS:] if refusals else detail)
    if any(marker in text for marker in ENDED_MARKERS):
        return Failure("ended", detail or "the stream ended")
    if any(marker in text for marker in NOT_LIVE_MARKERS):
        return Failure("not_live", detail or "nothing is live at this URL")
    if any(marker in text for marker in NETWORK_MARKERS):
        return Failure("network", detail or "network error")
    if had_data:
        return Failure("ended", detail or "the stream ended")
    return Failure("error", detail or "the recorder stopped without output")


def delivery_lag(arrivals: Sequence[tuple[float, float]], jump_slack_s: float = 5.0) -> float:
    """How far the newest segment of a run is behind real time, in seconds. Pure.

    `arrivals` holds, per segment in order, the wall-clock second it was opened and the first stream
    timestamp in it. `wall - timestamp` is constant while the stream arrives in real time and grows when
    it arrives more slowly (a platform throttling the downloads). The lag is the newest value minus the
    smallest one seen. Delivery can fall behind by at most the wall time that passed, so a larger step
    between two segments is a jump of the stream's clock, not a delay: the comparison starts again there.
    """
    best: float | None = None
    previous: tuple[float, float] | None = None
    lag = 0.0
    for wall, stamp in arrivals:
        offset = wall - stamp
        if previous is not None:
            step = offset - (previous[0] - previous[1])
            if abs(step) > (wall - previous[0]) + jump_slack_s:
                best = None  # the stream's timestamps jumped
        best = offset if best is None else min(best, offset)
        lag = offset - best
        previous = (wall, stamp)
    return lag


def stream_url(platform: str, channel_login: str, source_id: str, url: str) -> str:
    """The URL streamlink records. Twitch: the channel page (needs no API key). YouTube: the watch page
    of the live video."""
    if platform == "twitch":
        return f"https://www.twitch.tv/{channel_login}"
    return url or f"https://www.youtube.com/watch?v={source_id}"


def streamlink_command(
    url: str,
    platform: str,
    *,
    max_height: int,
    proxy: str | None = None,
    cookies_file: Path | None = None,
    python: str | None = None,
) -> list[str]:
    """The streamlink half of a recorder run.

    `--stream-types hls` matters on YouTube: without it streamlink plays the finished video of an ended
    stream from its start instead of reporting that nothing is live.
    """
    command = [
        python or sys.executable,
        "-m",
        "streamlink",
        "--stdout",
        "--loglevel",
        "info",
        "--stream-types",
        "hls",
        "--http-timeout",
        str(HTTP_TIMEOUT_S),
        "--stream-timeout",
        str(STREAM_TIMEOUT_S),
        "--retry-open",
        str(RETRY_OPEN),
        "--stream-sorting-excludes",
        f">{max_height}p60",  # "1080p60" sorts above "1080p": excluding ">1080p" would drop it
    ]
    if platform == "twitch":
        command += ["--twitch-disable-ads"]
    if proxy:
        command += ["--http-proxy", proxy]
    if cookies_file is not None and platform == "youtube":
        command += ["--http-cookies-file", str(cookies_file)]
    return [*command, url, "best"]


def ffmpeg_command(directory: Path, run: int, segment_s: float, ffmpeg: str = "ffmpeg") -> list[str]:
    """The ffmpeg half: stream copy from the pipe into segment files named by run and UTC time."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        f"{segment_s:g}",
        "-segment_format",
        "mpegts",
        "-strftime",
        "1",
        "-flush_packets",
        "1",
        str(directory / buffer.segment_pattern(run)),
    ]


def ad_state(line: str) -> bool | None:
    """What one line of the streamlink log says about ads: True when it starts skipping ad segments (its
    output pauses), False when the stream resumes, None for any other line. Pure."""
    lowered = line.lower()
    if any(marker in lowered for marker in AD_START_MARKERS):
        return True
    if any(marker in lowered for marker in AD_END_MARKERS):
        return False
    return None


def scrub(text: str, proxy: str | None) -> str:
    """Remove the proxy URL and any credentials inside a URL from text that will be logged or stored."""
    return redact_proxy(text, proxy)


class _Tail:
    """Collects the last lines a process writes to a pipe, in a daemon thread."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._lines: deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        self.filtering = False  # streamlink is skipping ad segments and writes nothing meanwhile
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: IO[bytes]) -> None:
        with contextlib.suppress(Exception):
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                self._lines.append(line)
                state = ad_state(line)
                if state is not None:
                    self.filtering = state
        with contextlib.suppress(Exception):
            stream.close()

    def text(self, wait_s: float = 1.0) -> str:
        self._thread.join(wait_s)
        return "\n".join(self._lines)


class RunHandle:
    """The two processes of one recorder run. Tests replace it with a fake that has the same methods."""

    def __init__(self, streamlink: Sequence[str], ffmpeg: Sequence[str], env: dict[str, str]) -> None:
        self.source = subprocess.Popen(  # noqa: S603 - fixed argument list, no shell
            list(streamlink),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert self.source.stdout is not None and self.source.stderr is not None
        try:
            self.sink = subprocess.Popen(  # noqa: S603 - fixed argument list, no shell
                list(ffmpeg),
                stdin=self.source.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError:
            self.source.kill()
            raise
        self.source.stdout.close()  # ffmpeg holds the read end now
        assert self.sink.stderr is not None
        self._source_tail = _Tail(self.source.stderr)
        self._sink_tail = _Tail(self.sink.stderr)

    def alive(self) -> bool:
        return self.source.poll() is None and self.sink.poll() is None

    def output(self) -> str:
        """What both processes printed (streamlink first)."""
        return self._source_tail.text() + "\n" + self._sink_tail.text()

    def filtering(self) -> bool:
        """True while streamlink is skipping an ad break (it writes no data then)."""
        return self._source_tail.filtering

    def stop(self, timeout_s: float) -> None:
        """Stop streamlink first so ffmpeg sees the end of its input and closes the last segment; kill
        whatever is still running after `timeout_s`."""
        if self.source.poll() is None:
            with contextlib.suppress(OSError):
                self.source.terminate()
        deadline = time.monotonic() + max(0.0, timeout_s)
        for process in (self.source, self.sink):
            try:
                process.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(KILL_WAIT_S)

    def kill(self) -> None:
        """Kill both processes at once, as a crash would."""
        for process in (self.source, self.sink):
            with contextlib.suppress(OSError):
                process.kill()


Spawn = Callable[[Sequence[str], Sequence[str], dict[str, str]], RunHandle]


@dataclass(frozen=True, slots=True)
class RecorderStatus:
    """What `Recorder.poll` saw. `failure` is set once, when a run has ended or was stopped as stuck."""

    running: bool
    has_data: bool  # this run wrote stream data
    failure: Failure | None = None
    bytes_written: int = 0


class Recorder:
    """One stream's recorder: starts runs, watches them and reports how each one ended.

    The caller drives it: `start()` begins a run, `poll()` (every second or so) says whether the run is
    alive, has data, has stalled or has ended, and `stop()` ends it. Nothing here retries; the session
    owns the reconnect policy.
    """

    def __init__(
        self,
        url: str,
        platform: str,
        directory: Path,
        *,
        segment_s: float,
        max_height: int,
        start_timeout_s: float,
        stall_timeout_s: float,
        ad_break_max_s: float = 300.0,
        max_lag_s: float = 0.0,
        probe: Callable[[Path], float | None] | None = None,
        proxy: str | None = None,
        cookies_file: Path | None = None,
        ffmpeg: str = "ffmpeg",
        spawn: Spawn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.platform = platform
        self.directory = directory
        self.segment_s = segment_s
        self.max_height = max_height
        self.start_timeout_s = start_timeout_s
        self.stall_timeout_s = stall_timeout_s
        self.ad_break_max_s = ad_break_max_s
        self.max_lag_s = max_lag_s  # 0 = do not watch the delivery lag
        self._probe = probe or buffer.probe_start
        self._arrivals: dict[str, tuple[float, float]] = {}
        self._lag_from = 0.0  # wall time before which segments do not count for the lag
        self._proxy = proxy
        self._cookies_file = cookies_file
        self._ffmpeg = ffmpeg
        self._spawn: Spawn = spawn or RunHandle
        self._clock = clock
        self._handle: RunHandle | None = None
        self._run = 0
        self._started_at = 0.0
        self._last_growth = 0.0
        self._newest: tuple[str, int] | None = None
        self._ad_started: float | None = None
        self._log = get_logger("live")

    def _ad_since(self, now: float) -> float:
        if self._ad_started is None:
            self._ad_started = now
        return self._ad_started

    @property
    def run(self) -> int:
        """Number of the current (or last) run; segment names carry it."""
        return self._run

    def start(self) -> int:
        """Begin a new run and return its number. The previous run must have ended."""
        if self._handle is not None and self._handle.alive():
            raise RuntimeError("the previous recorder run is still alive")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._run = buffer.next_run(self.directory)
        env = {**os.environ, "TZ": "UTC0", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        source = streamlink_command(
            self.url,
            self.platform,
            max_height=self.max_height,
            proxy=self._proxy,
            cookies_file=self._cookies_file,
        )
        sink = ffmpeg_command(self.directory, self._run, self.segment_s, self._ffmpeg)
        self._handle = self._spawn(source, sink, env)
        self._started_at = self._last_growth = self._clock()
        self._newest = None
        self._arrivals = {}
        self._lag_from = 0.0
        self._log.info("recorder_started", run=self._run, platform=self.platform)
        return self._run

    def _progress(self) -> tuple[int, tuple[str, int] | None]:
        """Bytes of this run on disk, and its newest segment as (name, size). Growth is judged by the
        newest segment alone: the total shrinks whenever the janitor deletes an old segment."""
        segments = [segment for segment in buffer.scan(self.directory) if segment.run == self._run]
        newest = (segments[-1].name, segments[-1].size) if segments else None
        if self.max_lag_s > 0:
            self._note_arrivals(segments[:-1])
        return sum(segment.size for segment in segments), newest

    def _note_arrivals(self, closed: Sequence[buffer.Segment]) -> None:
        """Read the first timestamp of every newly closed segment, once (for `delivery_lag`). Only the
        newest `ARRIVALS_KEEP` closed segments count, and none from before the last ad break (the
        comparison starts again after a break)."""
        recent = [segment for segment in closed[-ARRIVALS_KEEP:] if segment.start >= self._lag_from]
        names = {segment.name for segment in recent}
        self._arrivals = {name: value for name, value in self._arrivals.items() if name in names}
        for segment in recent:
            if segment.name in self._arrivals or segment.size == 0:
                continue
            stamp = self._probe(segment.path)
            # A segment that cannot be probed is remembered as such, so it is not probed every second.
            self._arrivals[segment.name] = (segment.start, stamp if stamp is not None else float("nan"))

    def lag_s(self) -> float:
        """Seconds the stream is behind real time in this run (0 when it is not watched or unknown)."""
        known = [value for _, value in sorted(self._arrivals.items()) if value[1] == value[1]]
        return delivery_lag(known) if len(known) >= 2 else 0.0

    def poll(self) -> RecorderStatus:
        """Look at the current run. A run that ended, never started or stalled is stopped and its
        failure returned once; after that `running` is False until `start()` is called again."""
        handle = self._handle
        if handle is None:
            return RecorderStatus(running=False, has_data=False)
        now = self._clock()
        size, newest = self._progress()
        if newest is not None and newest != self._newest:
            self._newest, self._last_growth = newest, now
        has_data = self._newest is not None and (size > 0 or self._newest[1] > 0)
        if handle.alive() and handle.filtering() and now - self._ad_since(now) <= self.ad_break_max_s:
            # An ad break: no data is expected. The start and stall timers wait for its end, and the
            # lag watch starts again with the segments after it.
            self._last_growth = now
            self._lag_from = time.time()
            if not has_data:
                self._started_at = now
            return RecorderStatus(running=True, has_data=has_data, bytes_written=size)
        self._ad_started = None
        failure: Failure | None = None
        if not handle.alive():
            handle.stop(KILL_WAIT_S)
            failure = classify_failure(scrub(handle.output(), self._proxy), had_data=has_data)
        elif not has_data and now - self._started_at > self.start_timeout_s:
            handle.stop(KILL_WAIT_S)
            classified = classify_failure(scrub(handle.output(), self._proxy), had_data=False)
            failure = (
                classified
                if classified.kind != "error"
                else Failure("start_timeout", f"no stream data within {self.start_timeout_s:g} s")
            )
        elif has_data and now - self._last_growth > self.stall_timeout_s:
            handle.stop(KILL_WAIT_S)
            failure = Failure("stalled", f"no new stream data for {self.stall_timeout_s:g} s")
        elif self.max_lag_s > 0 and self.lag_s() > self.max_lag_s:
            lag = self.lag_s()
            handle.stop(KILL_WAIT_S)
            failure = Failure(
                "throttled",
                f"the stream arrives slower than real time: {lag:.0f} s behind after "
                f"{now - self._started_at:.0f} s of recording",
            )
        if failure is None:
            return RecorderStatus(running=True, has_data=has_data, bytes_written=size)
        self._handle = None
        self._log.warning("recorder_run_ended", run=self._run, kind=failure.kind, detail=failure.detail)
        return RecorderStatus(running=False, has_data=has_data, failure=failure, bytes_written=size)

    def stop(self, timeout_s: float) -> None:
        """End the current run, if any, within about `timeout_s` seconds."""
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.stop(timeout_s)
            self._log.info("recorder_stopped", run=self._run)

    def kill(self) -> None:
        """Kill the processes of the current run without stopping the recorder: the next `poll` sees a
        dead run, as after a crash (used by the recovery check)."""
        if self._handle is not None:
            self._handle.kill()

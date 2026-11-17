"""Stream recorder: captures a live stream with streamlink into segment files on the data volume.

One recorder run is two processes joined by a pipe, without a shell:

    streamlink --stdout <url> best  |  ffmpeg -i pipe:0 -c copy -f segment ... seg_<run>_<UTC time>.ts

The video and audio are copied, never encoded, so a slot costs almost no CPU. Each segment file name
carries the run number and the wall-clock second (UTC) at which ffmpeg opened it; timestamps inside the
files continue across the segments of a run, so a run can be joined byte for byte (see `buffer.py`).

A run that ends is classified from what streamlink printed (`classify_failure`): `blocked` (the platform
refuses this machine: bot check, rate limit), `ended` / `not_live` (nothing is live at the URL any more),
`network`, or `error`. The session decides what to do with that; this module only reports it.

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

from sixstars.live import buffer
from sixstars.logging import get_logger
from sixstars.vod.ytdlp import BLOCKED_MARKERS, normalise

FailureKind = Literal["blocked", "ended", "not_live", "network", "error", "stalled", "start_timeout"]

STDERR_KEEP_LINES: Final = 40
DETAIL_CHARS: Final = 300
HTTP_TIMEOUT_S: Final = 20
STREAM_TIMEOUT_S: Final = 30
RETRY_OPEN: Final = 2
KILL_WAIT_S: Final = 3.0

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
    if any(marker in text for marker in LIVE_BLOCKED_MARKERS):
        return Failure("blocked", detail or "the platform refused this machine")
    if any(marker in text for marker in ENDED_MARKERS):
        return Failure("ended", detail or "the stream ended")
    if any(marker in text for marker in NOT_LIVE_MARKERS):
        return Failure("not_live", detail or "nothing is live at this URL")
    if any(marker in text for marker in NETWORK_MARKERS):
        return Failure("network", detail or "network error")
    if had_data:
        return Failure("ended", detail or "the stream ended")
    return Failure("error", detail or "the recorder stopped without output")


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
        f">{max_height}p",
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


def scrub(text: str, proxy: str | None) -> str:
    """Remove the proxy URL (it embeds credentials) from text that will be logged or stored."""
    return text.replace(proxy, "<proxy>") if proxy else text


class _Tail:
    """Collects the last lines a process writes to a pipe, in a daemon thread."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._lines: deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: IO[bytes]) -> None:
        with contextlib.suppress(Exception):
            for raw in iter(stream.readline, b""):
                self._lines.append(raw.decode("utf-8", errors="replace").rstrip())
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
        self._proxy = proxy
        self._cookies_file = cookies_file
        self._ffmpeg = ffmpeg
        self._spawn: Spawn = spawn or RunHandle
        self._clock = clock
        self._handle: RunHandle | None = None
        self._run = 0
        self._started_at = 0.0
        self._last_growth = 0.0
        self._last_size = 0
        self._log = get_logger("live")

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
        self._last_size = 0
        self._log.info("recorder_started", run=self._run, platform=self.platform)
        return self._run

    def _run_bytes(self) -> int:
        return sum(segment.size for segment in buffer.scan(self.directory) if segment.run == self._run)

    def poll(self) -> RecorderStatus:
        """Look at the current run. A run that ended, never started or stalled is stopped and its
        failure returned once; after that `running` is False until `start()` is called again."""
        handle = self._handle
        if handle is None:
            return RecorderStatus(running=False, has_data=False)
        now = self._clock()
        size = self._run_bytes()
        if size > self._last_size:
            self._last_size, self._last_growth = size, now
        has_data = size > 0
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

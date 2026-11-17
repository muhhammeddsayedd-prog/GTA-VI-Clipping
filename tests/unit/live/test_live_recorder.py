"""Recorder: the two commands, the classification of a run that ended, and the watch over a run. The
processes are replaced by a fake handle; nothing is recorded."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sixstars.live import buffer
from sixstars.live.recorder import (
    Recorder,
    RunHandle,
    ad_state,
    classify_failure,
    ffmpeg_command,
    scrub,
    stream_url,
    streamlink_command,
)

T0 = datetime(2026, 11, 17, 9, 0, 0, tzinfo=UTC)


# ── commands ─────────────────────────────────────────────────


def test_twitch_is_recorded_from_the_channel_page_without_any_key() -> None:
    assert stream_url("twitch", "kiwo", "stream-1", "ignored") == "https://www.twitch.tv/kiwo"
    command = streamlink_command("https://www.twitch.tv/kiwo", "twitch", max_height=1080, python="py")
    assert command[:3] == ["py", "-m", "streamlink"] and command[-2:] == [
        "https://www.twitch.tv/kiwo",
        "best",
    ]
    assert "--twitch-disable-ads" in command and "--stdout" in command
    assert "--http-proxy" not in command and "--http-cookies-file" not in command


def test_youtube_is_recorded_from_the_watch_page_and_only_as_a_live_stream() -> None:
    assert stream_url("youtube", "UCx", "abc123", "") == "https://www.youtube.com/watch?v=abc123"
    assert stream_url("youtube", "UCx", "abc123", "https://youtu.be/abc123") == "https://youtu.be/abc123"
    command = streamlink_command("https://youtu.be/abc123", "youtube", max_height=720)
    # Without this streamlink would play the finished video of an ended stream from its start.
    assert command[command.index("--stream-types") + 1] == "hls"
    # "720p60" sorts above "720p" in streamlink, so the limit names the 60 fps variant.
    assert command[command.index("--stream-sorting-excludes") + 1] == ">720p60"
    assert "--twitch-disable-ads" not in command


def test_the_proxy_and_the_cookies_are_passed_only_when_given() -> None:
    cookies = Path("cookies.txt")
    youtube = streamlink_command(
        "u", "youtube", max_height=1080, proxy="http://u:p@proxy:1", cookies_file=cookies
    )
    assert youtube[youtube.index("--http-proxy") + 1] == "http://u:p@proxy:1"
    assert youtube[youtube.index("--http-cookies-file") + 1] == "cookies.txt"
    twitch = streamlink_command("u", "twitch", max_height=1080, cookies_file=cookies)
    assert "--http-cookies-file" not in twitch  # the cookies are YouTube's


def test_every_streamlink_run_has_network_timeouts() -> None:
    command = streamlink_command("u", "twitch", max_height=1080)
    assert "--http-timeout" in command and "--stream-timeout" in command


def test_ffmpeg_copies_the_stream_into_utc_named_segments(tmp_path: Path) -> None:
    command = ffmpeg_command(tmp_path, 4, 60)
    assert command[command.index("-c") + 1] == "copy"  # never an encode: a slot costs almost no CPU
    assert command[command.index("-segment_time") + 1] == "60"
    assert command[command.index("-strftime") + 1] == "1"
    assert "-reset_timestamps" not in command  # timestamps continue across the segments of a run
    assert buffer.parse_segment_name(T0.strftime(Path(command[-1]).name)) == (4, T0.timestamp())


def test_the_proxy_never_reaches_a_log_line() -> None:
    assert scrub("error: http://u:p@proxy:1 refused", "http://u:p@proxy:1") == "error: <proxy> refused"
    assert scrub("plain", None) == "plain"


# ── classification ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("output", "had_data", "kind"),
    [
        (
            "[plugins.youtube][error] Could not get video info - LOGIN_REQUIRED: Sign in to confirm "
            "you’re not a bot",
            False,
            "blocked",
        ),
        ("error: Unable to open URL: https://x (429 Client Error: Too Many Requests)", False, "blocked"),
        ("error: No playable streams found on this URL: twitch.tv/kiwo", False, "not_live"),
        ("[cli][info] Stream ended\n[cli][info] Closing currently open stream...", True, "ended"),
        ("error: Unable to open URL: https://x (HTTPSConnectionPool: Read timed out.)", False, "network"),
        ("[stream.hls][warning] Failed to reload playlist: Unable to open URL", True, "network"),
        ("", True, "ended"),
        ("", False, "error"),
        ("something unexpected", False, "error"),
    ],
)
def test_a_run_that_ended_is_classified_from_what_streamlink_printed(
    output: str, had_data: bool, kind: str
) -> None:
    assert classify_failure(output, had_data=had_data).kind == kind


def test_the_bot_check_wins_over_the_no_streams_line_that_follows_it() -> None:
    output = (
        "[plugins.youtube][error] Could not get video info - LOGIN_REQUIRED: Sign in to confirm you're "
        "not a bot\n"
        "error: No playable streams found on this URL: https://www.youtube.com/watch?v=x"
    )
    failure = classify_failure(output, had_data=False)
    assert failure.kind == "blocked" and "not a bot" in failure.detail  # the refusal is what is reported


# ── watching a run ───────────────────────────────────────────


class FakeHandle:
    """A recorder run without processes: the test decides when it dies and what it printed."""

    def __init__(self) -> None:
        self.running = True
        self.text = ""
        self.stopped = 0
        self.killed = False
        self.ads = False

    def alive(self) -> bool:
        return self.running

    def output(self) -> str:
        return self.text

    def filtering(self) -> bool:
        return self.ads

    def stop(self, timeout_s: float) -> None:
        self.stopped += 1
        self.running = False

    def kill(self) -> None:
        self.killed = True
        self.running = False


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(tmp_path: Path) -> tuple[Recorder, list[FakeHandle], Clock]:
    handles: list[FakeHandle] = []
    clock = Clock()

    def spawn(streamlink: Sequence[str], ffmpeg: Sequence[str], env: dict[str, str]) -> RunHandle:
        assert env["TZ"] == "UTC0"  # segment names must be UTC on every machine
        handles.append(FakeHandle())
        return handles[-1]  # type: ignore[return-value]

    recorder = Recorder(
        "https://www.twitch.tv/kiwo",
        "twitch",
        tmp_path / "session",
        segment_s=60,
        max_height=1080,
        start_timeout_s=30,
        stall_timeout_s=45,
        spawn=spawn,
        clock=clock,
    )
    return recorder, handles, clock


def write_segment(recorder: Recorder, second: int, size: int) -> None:
    name = buffer.segment_name(recorder.run, T0.replace(second=second))
    (recorder.directory / name).write_bytes(b"x" * size)


def test_a_run_is_healthy_while_it_writes_data(tmp_path: Path) -> None:
    recorder, _, clock = make(tmp_path)
    assert recorder.poll().running is False  # nothing started yet
    assert recorder.start() == 1
    assert recorder.poll() == recorder.poll().__class__(running=True, has_data=False)
    write_segment(recorder, 0, 100)
    clock.now += 10
    status = recorder.poll()
    assert status.running and status.has_data and status.bytes_written == 100 and status.failure is None


def test_a_start_that_delivers_nothing_times_out(tmp_path: Path) -> None:
    recorder, handles, clock = make(tmp_path)
    recorder.start()
    clock.now += 31
    status = recorder.poll()
    assert not status.running and status.failure is not None and status.failure.kind == "start_timeout"
    assert handles[0].stopped == 1
    assert recorder.poll().failure is None  # reported once


def test_a_start_refused_by_the_platform_is_reported_as_blocked(tmp_path: Path) -> None:
    recorder, handles, _ = make(tmp_path)
    recorder.start()
    handles[0].running = False
    handles[0].text = "error: Sign in to confirm you're not a bot"
    failure = recorder.poll().failure
    assert failure is not None and failure.kind == "blocked"


def test_a_run_that_stops_growing_is_restarted_as_stalled(tmp_path: Path) -> None:
    recorder, handles, clock = make(tmp_path)
    recorder.start()
    write_segment(recorder, 0, 100)
    clock.now += 5
    assert recorder.poll().running
    clock.now += 40
    write_segment(recorder, 0, 200)  # still growing
    assert recorder.poll().running
    clock.now += 46
    failure = recorder.poll().failure
    assert failure is not None and failure.kind == "stalled" and handles[0].stopped == 1


def test_a_run_that_dies_after_recording_means_the_stream_ended(tmp_path: Path) -> None:
    recorder, handles, _ = make(tmp_path)
    recorder.start()
    write_segment(recorder, 0, 100)
    handles[0].kill()
    status = recorder.poll()
    assert status.failure is not None and status.failure.kind == "ended" and status.has_data


def test_every_run_gets_its_own_number_so_runs_are_never_joined(tmp_path: Path) -> None:
    recorder, handles, _ = make(tmp_path)
    recorder.start()
    write_segment(recorder, 0, 100)
    with pytest.raises(RuntimeError):
        recorder.start()  # the first run is still alive
    handles[0].kill()
    recorder.poll()
    assert recorder.start() == 2
    assert recorder.poll().has_data is False  # the data of run 1 does not count for run 2
    recorder.stop(1)
    assert handles[1].stopped == 1 and recorder.poll().running is False


def test_the_janitor_deleting_old_segments_is_not_mistaken_for_a_stall(tmp_path: Path) -> None:
    # Found in the real check: once the buffer is full, every new segment is matched by a deleted old
    # one, so the bytes on disk stop growing although the recorder is healthy.
    recorder, _, clock = make(tmp_path)
    recorder.start()
    for second in range(0, 50, 10):
        write_segment(recorder, second, 1000)
        for old in sorted(recorder.directory.glob("seg_*.ts"))[:-2]:
            old.unlink()  # the janitor keeps two segments
        clock.now += 20
        status = recorder.poll()
        assert status.running and status.failure is None, second
    clock.now += 46  # and a real stall is still caught
    failure = recorder.poll().failure
    assert failure is not None and failure.kind == "stalled"


# ── Twitch ad breaks ─────────────────────────────────────────


def test_streamlink_says_when_it_skips_an_ad_break_and_when_the_stream_resumes() -> None:
    assert ad_state("[stream.hls][info] Filtering out segments and pausing stream output") is True
    assert ad_state("[plugins.twitch][info] Waiting for pre-roll ads to finish, be patient") is True
    assert ad_state("[stream.hls][info] Resuming stream output") is False
    assert ad_state("[plugins.twitch][info] Will skip ad segments") is None
    assert ad_state("[plugins.twitch][info] Detected advertisement break of 30 seconds") is None


def test_an_ad_break_is_waited_out_instead_of_restarting_the_recorder(tmp_path: Path) -> None:
    recorder, handles, clock = make(tmp_path)
    recorder.start()
    write_segment(recorder, 0, 100)
    assert recorder.poll().running
    handles[0].ads = True  # a three-minute mid-roll: streamlink writes nothing
    for _ in range(18):
        clock.now += 10
        assert recorder.poll().running
    handles[0].ads = False
    clock.now += 40  # the stream is back; the stall timer starts from the end of the break
    assert recorder.poll().running
    clock.now += 10
    failure = recorder.poll().failure
    assert failure is not None and failure.kind == "stalled"


def test_a_pre_roll_does_not_count_against_the_start_timeout(tmp_path: Path) -> None:
    recorder, handles, clock = make(tmp_path)
    recorder.start()
    handles[0].ads = True
    clock.now += 90  # longer than the start timeout of 30 s
    assert recorder.poll().running
    handles[0].ads = False
    clock.now += 31
    failure = recorder.poll().failure
    assert failure is not None and failure.kind == "start_timeout"


def test_an_ad_break_that_never_ends_is_treated_as_a_stall(tmp_path: Path) -> None:
    recorder, handles, clock = make(tmp_path)
    recorder.start()
    write_segment(recorder, 0, 100)
    recorder.poll()
    handles[0].ads = True
    clock.now += 10
    assert recorder.poll().running
    clock.now += 301  # live.ad_break_max_s
    assert recorder.poll().running is False

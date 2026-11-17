"""Rolling buffer: names, the wall-clock index, cut planning, the janitor and pins. No network."""

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sixstars.live import buffer
from sixstars.live.buffer import Segment, Span

T0 = datetime(2026, 11, 17, 9, 0, 0, tzinfo=UTC).timestamp()


def seg(run: int, offset: float, size: int = 1000, mtime: float | None = None) -> Segment:
    start = T0 + offset
    name = buffer.segment_name(run, datetime.fromtimestamp(start, UTC))
    return Segment(Path("buf") / name, run, start, size, mtime if mtime is not None else start + 60)


def touch(directory: Path, run: int, offset: float, size: int = 10) -> Path:
    path = directory / buffer.segment_name(run, datetime.fromtimestamp(T0 + offset, UTC))
    path.write_bytes(b"x" * size)
    stamp = T0 + offset + 60
    os.utime(path, (stamp, stamp))
    return path


# ── names ────────────────────────────────────────────────────


def test_a_segment_name_carries_the_run_and_the_utc_second() -> None:
    name = buffer.segment_name(3, datetime(2026, 11, 17, 9, 7, 41, tzinfo=UTC))
    assert name == "seg_0003_20261117T090741.ts"
    assert buffer.parse_segment_name(name) == (3, datetime(2026, 11, 17, 9, 7, 41, tzinfo=UTC).timestamp())


def test_the_ffmpeg_pattern_produces_names_the_parser_reads() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert buffer.parse_segment_name(moment.strftime(buffer.segment_pattern(12))) == (12, moment.timestamp())


@pytest.mark.parametrize("name", ["seg_1_20261117T090741.ts", "seg_0001_20261317T090741.ts", "x.ts", "a.pin"])
def test_other_files_are_not_segments(name: str) -> None:
    assert buffer.parse_segment_name(name) is None


def test_scan_lists_segments_oldest_first_and_next_run_counts_up(tmp_path: Path) -> None:
    assert buffer.scan(tmp_path / "missing") == [] and buffer.next_run(tmp_path) == 1
    touch(tmp_path, 2, 60)
    touch(tmp_path, 1, 0, size=7)
    (tmp_path / "note.txt").write_text("x")
    found = buffer.scan(tmp_path)
    assert [(item.run, item.start - T0, item.size) for item in found] == [(1, 0, 7), (2, 60, 10)]
    assert buffer.next_run(tmp_path) == 3


# ── index ────────────────────────────────────────────────────


def test_a_segment_ends_where_the_next_of_its_run_starts() -> None:
    result = buffer.spans([seg(1, 0), seg(1, 60), seg(1, 120, mtime=T0 + 150)])
    assert [(span.start - T0, span.end - T0) for span in result] == [(0, 60), (60, 120), (120, 150)]


def test_the_last_segment_of_a_run_ends_at_its_last_write_never_after_now() -> None:
    result = buffer.spans([seg(1, 0, mtime=T0 + 40), seg(2, 100, mtime=T0 + 400)], now=T0 + 130)
    assert [(span.segment.run, span.end - T0) for span in result] == [(1, 40), (2, 130)]


def test_timestamps_remove_the_burst_at_the_start_of_a_run() -> None:
    # The first segment was opened 5 s late on the wall clock (the first HLS chunks arrive at once).
    segments = [seg(1, 0), seg(1, 5), seg(1, 15), seg(1, 25)]
    stamps = {segments[0].name: 1.4, segments[1].name: 11.4, segments[2].name: 21.4, segments[3].name: 31.4}
    starts = buffer.anchored_starts(segments, stamps, around=T0 + 10, span_s=300)
    assert [round(starts[item.name] - T0, 1) for item in segments] == [-5.0, 5.0, 15.0, 25.0]


def test_a_jump_in_the_timestamps_is_not_taken_for_fast_delivery() -> None:
    segments = [seg(1, 0), seg(1, 10), seg(1, 20), seg(1, 30)]
    stamps = {segments[0].name: 100.0, segments[1].name: 110.0, segments[2].name: 120.0}
    stamps[segments[3].name] = 95_000.0  # the stream's clock jumped
    starts = buffer.anchored_starts(segments, stamps, around=T0, span_s=300)
    assert [round(starts[item.name] - T0, 1) for item in segments[:3]] == [0.0, 10.0, 20.0]
    assert segments[3].name not in starts  # falls back to its name time


def test_without_timestamps_nothing_is_refined() -> None:
    assert buffer.anchored_starts([seg(1, 0)], {}, around=T0, span_s=300) == {}


def test_only_segments_near_the_window_set_the_anchor() -> None:
    # An hour ago delivery was 8 s faster (the stream stalled since); the old value must not be used.
    segments = [seg(1, 0), seg(1, 3600), seg(1, 3610)]
    stamps = {segments[0].name: 8.0, segments[1].name: 3600.0, segments[2].name: 3610.0}
    starts = buffer.anchored_starts(segments, stamps, around=T0 + 3605, span_s=300)
    assert round(starts[segments[1].name] - T0, 1) == 3600.0


# ── cut planning ─────────────────────────────────────────────


def spans_of(*segments: Segment) -> list[Span]:
    return buffer.spans(list(segments))


def test_a_window_inside_one_segment_starts_the_file_at_that_segment() -> None:
    plan = buffer.plan_cut(spans_of(seg(1, 0), seg(1, 60), seg(1, 120)), T0 + 70, T0 + 100)
    assert plan is not None
    assert [item.start - T0 for item in plan.segments] == [60]
    assert (plan.file_start - T0, plan.start - T0, plan.end - T0) == (60, 70, 100)


def test_a_window_across_segments_takes_every_segment_it_touches() -> None:
    plan = buffer.plan_cut(spans_of(seg(1, 0), seg(1, 60), seg(1, 120), seg(1, 180)), T0 + 50, T0 + 125)
    assert plan is not None and [item.start - T0 for item in plan.segments] == [0, 60, 120]
    assert plan.file_start == T0


def test_a_window_that_ends_exactly_on_a_boundary_does_not_take_the_next_segment() -> None:
    plan = buffer.plan_cut(spans_of(seg(1, 0), seg(1, 60), seg(1, 120)), T0 + 10, T0 + 60)
    assert plan is not None and [item.start - T0 for item in plan.segments] == [0]


def test_a_window_is_clamped_to_what_was_recorded() -> None:
    plan = buffer.plan_cut(spans_of(seg(1, 60), seg(1, 120, mtime=T0 + 150)), T0 + 20, T0 + 500)
    assert plan is not None
    assert (plan.start - T0, plan.end - T0, plan.file_start - T0) == (60, 150, 60)


def test_a_window_never_joins_two_recorder_runs() -> None:
    # Run 1 stopped at 100, run 2 started at 130: the window 90..170 lies mostly in run 2.
    items = spans_of(seg(1, 0), seg(1, 60, mtime=T0 + 100), seg(2, 130, mtime=T0 + 200))
    plan = buffer.plan_cut(items, T0 + 90, T0 + 170)
    assert plan is not None and plan.run == 2
    assert (plan.start - T0, plan.end - T0) == (130, 170)


def test_nothing_recorded_in_the_window_gives_no_plan() -> None:
    assert buffer.plan_cut(spans_of(seg(1, 0, mtime=T0 + 50)), T0 + 100, T0 + 130) is None
    assert buffer.plan_cut([], T0, T0 + 30) is None
    assert buffer.plan_cut(spans_of(seg(1, 0)), T0 + 30, T0 + 30) is None


def test_cut_offsets_stay_inside_the_file() -> None:
    cut = buffer.Cut(Path("c.mp4"), T0 + 60, 75.0, T0 + 70, T0 + 135, ("a",), 1)
    assert cut.offset(T0 + 70) == 10 and cut.offset(T0) == 0 and cut.offset(T0 + 999) == 75.0


@pytest.mark.parametrize(
    ("end_in", "expected"),
    [(-30.0, 0.0), (-2.0, 2.0), (10.0, 14.0), (500.0, 70.0)],
)
def test_the_wait_before_a_cut_covers_the_settle_time_and_is_capped(end_in: float, expected: float) -> None:
    assert buffer.wait_seconds(T0 + end_in, T0, settle_s=4, max_wait_s=70) == expected


# ── janitor and pins ─────────────────────────────────────────


def test_segments_older_than_the_buffer_are_deleted_and_the_rest_kept() -> None:
    items = spans_of(seg(1, 0), seg(1, 60), seg(1, 120), seg(1, 180))
    doomed = buffer.expired(items, set(), now=T0 + 400, buffer_s=300, max_bytes=10**9)
    assert [item.start - T0 for item in doomed] == [0]  # ended at 60, more than 300 s before 400


def test_pinned_segments_and_the_newest_one_are_never_deleted() -> None:
    items = spans_of(seg(1, 0), seg(1, 60), seg(1, 120, mtime=T0 + 130))
    pinned = {items[0].segment.name}
    doomed = buffer.expired(items, pinned, now=T0 + 10_000, buffer_s=300, max_bytes=0)
    assert [item.start - T0 for item in doomed] == [60]


def test_the_byte_cap_deletes_the_oldest_segments_first() -> None:
    items = spans_of(seg(1, 0, size=400), seg(1, 60, size=400), seg(1, 120, size=400), seg(1, 180, size=400))
    doomed = buffer.expired(items, set(), now=T0 + 200, buffer_s=1800, max_bytes=900)
    assert [item.start - T0 for item in doomed] == [0, 60]


def test_sweep_deletes_files_and_respects_pins(tmp_path: Path) -> None:
    old, pinned_file = touch(tmp_path, 1, 0), touch(tmp_path, 1, 60)
    fresh = touch(tmp_path, 1, 120), touch(tmp_path, 1, 2000)
    buffer.pin(tmp_path, "spike-1", [pinned_file.name])
    now = T0 + 2100
    os.utime(tmp_path / "spike-1.pin", (now, now))
    # the segment at 120 runs until 2000, so it is still inside the buffer; only the first one goes
    assert buffer.sweep(tmp_path, now=now, buffer_s=300, max_bytes=10**9, pin_max_age_s=600) == 1
    assert not old.exists() and pinned_file.exists() and fresh[1].exists()
    buffer.unpin(tmp_path, "spike-1")
    assert buffer.sweep(tmp_path, now=now, buffer_s=300, max_bytes=10**9, pin_max_age_s=600) == 1
    assert not pinned_file.exists() and fresh[1].exists()


def test_a_stale_pin_is_dropped(tmp_path: Path) -> None:
    path = buffer.pin(tmp_path, "crashed", ["seg_0001_20261117T090000.ts"])
    os.utime(path, (T0, T0))
    assert buffer.pinned(tmp_path, now=T0 + 100, max_age_s=600) == {"seg_0001_20261117T090000.ts"}
    assert buffer.pinned(tmp_path, now=T0 + 1000, max_age_s=600) == set()
    assert not path.exists()


def test_old_cuts_are_deleted_and_an_empty_session_directory_removed(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    cut = buffer.cut_path(directory, "spike")
    cut.parent.mkdir(parents=True)
    cut.write_bytes(b"x")
    os.utime(cut, (T0, T0))
    touch(directory, 1, 0)
    assert buffer.remove_segments(directory) == 1
    assert buffer.remove_if_empty(directory) is False  # the cut is still there
    assert buffer.sweep_cuts(directory, now=T0 + 100, keep_s=600) == 0
    assert buffer.sweep_cuts(directory, now=T0 + 1000, keep_s=600) == 1
    assert buffer.remove_if_empty(directory) is True and not directory.exists()
    assert buffer.dir_bytes(directory) == 0


# ── a real cut ───────────────────────────────────────────────


@pytest.mark.ffmpeg
def test_a_window_cut_from_real_segments_has_the_requested_length_and_place(tmp_path: Path) -> None:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg is not installed")
    # 24 s of test video in 4 s segments, as one recorder run writes them (timestamps continue).
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=25",
    ]
    command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "24", "-c:v", "libx264"]
    command += ["-preset", "ultrafast", "-g", "25", "-c:a", "aac", "-f", "segment", "-segment_time", "4"]
    command += ["-segment_format", "mpegts", str(tmp_path / "part_%02d.ts")]
    subprocess.run(command, check=True, timeout=120)
    parts = sorted(tmp_path.glob("part_*.ts"))
    assert len(parts) == 6
    for number, part in enumerate(parts):
        stamp = T0 + number * 4
        target = tmp_path / buffer.segment_name(1, datetime.fromtimestamp(stamp, UTC))
        part.rename(target)
        os.utime(target, (stamp + 4, stamp + 4))
    out = buffer.cut_path(tmp_path, "spike")
    cut = buffer.extract(
        tmp_path, T0 + 6, T0 + 15, out, anchor_span_s=300, clock=lambda: T0 + 30, pin_key="spike"
    )
    assert cut.segments == tuple(
        buffer.segment_name(1, datetime.fromtimestamp(T0 + n, UTC)) for n in (4, 8, 12)
    )
    assert abs(cut.file_start - (T0 + 4)) < 0.2  # the file starts at the segment boundary
    assert abs(cut.duration_s - 11.0) < 0.3  # 4..15
    assert abs(cut.offset(T0 + 6) - 2.0) < 0.2 and abs(cut.end - (T0 + 15)) < 0.3
    assert out.is_file() and not (tmp_path / "spike.pin").exists()
    with pytest.raises(buffer.BufferCutError):
        buffer.extract(
            tmp_path, T0 + 500, T0 + 520, tmp_path / "none.mp4", anchor_span_s=300, clock=lambda: T0 + 600
        )

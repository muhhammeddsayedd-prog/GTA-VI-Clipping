"""Session rules that need no database: what happens after a recorder run ends, where a confirmed spike
goes, what is cut for it and how wall-clock times become positions in the cut file."""

from pathlib import Path

import pytest

from sixstars.live import buffer
from sixstars.live.config import LiveConfig
from sixstars.live.recorder import Failure
from sixstars.live.session import (
    Route,
    after_failure,
    cut_range,
    looks_blocked,
    render_input,
    route_for,
    spike_context,
    top_tokens,
)
from sixstars.live.store import SessionRow
from sixstars.spikes import DetectedSpike

CFG = LiveConfig.offline()
T = 1_794_900_000.0
EXPRESS = Route("express", "express.render", 100, "spike", "express")
NORMAL = Route("normal", "clip.render", 60, "hourly", "normal")


def spike(**changes: object) -> DetectedSpike:
    values: dict[str, object] = {
        "mode": "live",
        "peak_t": T + 100,
        "first_t": T + 95,
        "score_z": 6.2,
        "parts": {"msgs_z": 10.0},
        "msgs": 120,
        "uniq": 80,
        "reaction_count": 90,
        "clip_requests": 4,
        "tier": "S",
        "window_start": T + 75,
        "window_end": T + 120,
        "spike_floor": T + 95,
        "pad_start": T + 60,
        "pad_end": T + 135,
        "chat_sample": [
            {"t_rel": -3.0, "user": "a", "text": "wait what"},
            {"t_rel": 0.5, "user": "b", "text": "LMAO no way"},
            {"t_rel": 2.0, "user": "c", "text": "LMAO clip it"},
            {"user": "d", "text": "later"},
        ],
    }
    values.update(changes)
    return DetectedSpike(**values)  # type: ignore[arg-type]


def row(**changes: object) -> SessionRow:
    values: dict[str, object] = {
        "id": "11111111-1111-1111-1111-111111111111",
        "creator_id": "22222222-2222-2222-2222-222222222222",
        "platform": "twitch",
        "source_id": "stream-9",
        "channel_login": "kiwo",
        "url": "https://www.twitch.tv/kiwo",
        "status": "recording",
        "reconnects": 0,
        "assigned_at": None,
        "started_at": None,
        "bot_names": (),
        "vod_source_id": None,
        "handle": "kiwo",
        "channel_id": "777",
        "tier": "auto",
    }
    values.update(changes)
    return SessionRow(**values)  # type: ignore[arg-type]


# ── reconnect policy ─────────────────────────────────────────


@pytest.mark.parametrize("kind", ["ended", "not_live", "network", "error", "stalled", "start_timeout"])
def test_a_run_that_ended_is_retried_while_reconnects_are_left(kind: str) -> None:
    failure = Failure(kind, "detail")  # type: ignore[arg-type]
    for failures in (1, 2, 3):
        assert after_failure(failure, ever_recorded=True, failures=failures, max_reconnects=3) is None


def test_after_the_last_reconnect_a_recorded_session_ends_and_its_replay_is_scanned() -> None:
    outcome = after_failure(
        Failure("not_live", "No playable streams"), ever_recorded=True, failures=4, max_reconnects=3
    )
    assert outcome is not None and outcome.status == "ended" and not outcome.blocked
    assert "the stream ended" in outcome.reason
    broken = after_failure(Failure("network", "timed out"), ever_recorded=True, failures=4, max_reconnects=3)
    assert broken is not None and broken.status == "ended" and "could not be resumed" in broken.reason


def test_a_session_that_never_recorded_fails_instead_of_ending() -> None:
    outcome = after_failure(Failure("not_live", "offline"), ever_recorded=False, failures=4, max_reconnects=3)
    assert outcome is not None and outcome.status == "failed"


def test_a_platform_block_is_never_retried_by_the_session() -> None:
    blocked = Failure("blocked", "Sign in to confirm you're not a bot")
    first = after_failure(blocked, ever_recorded=False, failures=1, max_reconnects=3)
    assert first is not None and first.blocked and first.status == "released"  # the slot is given back
    later = after_failure(blocked, ever_recorded=True, failures=1, max_reconnects=3)
    assert later is not None and later.blocked and later.status == "ended"  # what was recorded is kept


def test_with_no_reconnects_allowed_the_first_failure_closes_the_session() -> None:
    outcome = after_failure(Failure("stalled", "x"), ever_recorded=True, failures=1, max_reconnects=0)
    assert outcome is not None and outcome.status == "ended"


def test_the_bot_check_is_recognised_in_a_chat_error() -> None:
    assert looks_blocked("Sign in to confirm you’re not a bot")
    assert not looks_blocked("the video has no live chat")


# ── routing ──────────────────────────────────────────────────


def test_s_tier_goes_to_the_express_queue_and_the_spike_lane() -> None:
    assert route_for("S", CFG, emergency_stop=False) == EXPRESS


def test_a_and_b_go_to_the_normal_queue_and_the_hourly_lane() -> None:
    assert route_for("A", CFG, emergency_stop=False) == NORMAL
    assert route_for("B", CFG, emergency_stop=False) == Route("normal", "clip.render", 40, "hourly", "normal")


def test_under_the_emergency_stop_an_s_spike_does_not_take_the_express_path() -> None:
    assert route_for("S", CFG, emergency_stop=True) == NORMAL


def test_a_and_b_can_be_left_to_the_replay_pass() -> None:
    quiet = LiveConfig.offline(ab_clips=False)
    assert route_for("A", quiet, emergency_stop=False) is None
    assert route_for("S", quiet, emergency_stop=True) is None  # nothing to post now, nothing queued
    assert route_for("S", quiet, emergency_stop=False) == EXPRESS
    assert route_for("rejected", CFG, emergency_stop=False) is None


# ── the cut and the factory input ────────────────────────────


def test_an_express_cut_waits_for_only_a_little_context_after_the_window() -> None:
    assert cut_range(spike(), EXPRESS, express_pad_wait_s=6) == (T + 60, T + 126)
    assert cut_range(spike(), EXPRESS, express_pad_wait_s=60) == (T + 60, T + 135)  # never past the pad
    assert cut_range(spike(), NORMAL, express_pad_wait_s=6) == (T + 60, T + 135)


def cut_at(file_start: float, duration: float) -> buffer.Cut:
    return buffer.Cut(Path("cut.mp4"), file_start, duration, file_start, file_start + duration, ("a",), 1)


def test_wall_clock_times_become_seconds_in_the_cut_file() -> None:
    # The file starts at a segment boundary 5 s before the padded window.
    item = render_input(
        spike(),
        "spike-1",
        cut_at(T + 55, 71.0),
        row(),
        EXPRESS,
        None,
        stream_start=T - 1000,
        chat_offset_s=0.0,
    )
    assert (item.window_start_s, item.window_end_s) == (20.0, 65.0)
    assert item.spike_floor_s == 40.0 and (item.pad_start_s, item.pad_end_s) == (5.0, 71.0)
    assert item.source_offset_s == 1055.0  # where the file starts in the stream
    assert (item.lane, item.mode, item.source_type, item.platform) == ("spike", "express", "live", "twitch")
    assert (item.creator, item.source_id, item.spike_id) == ("kiwo", "stream-9", "spike-1")
    # chat lines: peak at 45 s in the file; the entry without a time is left out
    assert [(line.t_s, line.text) for line in item.chat] == [
        (42.0, "wait what"),
        (45.5, "LMAO no way"),
        (47.0, "LMAO clip it"),
    ]


def test_the_chat_offset_moves_what_is_on_the_chat_clock() -> None:
    # The window start already carries the offset (the detector applied it); the floor and the chat
    # lines are chat-clock times and are moved here.
    item = render_input(
        spike(), "s", cut_at(T + 55, 71.0), row(), EXPRESS, None, stream_start=T, chat_offset_s=4.0
    )
    assert item.spike_floor_s == 36.0 and item.chat[1].t_s == 41.5


def test_a_cut_that_misses_the_window_is_refused() -> None:
    with pytest.raises(buffer.BufferCutError):
        render_input(
            spike(), "s", cut_at(T + 500, 30.0), row(), EXPRESS, None, stream_start=T, chat_offset_s=0
        )


def test_a_cut_clamped_by_a_recorder_restart_keeps_the_floor_inside_the_window() -> None:
    # The run started at T+100: only the last 20 s of the window were recorded.
    item = render_input(
        spike(), "s", cut_at(T + 100, 26.0), row(), EXPRESS, None, stream_start=T, chat_offset_s=0
    )
    assert (item.window_start_s, item.window_end_s, item.spike_floor_s) == (0.0, 20.0, 0.0)


def test_the_payload_survives_the_job_queue() -> None:
    from sixstars.factory.pipeline import RenderInput

    item = render_input(
        spike(), "s", cut_at(T + 55, 71.0), row(), EXPRESS, None, stream_start=T, chat_offset_s=0
    )
    again = RenderInput.from_payload(
        {**item.to_payload(), "live": {"session_id": "x"}}, default_mode="express"
    )
    assert again == item


# ── the confirmation prompt ──────────────────────────────────


def test_live_confirmation_sees_the_chat_sample_and_no_transcript() -> None:
    context = spike_context("spike-1", spike(), "kiwo", "twitch")
    assert context.transcript is None and context.spike_id == "spike-1"
    assert [(line.t_s, line.text) for line in context.chat] == [
        (-3.0, "wait what"),
        (0.5, "LMAO no way"),
        (2.0, "LMAO clip it"),
    ]
    assert (context.msgs, context.uniq, context.score_z) == (120, 80, 6.2)
    assert context.top_tokens[0] == "LMAO"


def test_top_tokens_count_each_word_once_per_message() -> None:
    sample = [{"text": "W W W"}, {"text": "W clip"}, {"text": "clip"}, {"text": "nice"}]
    assert top_tokens(sample, 2) == ["W", "CLIP"]

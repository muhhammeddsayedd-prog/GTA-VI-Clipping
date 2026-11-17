"""The pure parts of the `viewer_clip.process` job: what yt-dlp is asked for, which failures mean "the
clip is gone", and the factory input (credit, window, and where the clip row is stored)."""

from pathlib import Path

import pytest

from sixstars.ai.schemas import ClipWriting
from sixstars.factory import pipeline
from sixstars.factory.pipeline import RenderInput
from sixstars.factory.window import ClipWindow
from sixstars.viewer_clips import process
from sixstars.viewer_clips.store import ViewerClip
from sixstars.vod import ytdlp
from sixstars.vod.ytdlp import Failure, YtDlpError

GONE = "ERROR: [twitch:clips] Slug: This clip is no longer available"


def row(**changes: object) -> ViewerClip:
    values: dict[str, object] = {
        "id": "11111111-1111-1111-1111-111111111111",
        "source_id": "FakeClipSlug0",
        "creator_id": "22222222-2222-2222-2222-222222222222",
        "url": "https://www.twitch.tv/streamer0/clip/FakeClipSlug0",
        "title": "he did not have a parachute",
        "language": "en",
        "view_count": 1800,
        "duration_s": 45.0,
        "clip_created_at": None,
        "vod_id": "2372168353",
        "vod_offset_s": 8506.0,
        "status": "new",
        "skip_reason": None,
        "attempts": 0,
        "creator_handle": "streamer0",
        "creator_tier": "auto",
        **changes,
    }
    return ViewerClip(**values)  # type: ignore[arg-type]


def test_download_arguments_take_the_landscape_rendition_and_no_proxy_or_cookies() -> None:
    arguments = process.download_arguments(row().url, Path("/data/viewer_clips/x"), 1080)
    selector = arguments[arguments.index("-f") + 1]
    assert selector == "b[height<=1080][format_id!^=portrait]/b[format_id!^=portrait]/b"
    assert arguments[-1] == row().url and "--no-part" in arguments
    assert Path(arguments[arguments.index("--output") + 1]).name == "source.%(ext)s"
    command = [*ytdlp.base_command(ytdlp.Access()), *arguments]
    assert "--proxy" not in command and "--cookies" not in command
    assert "--download-sections" not in command  # the whole clip, not a window


def test_a_deleted_clip_is_recognised_and_other_failures_are_not() -> None:
    # the message yt-dlp gives for a deleted clip is not one of the VOD lane's markers
    failure = ytdlp.classify_failure(1, GONE)
    assert failure.kind == "error" and process.is_gone(ytdlp.error_for(failure))
    assert process.is_gone(ytdlp.error_for(Failure("unavailable", "ERROR: Video unavailable")))
    for kind in ("blocked", "forbidden", "timeout", "error"):
        assert not process.is_gone(YtDlpError(Failure(kind, "HTTP Error 503")))  # type: ignore[arg-type]


def test_the_render_input_credits_the_broadcaster_and_sits_in_the_broadcast() -> None:
    item = process.render_input(row(), Path("/d/source.mp4"), 45.0, max_len_s=60.0)
    assert item.creator == "streamer0" and item.creator_id == row().creator_id
    assert (item.platform, item.source_type, item.lane, item.mode) == ("twitch", "vod", "hourly", "normal")
    # stored under the video of the broadcast, on its clock: what the duplicate check of the gate compares
    assert (item.source_id, item.source_offset_s) == ("2372168353", 8506.0)
    assert item.source_url == row().url and item.stream_title == "he did not have a parachute"
    assert (item.window_start_s, item.window_end_s) == (0.0, 45.0)
    assert (item.pad_start_s, item.pad_end_s) == (0.0, 45.0)
    # the row id is the clip id: a retried job finds the same clip row
    assert item.clip_id == row().id and item.spike_id is None
    assert item.chat == () and item.verdict is None and item.upload is False
    assert item.moment_chosen is False  # only with twitch_clips.render_without_moment
    chosen = process.render_input(row(), Path("/d/source.mp4"), 45.0, max_len_s=60.0, moment_chosen=True)
    assert chosen.moment_chosen and RenderInput.from_payload(chosen.to_payload()) == chosen
    assert RenderInput.from_payload({**item.to_payload(), "moment_chosen": "yes"}).moment_chosen is False
    assert item.to_payload()["creator"] == "streamer0"


def test_a_clip_longer_than_the_gate_allows_keeps_its_last_seconds() -> None:
    item = process.render_input(row(), Path("/d/source.mp4"), 75.0, max_len_s=60.0)
    assert (item.window_start_s, item.window_end_s) == (15.0, 75.0)  # the moment is at the end
    assert (item.pad_start_s, item.pad_end_s) == (0.0, 75.0)  # the clip writer still reads all of it


def test_the_writer_does_not_trim_a_viewer_clip() -> None:
    item = process.render_input(row(), Path("/d/source.mp4"), 60.0, max_len_s=60.0)
    assert item.whole_window and RenderInput.from_payload(item.to_payload()).whole_window
    assert RenderInput.from_payload({**item.to_payload(), "whole_window": None}).whole_window is False
    # the writer wanted the first 20 seconds of a 60-second clip: its in and out points are dropped
    cut = writing(reject=False, reject_reason=None, start_s=0.0, end_s=20.0)
    kept = pipeline.keep_window(cut, ClipWindow(0.0, 60.0))
    assert (kept.start_s, kept.end_s) == (0.0, 60.0)
    assert (kept.hook, kept.title, kept.score) == (cut.hook, cut.title, cut.score)


@pytest.mark.parametrize("changes", [{"vod_id": None, "vod_offset_s": None}, {"vod_offset_s": None}])
def test_without_a_place_in_the_broadcast_the_clip_is_stored_under_its_own_id(
    changes: dict[str, object],
) -> None:
    item = process.render_input(row(**changes), Path("/d/source.mp4"), 45.0, max_len_s=60.0)
    assert (item.source_id, item.source_offset_s) == ("FakeClipSlug0", 0.0)


def test_requeue_key_flips() -> None:
    assert process.requeue_key("viewer_clip:1") == "viewer_clip:1:again"
    assert process.requeue_key("viewer_clip:1:again") == "viewer_clip:1"
    assert process.requeue_key(None) is None


def writing(**changes: object) -> ClipWriting:
    values: dict[str, object] = {
        "start_s": 0.0,
        "end_s": 30.0,
        "hook": "The blimp armada is here",
        "title": "The last blimp strat",
        "caption": "He lined up every blimp",
        "hashtags": ["gta", "gta5", "speedrun"],
        "commentary": None,
        "topic": "blimp-armada",
        "keywords": [],
        "spoiler": False,
        "safety": {},
        "score_parts": {"hook": 4, "clarity": 3, "payoff": 3, "rewatch": 3},
        "score": 3,
        "reject": True,
        "reject_reason": "No clear payoff; mostly sparse commentary",
        **changes,
    }
    return ClipWriting.model_validate(values)


def test_a_no_moment_reject_stops_every_render_except_a_viewer_chosen_one() -> None:
    no_moment = writing()
    assert pipeline.reject_stands(no_moment, moment_chosen=False)  # every other lane: as before
    assert not pipeline.reject_stands(no_moment, moment_chosen=True)  # rendered; the gate judges it
    assert not pipeline.reject_stands(writing(reject=False, reject_reason=None), moment_chosen=False)
    assert not pipeline.reject_stands(writing(reject=False, reject_reason=None), moment_chosen=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"safety": {"sexual": True}},
        {"safety": {"gambling": True}},
        {"safety": {"copyrighted_media": True}, "reject_reason": "safety: copyrighted_media"},
        {"spoiler": True, "reject_reason": "story spoiler before the block date"},
    ],
)
def test_a_reject_for_safety_or_a_spoiler_always_stands(changes: dict[str, object]) -> None:
    assert pipeline.reject_stands(writing(**changes), moment_chosen=True)
    assert pipeline.reject_stands(writing(**changes), moment_chosen=False)

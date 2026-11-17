"""Pure logic of the VOD lane: settings, spike ranking, the confirmation input, the stored verdict, the
re-queue key (budget and block deferrals), the block notification, the fallback maths, clean-up rules and
URL parsing. No network, no database."""

from datetime import UTC, datetime
from typing import Any

import pytest

from sixstars.ai.calls import TranscriptChunk
from sixstars.ai.schemas import Moment
from sixstars.factory.pipeline import next_budget_reset
from sixstars.spikes.detector import DetectedSpike
from sixstars.vod import cleanup, commands, fallback, process
from sixstars.vod.captions import Track, Word
from sixstars.vod.config import DEFAULTS, VodConfig, VodConfigError
from sixstars.vod.store import SpikeRow, Video
from sixstars.vod.ytdlp import Access

BASE: dict[str, Any] = {
    "vod.max_spikes_per_video": 25,
    "vod.fallback_moments": [3, 8],
    "vod.whisper_model": "medium",
    "downloads.max_height": 1080,
    "spikes.end_guard_s": 300,
    "spikes.min_len_s": 15,
    "spikes.max_len_s": 60,
    "spikes.context_pad_s": 15,
}


def cfg(**overrides: Any) -> VodConfig:
    return VodConfig.from_mapping({**BASE, **overrides})


def video(**overrides: Any) -> Video:
    values: dict[str, Any] = {
        "id": "v-1",
        "creator_id": "c-1",
        "platform": "youtube",
        "source_id": "abcdefghijk",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "title": "GTA RP night",
        "duration_s": 18000.0,
        "has_chat": True,
        "live_session_id": None,
        "status": "new",
        "skip_reason": None,
        "attempts": 0,
        "creator_handle": "speedy",
        "creator_tier": "auto",
        "session_started_at": None,
        "clipped_ranges": (),
    }
    return Video(**{**values, **overrides})


def spike_row(**overrides: Any) -> SpikeRow:
    values: dict[str, Any] = {
        "id": "s-1",
        "mode": "replay",
        "status": "detected",
        "tier": "A",
        "score_z": 3.2,
        "offset_s": 4500.0,
        "window_start_s": 4480.0,
        "window_end_s": 4530.0,
        "msgs": 31,
        "uniq": 27,
        "parts": {"pad_start": 4465.0, "pad_end": 4545.0, "spike_floor": 4500.0},
        "chat_sample": (
            {"t_rel": -4.0, "user": "@a", "text": "what is he doing"},
            {"t_rel": 0.5, "user": "@b", "text": "LMAOOO no way"},
            {"t_rel": 1.0, "user": "@c", "text": "LMAO LMAO"},
            {"t_rel": 2.5, "user": "@d", "text": "no way"},
        ),
        "confidence": None,
        "reason": None,
    }
    return SpikeRow(**{**values, **overrides})


def detected(score: float, msgs: int, peak: float) -> DetectedSpike:
    return DetectedSpike(
        mode="replay",
        peak_t=peak,
        first_t=peak,
        score_z=score,
        parts={},
        msgs=msgs,
        uniq=msgs,
        reaction_count=0,
        clip_requests=0,
        tier="B",
        window_start=peak - 20,
        window_end=peak + 20,
        spike_floor=peak,
        pad_start=peak - 35,
        pad_end=peak + 35,
        chat_sample=[],
    )


# ── settings ─────────────────────────────────────────────────


def test_config_defaults_match_the_migration() -> None:
    import json
    import re
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[3] / "supabase/migrations/0008_vod.sql").read_text("utf-8")
    seeded = {
        key: json.loads(raw.replace("''", "'"))
        for key, raw in re.findall(r"^\s*\('([\w.]+)', '(.*?)'::jsonb, '", sql, re.MULTILINE)
    }
    # Migration 0010 raised `vod.fallback_below_spikes` from the 1 seeded here to 3 (hardening-sources).
    later = (Path(__file__).resolve().parents[3] / "supabase/migrations/0010_sources.sql").read_text("utf-8")
    assert "where key = 'vod.fallback_below_spikes' and value = '1'::jsonb" in later
    assert seeded.pop("vod.fallback_below_spikes") == 1
    assert {**seeded, "vod.fallback_below_spikes": 3} == DEFAULTS


def test_config_reads_every_value() -> None:
    config = cfg(**{"vod.render_priority": {"S": 9, "A": 8, "B": 1}, "vod.download_workers": 0})
    assert config.max_spikes_per_video == 25 and config.fallback_moments == (3, 8)
    assert config.max_height == 1080 and config.end_guard_s == 300
    assert (config.priority_for("S"), config.priority_for("A"), config.priority_for("B")) == (9, 8, 1)
    assert config.priority_for("rejected") == 0
    assert config.download_workers == 1  # never fewer than one
    assert cfg().priority_for("S") == 55 and cfg().priority_for("B") == 40
    assert cfg().fallback_whisper_model is None and cfg().section_exact_cuts is False


@pytest.mark.parametrize(
    "bad",
    [
        {"vod.fallback_moments": [3]},
        {"vod.render_priority": 55},
        {"vod.fallback_transcribe_mode": "cloud"},
        {"vod.download_timeout_s": "900"},
        {"vod.fallback_enabled": 1},
        {"vod.caption_langs": "en"},
    ],
)
def test_config_rejects_wrong_types(bad: dict[str, Any]) -> None:
    with pytest.raises(VodConfigError):
        cfg(**bad)


# ── spikes and the confirmation input ────────────────────────


def test_best_first_ranks_by_score_then_messages_and_caps() -> None:
    spikes = [detected(2.1, 10, 100), detected(3.5, 12, 200), detected(3.5, 40, 300), detected(2.9, 9, 400)]
    assert [spike.peak_t for spike in process.best_first(spikes, 3)] == [300, 200, 400]
    assert process.best_first(spikes, 0) == []


def test_top_tokens_count_each_word_once_per_message_after_the_peak() -> None:
    assert set(process.top_tokens(spike_row(), 3)) == {"LMAO", "NO", "WAY"}
    assert "WHAT" not in process.top_tokens(spike_row())  # said before the peak


def test_spike_context_carries_chat_and_the_words_of_the_padded_window() -> None:
    track = Track(
        "en",
        (
            Word("before", 4400.0, 4400.5),
            Word("he", 4466.0, 4466.3),
            Word("crashed", 4530.0, 4530.6),
            Word("after", 4545.0, 4545.4),
        ),
    )
    context = process.spike_context(video(), spike_row(), track)
    assert context.spike_id == "s-1" and context.creator == "speedy" and context.platform == "youtube"
    assert context.stream_title == "GTA RP night" and context.msgs == 31 and context.uniq == 27
    assert [line.t_s for line in context.chat] == [-4.0, 0.5, 1.0, 2.5]
    assert context.transcript == "he crashed"
    assert process.spike_context(video(), spike_row(), None).transcript is None


def test_verdict_is_rebuilt_from_the_stored_reason() -> None:
    row = spike_row(status="confirmed", confidence=0.82, reason="fail: Streamer drives into the canal")
    verdict = process.verdict_from_row(row)
    assert verdict is not None and verdict.is_moment and verdict.confidence == 0.82
    assert verdict.moment_type == "fail" and verdict.one_line == "Streamer drives into the canal"
    odd = process.verdict_from_row(spike_row(confidence=0.7, reason="something without a type"))
    assert odd is not None and odd.moment_type == "other" and odd.one_line == "something without a type"
    assert process.verdict_from_row(spike_row()) is None


# ── deferrals ────────────────────────────────────────────────


def test_requeue_key_differs_from_the_running_job_and_flips_back() -> None:
    assert process.requeue_key("vod:123") == "vod:123:again"
    assert process.requeue_key("vod:123:again") == "vod:123"
    assert process.requeue_key(None) is None


def test_a_budget_deferral_waits_for_the_next_utc_day() -> None:
    at = next_budget_reset(datetime(2026, 11, 17, 15, 30, tzinfo=UTC))
    assert at.date().isoformat() == "2026-11-18" and at.hour == 0 and 0 < at.minute < 10


def test_block_message_says_what_happens_and_what_fixes_it() -> None:
    text = process.blocked_message(
        "youtube", 7, 1800, Access(), "ERROR: [youtube] x: Sign in to confirm you're not a bot"
    )
    assert "Sign in to confirm you're not a bot" in text
    assert "7 video(s) are waiting" in text and "every 30 min" in text and "not failed" in text
    assert "PROXY_URL" in text and "YOUTUBE_COOKIES_FILE" in text
    assert "PROXY_URL not set, YOUTUBE_COOKIES_FILE not set" in text


# ── transcript fallback ──────────────────────────────────────


@pytest.mark.parametrize(
    ("duration_s", "limit_s", "expected"),
    [
        (3000.0, 5400.0, (0.0, 3000.0)),  # fits: the whole video
        (18000.0, 5400.0, (900.0, 6300.0)),  # long: skip the warm-up
        (5800.0, 5400.0, (400.0, 5800.0)),  # not enough left after the warm-up: the last part
        (None, 5400.0, (0.0, 5400.0)),
        (18000.0, 0.0, (0.0, 0.0)),
    ],
)
def test_transcribe_range(duration_s: float | None, limit_s: float, expected: tuple[float, float]) -> None:
    assert fallback.transcribe_range(duration_s, limit_s, 900.0) == expected


def test_the_picker_reads_a_capped_part_of_a_long_transcript() -> None:
    chunks = [TranscriptChunk(float(start), float(start) + 30, "words") for start in range(0, 18000, 30)]
    assert fallback.limit_chunks(chunks, 0, 900.0) == chunks  # no cap
    assert fallback.limit_chunks(chunks[:100], 21600, 900.0) == chunks[:100]  # short enough
    capped = fallback.limit_chunks(chunks, 3600, 900.0)
    assert (capped[0].start_s, capped[-1].end_s, len(capped)) == (900.0, 4500.0, 120)
    assert fallback.limit_chunks([], 3600, 900.0) == []


def moment(confidence: float, start: float = 600.0, end: float = 640.0) -> Moment:
    return Moment(
        start_s=start, end_s=end, moment_type="funny", one_line="He falls off", confidence=confidence
    )


def test_a_moment_becomes_a_padded_spike() -> None:
    spike = fallback.moment_spike(moment(0.7), cfg(), a_min_confidence=0.8, video_duration_s=3600.0)
    assert (spike.status, spike.tier) == ("confirmed", "B")
    assert (spike.pad_start_s, spike.pad_end_s) == (585.0, 655.0)
    assert spike.spike_floor_s == 620.0 and spike.reason == "funny: He falls off"


def test_moment_confidence_decides_status_and_tier() -> None:
    config = cfg(**{"vod.fallback_min_confidence": 0.5})
    assert fallback.moment_spike(
        moment(0.49), config, a_min_confidence=0.8, video_duration_s=None
    ).status == ("rejected")
    strong = fallback.moment_spike(moment(0.85), config, a_min_confidence=0.8, video_duration_s=None)
    assert (strong.status, strong.tier) == ("confirmed", "A")


def test_moment_padding_stays_inside_the_video() -> None:
    early = fallback.moment_spike(moment(0.7, 5.0, 40.0), cfg(), a_min_confidence=0.8, video_duration_s=50.0)
    assert (early.pad_start_s, early.pad_end_s) == (0.0, 50.0)


# ── clean-up ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "age_s", "expected"),
    [
        ("clipped", 10.0, True),
        ("rejected", 10.0, True),
        ("failed", 10.0, True),
        (None, 10.0, True),  # no such spike any more
        ("confirmed", 10.0, False),  # render queued, not started
        ("rendering", 10.0, False),
        ("rendering", 100000.0, True),  # a render that never ended
    ],
)
def test_which_window_files_may_be_deleted(status: str | None, age_s: float, expected: bool) -> None:
    assert cleanup.removable(status, age_s, 93600.0) is expected


# ── CLI ──────────────────────────────────────────────────────


def test_parse_source() -> None:
    youtube = commands.parse_source("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s")
    assert (youtube.platform, youtube.source_id) == ("youtube", "dQw4w9WgXcQ")
    assert youtube.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    twitch = commands.parse_source("https://www.twitch.tv/videos/2900689447?t=1h2m")
    assert (twitch.platform, twitch.source_id, twitch.url) == (
        "twitch",
        "2900689447",
        "https://www.twitch.tv/videos/2900689447",
    )
    assert commands.parse_source("2900689447").platform == "twitch"
    with pytest.raises(ValueError):
        commands.parse_source("https://example.com/")


def test_parse_metadata_for_both_platforms() -> None:
    youtube = commands.parse_metadata(
        commands.parse_source("https://youtu.be/dQw4w9WgXcQ"),
        {
            "channel_id": "UC123",
            "uploader_id": "@Speedy",
            "channel": "Speedy RP",
            "title": "Night",
            "duration": 18000,
            "live_status": "was_live",
        },
    )
    assert (youtube.channel_id, youtube.handle, youtube.display_name) == ("UC123", "Speedy", "Speedy RP")
    assert youtube.duration_s == 18000.0 and youtube.was_live is True and youtube.title == "Night"
    upload = commands.parse_metadata(
        commands.parse_source("https://youtu.be/dQw4w9WgXcQ"),
        {"channel_id": "UC123", "channel": "Speedy RP", "live_status": "not_live"},
    )
    assert upload.was_live is False and upload.handle == "Speedy RP" and upload.duration_s is None
    twitch = commands.parse_metadata(
        commands.parse_source("2900689447"),
        {"uploader_id": "buddha", "uploader": "Buddha", "duration": 43000.5},
    )
    assert (twitch.channel_id, twitch.handle, twitch.was_live) == ("buddha", "buddha", True)
    with pytest.raises(ValueError):
        commands.parse_metadata(commands.parse_source("2900689447"), {})


def test_cli_refuses_a_url_that_is_no_video() -> None:
    from typer.testing import CliRunner

    from sixstars.cli import app

    runner = CliRunner()
    for command in ("process", "scan-only"):
        result = runner.invoke(app, ["vod", command, "https://example.com/v"])
        assert result.exit_code == commands.EXIT_USAGE

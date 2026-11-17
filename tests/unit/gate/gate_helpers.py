"""Builders for the gate rule tests: the documented default settings and a clip that passes both profiles."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sixstars.gate.models import (
    ClipFacts,
    CreatorFacts,
    FileFacts,
    GateSettings,
    PrecheckState,
    RecentClip,
    VisualFindings,
)
from sixstars.gate.profiles import settings_from_values

# Two weeks before the spoiler block ends (2026-12-03 00:00 UTC).
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
BLOCK_UNTIL = datetime(2026, 12, 3, 0, 0, tzinfo=UTC)

# docs/16-settings-reference.md plus the keys of migration 0007.
DEFAULT_VALUES: dict[str, Any] = {
    "gate.hourly": {
        "min_len": 20,
        "max_len": 60,
        "max_music_ratio": 0.1,
        "min_score": 7,
        "topic_dedupe_h": 48,
        "precheck_risky": True,
    },
    "gate.express": {
        "min_len": 15,
        "max_len": 45,
        "max_music_ratio": 0.0,
        "min_score": 8,
        "topic_dedupe_h": 6,
        "tiers": ["preferred", "auto_no_music"],
    },
    "gate.spoiler_block_until": "2026-12-03T00:00:00Z",
    "gate.reject_topics": [
        "real-world politics",
        "gambling",
        "sponsored segment",
        "real-world violence",
        "sexual content",
        "personal information",
    ],
    "gate.auto_creator_daily_cap": 4,
    "gate.spoiler_terms": ["final mission", "ending", "plot twist", "spoiler"],
    "gate.strict_max_music_ratio": 0.0,
    "gate.music_min_run_frames": 1,
    "gate.checks": {
        "max_black_s": 2.0,
        "max_freeze_s": 2.0,
        "freeze_noise": 0.003,
        "max_subtitle_drift_s": 0.3,
        "max_file_mb": 100,
        "width": 1080,
        "height": 1920,
        "duration_tolerance_s": 1.0,
    },
    "gate.duplicate": {
        "source_pad_s": 30,
        "topic_overlap_same_creator": 0.5,
        "topic_overlap_any_creator": 0.8,
        "transcript_overlap": 0.5,
    },
    "factory.end_card_s": 1.5,
}

TRANSCRIPT = "no way he actually landed that jump over the whole bridge that was insane chat"


def make_settings(**overrides: Any) -> GateSettings:
    """The documented defaults; `overrides` are `GateSettings` fields."""
    return replace(settings_from_values(DEFAULT_VALUES), **overrides)


def good_file(**overrides: Any) -> FileFacts:
    base = FileFacts(
        width=1080,
        height=1920,
        has_video=True,
        has_audio=True,
        duration_s=31.5,
        size_bytes=20_000_000,
        longest_black_s=0.0,
        longest_freeze_s=0.0,
    )
    return replace(base, **overrides)


def good_clip(**overrides: Any) -> ClipFacts:
    """A 30-second clip that passes every check of both profiles. With another `duration_s` the file
    length and the subtitle words follow it, unless they are given."""
    words = TRANSCRIPT.split()
    duration = overrides.get("duration_s")
    if isinstance(duration, int | float):
        step = duration / (len(words) + 1)
        overrides.setdefault("file", good_file(duration_s=duration + 1.5))
        overrides.setdefault("word_spans", tuple((i * step, (i + 0.5) * step) for i in range(len(words))))
    base = ClipFacts(
        id="clip-1",
        creator_id="creator-1",
        profile="hourly",
        lane="hourly",
        source_platform="youtube",
        source_id="video-1",
        start_s=1000.0,
        end_s=1030.0,
        duration_s=30.0,
        score=9.0,
        music_ratio=0.0,
        spoiler=False,
        safety_flags={
            "politics": False,
            "gambling": False,
            "sponsored": False,
            "real_violence": False,
            "sexual": False,
            "personal_info": False,
            "hate": False,
            "copyrighted_media": False,
        },
        hook="He did NOT expect that",
        title="Streamer jumps the whole bridge",
        caption="Nobody expected this jump",
        hashtags=("gta", "stunt", "bridge"),
        topic="streamer-jumps-whole-bridge",
        transcript_text=TRANSCRIPT,
        word_spans=tuple((1.0 + i * 1.5, 1.4 + i * 1.5) for i in range(len(words))),
        created_at=NOW - timedelta(minutes=2),
        file=good_file(),
    )
    return replace(base, **overrides)


def express_clip(**overrides: Any) -> ClipFacts:
    return good_clip(**{"profile": "express", "lane": "spike", **overrides})


def creator(tier: str = "preferred", **overrides: Any) -> CreatorFacts:
    return replace(CreatorFacts(id="creator-1", tier=tier, clips_today=0, prior_clips=5), **overrides)


def clean_visual(**overrides: Any) -> VisualFindings:
    base = VisualFindings(
        other_watermark=False,
        platform_logos=False,
        menu_or_loading=False,
        pause_or_brb_screen=False,
        empty_facecam=False,
        readable_subtitles=True,
    )
    return replace(base, **overrides)


def other_clip(**overrides: Any) -> RecentClip:
    """An approved clip of another creator, from another video, about something else, one hour ago."""
    base = RecentClip(
        id="clip-0",
        creator_id="creator-2",
        status="approved",
        source_platform="youtube",
        source_id="video-0",
        start_s=50.0,
        end_s=80.0,
        topic="cop-chase-ends-in-river",
        transcript_text="they are right behind me oh my god turn left turn left we are in the water",
        decided_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(hours=1, minutes=5),
    )
    return replace(base, **overrides)


UNAVAILABLE = PrecheckState.UNAVAILABLE
AVAILABLE = PrecheckState.AVAILABLE

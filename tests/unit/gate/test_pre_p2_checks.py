"""AI cost plan A5a: the checks before the clip writer reject only clips the gate is certain to reject, with
the gate's own functions and settings."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from gate_helpers import make_settings

from sixstars import settings
from sixstars.factory import precheck
from sixstars.gate import profiles
from sixstars.gate import store as gate_store
from sixstars.gate.models import CreatorFacts
from sixstars.scoring import config as scoring_config

AUTO = CreatorFacts(id="creator-1", tier="auto", clips_today=0)
PREFERRED = CreatorFacts(id="creator-1", tier="preferred", clips_today=0)
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


# ── lowest_music_ratio ───────────────────────────────────────


def test_music_everywhere_leaves_no_cut_under_the_limit() -> None:
    lowest = precheck.lowest_music_ratio([True] * 60, min_cut_frames=20, min_run_frames=1)
    assert lowest == pytest.approx(1 - 2 / 20)  # the shortest cut, less the two edge frames


def test_a_quiet_stretch_as_long_as_the_shortest_cut_gives_zero() -> None:
    frames = [True] * 40 + [False] * 20
    assert precheck.lowest_music_ratio(frames, min_cut_frames=20, min_run_frames=1) == 0.0


def test_short_runs_are_ignored_as_the_gate_ignores_them() -> None:
    frames = [True, False] * 30
    assert precheck.lowest_music_ratio(frames, min_cut_frames=20, min_run_frames=3) == 0.0
    assert precheck.lowest_music_ratio(frames, min_cut_frames=20, min_run_frames=1) > 0.3


def test_edge_frames_are_allowed_for() -> None:
    # 22 frames: music in all but the last two. A 20-frame cut at the end has 18 music frames; the gate may
    # measure one frame off at each edge, so 16 / 20 is the lowest it could see.
    frames = [True] * 20 + [False] * 2
    assert precheck.lowest_music_ratio(frames, min_cut_frames=20, min_run_frames=1) == pytest.approx(0.8)


def test_an_empty_window_has_no_music() -> None:
    assert precheck.lowest_music_ratio([], min_cut_frames=20, min_run_frames=1) == 0.0


# ── reject_reason ────────────────────────────────────────────


def test_an_hourly_clip_of_a_creator_at_the_daily_cap_is_rejected() -> None:
    gate = make_settings()
    capped = replace(AUTO, clips_today=gate.auto_creator_daily_cap)
    reason = precheck.reject_reason("hourly", capped, gate, music=0.0)
    assert reason is not None and reason.startswith("hourly: ") and "clips today" in reason
    assert precheck.reject_reason("hourly", replace(capped, clips_today=3), gate, music=0.0) is None


def test_music_over_the_hourly_limit_in_every_cut_is_rejected() -> None:
    gate = make_settings()
    reason = precheck.reject_reason("hourly", PREFERRED, gate, music=0.5)
    assert reason is not None and "music ratio" in reason
    assert precheck.reject_reason("hourly", PREFERRED, gate, music=gate.hourly.max_music_ratio) is None


def test_unknown_music_is_never_a_reason() -> None:
    assert precheck.reject_reason("hourly", PREFERRED, make_settings(), music=None) is None


def test_a_spike_clip_that_fails_express_but_may_pass_hourly_goes_on() -> None:
    gate = make_settings()
    # an auto creator with a little music: not allowed to post at once, but fine for the hourly lane
    assert precheck.reject_reason("express", AUTO, gate, music=0.05) is None


def test_a_spike_clip_is_rejected_only_when_both_profiles_fail() -> None:
    reason = precheck.reject_reason("express", AUTO, make_settings(), music=0.5)
    assert reason is not None and "express: " in reason and "hourly: " in reason


def test_an_unknown_profile_is_refused() -> None:
    with pytest.raises(ValueError):
        precheck.reject_reason("story", AUTO, make_settings(), music=0.0)


def test_near_day_end() -> None:
    assert precheck.near_day_end(datetime(2026, 11, 23, 23, 45, tzinfo=UTC))
    assert not precheck.near_day_end(datetime(2026, 11, 23, 23, 15, tzinfo=UTC))


# ── the same settings as the gate ────────────────────────────


@pytest.fixture
def gate_world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`profiles.load` gives these settings; no scoring flag, no per-platform lengths."""
    world: dict[str, Any] = {"gate": make_settings(), "creator": PREFERRED}
    monkeypatch.setattr(profiles, "load", lambda: world["gate"])
    monkeypatch.setattr(scoring_config, "flag", lambda key: None)
    monkeypatch.setattr(settings, "_all", lambda: {})
    monkeypatch.setattr(gate_store, "creator_facts", lambda creator_id, clip_id, now: world["creator"])
    return world


def test_the_music_limit_is_the_gates_own_setting(gate_world: dict[str, Any]) -> None:
    gate = gate_world["gate"]
    gate_world["gate"] = replace(gate, hourly=replace(gate.hourly, max_music_ratio=0.95))
    assert precheck.gate_settings().hourly.max_music_ratio == 0.95
    music = [True] * 60
    # every cut has about 90% music: under the gate's (changed) limit, so the clip goes on
    assert precheck.check("hourly", "creator-1", "clip-1", music, 1.0, NOW) is None
    gate_world["gate"] = gate
    reason = precheck.check("hourly", "creator-1", "clip-1", music, 1.0, NOW)
    assert reason is not None and reason.startswith("pre_p2: hourly: ")


def test_the_daily_cap_is_not_checked_just_before_midnight(gate_world: dict[str, Any]) -> None:
    gate_world["creator"] = replace(AUTO, clips_today=gate_world["gate"].auto_creator_daily_cap)
    assert precheck.check("hourly", "creator-1", "clip-1", None, 1.0, NOW) is not None
    late = datetime(2026, 11, 23, 23, 50, tzinfo=UTC)
    assert precheck.check("hourly", "creator-1", "clip-1", None, 1.0, late) is None


def test_a_clip_without_a_creator_is_not_checked(gate_world: dict[str, Any]) -> None:
    assert precheck.check("hourly", None, "clip-1", [True] * 60, 1.0, NOW) is None

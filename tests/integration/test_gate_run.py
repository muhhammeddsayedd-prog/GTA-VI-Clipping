"""The gate against the test database: loading the inputs, storing the verdict, events, Storage upload and
the hand-off to the posting engine. Files are real (ffmpeg); Claude, YouTube, Storage and Upload-Post are
fakes, so nothing is uploaded or posted anywhere."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gate_support import (
    CLIP_S,
    P5_CLEAN,
    SAFE,
    Kit,
    add_clip,
    add_creator,
    add_spike,
    black_video,
    clip,
    clip_events,
    count,
    distinct,
    frozen_video,
    good_video,
    tables,
)
from posting_support import seed, use_env

from sixstars import db, jobs, settings
from sixstars.clients.anthropic_client import AnthropicError
from sixstars.factory import pipeline
from sixstars.gate import gate, profiles, store
from sixstars.gate.models import PrecheckState, ReasonCode, Verdict
from sixstars.posting.gate import Mode
from sixstars.posting.precedence import demoted_rank

pytestmark = [pytest.mark.ffmpeg, pytest.mark.usefixtures("tables")]
__all__ = ["black_video", "frozen_video", "good_video", "tables"]  # fixtures


def test_migration_0007_seeds_the_gate_settings() -> None:
    loaded = profiles.load()
    assert loaded.strict_max_music_ratio == 0.0 and loaded.music_min_run_frames == 1
    assert "final mission" in loaded.spoiler_terms and "gambling" in loaded.reject_topics
    assert (loaded.max_black_s, loaded.max_freeze_s, loaded.source_pad_s) == (2.0, 2.0, 30.0)
    assert loaded.express.max_music_ratio == 0.0  # the seeded value is left alone
    assert settings.get_int("precheck.max_status_checks") == 3


def test_a_good_hourly_clip_is_approved_stored_and_left_in_the_queue(good_video: Path) -> None:
    kit = Kit()
    clip_id = add_clip(add_creator(), good_video)
    outcome = gate.run_gate(clip_id, deps=kit.deps)

    assert outcome.status == "approved" and outcome.decision is not None and outcome.decision.approved
    row = clip(clip_id)
    assert (row["status"], row["lane"], row["gate_profile"]) == ("approved", "hourly", "hourly")
    assert row["approved_at"] is not None and row["reject_reason"] is None
    assert float(row["rank_score"]) == 9.0
    result = row["gate_result"]
    assert result["profile"] == "hourly" and result["passed"] is True and result["codes"] == []
    assert all(check["ok"] for check in result["checks"].values())
    assert result["inputs"]["file"]["width"] == 1080 and result["inputs"]["visual"] == P5_CLEAN
    assert result["precheck"] == "unavailable"
    # Uploaded to Storage, local copy kept for posting.
    assert row["storage_path"] == outcome.storage_path and row["storage_path"] in kit.storage.objects
    assert Path(row["local_path"]).is_file()
    approved = clip_events(clip_id, "clip.approved")
    assert len(approved) == 1 and approved[0]["data"]["lane"] == "hourly"
    # Hourly clips are not handed to the posting engine, and nothing is sent to Telegram.
    assert outcome.spike_run is None and count("posts") == 0 and kit.upload_post.uploads == {}
    assert count("notify_outbox") == 0
    assert kit.p5_calls == 1
    assert kit.ai.calls_to("complete_json")[0]["images"] == 4


def test_running_the_gate_twice_changes_nothing(good_video: Path) -> None:
    kit = Kit()
    clip_id = add_clip(add_creator(), good_video)
    gate.run_gate(clip_id, deps=kit.deps)
    again = gate.run_gate(clip_id, deps=kit.deps)
    assert again.skipped == "status is approved" and again.decision is None
    assert kit.p5_calls == 1 and len(clip_events(clip_id, "clip.approved")) == 1


def test_an_unknown_clip_is_an_error() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        gate.run_gate("00000000-0000-0000-0000-000000000000", deps=Kit().deps)


@pytest.mark.parametrize(
    ("overrides", "p5", "code"),
    [
        ({"music_ratio": 0.3}, None, "music_over_limit"),
        ({"music_ratio": None}, None, "music_unknown"),
        ({"music_ratio": 0.05}, None, "music_strict"),
        ({"hook": "The final mission is wild"}, None, "spoiler_in_text"),
        ({"hashtags": ["gta", "FinalMission"]}, None, "spoiler_in_text"),
        ({"commentary": "The ending nobody expected"}, None, "spoiler_in_text"),
        ({"spoiler": True}, None, "spoiler_blocked"),
        ({"score": 6}, None, "score_too_low"),
        ({"score": None}, None, "score_unknown"),
        ({"safety_flags": {**SAFE, "gambling": True}}, None, "safety_flag"),
        ({"safety_flags": None}, None, "safety_unknown"),
        ({"topic": "real-world-politics-rant"}, None, "reject_topic"),
        ({"duration_s": 70.0}, None, "too_long"),
        ({}, {**P5_CLEAN, "other_watermark": True, "notes": "@othercreator"}, "watermark"),
        ({}, {**P5_CLEAN, "platform_logos": True}, "platform_logo"),
        ({}, {**P5_CLEAN, "menu_or_loading": True}, "menu_or_loading"),
        ({}, {**P5_CLEAN, "readable_subtitles": False}, "unreadable_subtitles"),
    ],
)
def test_bad_clips_are_rejected_with_the_right_reason(
    good_video: Path, overrides: dict[str, Any], p5: dict[str, Any] | None, code: str
) -> None:
    kit = Kit(p5=p5)
    clip_id = add_clip(add_creator(), good_video, **overrides)
    outcome = gate.run_gate(clip_id, deps=kit.deps)

    assert outcome.status == "rejected" and outcome.decision is not None
    row = clip(clip_id)
    assert row["status"] == "rejected" and row["reject_reason"] == code
    assert row["approved_at"] is None and row["rank_score"] is None and row["storage_path"] is None
    assert row["gate_result"]["passed"] is False and code in row["gate_result"]["codes"]
    rejected = clip_events(clip_id, "clip.rejected")
    assert len(rejected) == 1 and code in rejected[0]["data"]["codes"] and rejected[0]["message"]
    # A rejection is an event for the daily report, never a Telegram message, never an upload.
    assert count("notify_outbox") == 0 and kit.storage.objects == {} and count("posts") == 0
    # P5 is only paid for when it can still change the verdict.
    assert kit.p5_calls == (1 if p5 is not None else 0)
    assert row["gate_result"]["inputs"]["p5"] == (
        "run" if p5 is not None else row["gate_result"]["inputs"]["p5"]
    )


def test_black_frames_are_rejected(black_video: Path) -> None:
    kit = Kit()
    clip_id = add_clip(add_creator(), black_video)
    assert gate.run_gate(clip_id, deps=kit.deps).status == "rejected"
    row = clip(clip_id)
    assert row["reject_reason"] == "black_frames"
    assert row["gate_result"]["checks"]["visual"]["value"]["longest_black_s"] == pytest.approx(3.0, abs=0.3)
    assert kit.p5_calls == 0


def test_a_frozen_picture_is_rejected(frozen_video: Path) -> None:
    clip_id = add_clip(add_creator(), frozen_video)
    assert gate.run_gate(clip_id, deps=Kit().deps).status == "rejected"
    assert clip(clip_id)["reject_reason"] == "frozen_video"


@pytest.mark.parametrize("problem", ["missing", "empty", "garbage", "no_path"])
def test_a_missing_or_unreadable_file_is_rejected(tmp_path: Path, problem: str) -> None:
    path: Path | None = tmp_path / "clip.mp4"
    assert path is not None
    if problem == "empty":
        path.write_bytes(b"")
    elif problem == "garbage":
        path.write_bytes(b"this is not a video" * 100)
    elif problem == "no_path":
        path = None
    clip_id = add_clip(add_creator(), None, local_path=None if path is None else str(path))
    assert gate.run_gate(clip_id, deps=Kit().deps).status == "rejected"
    assert clip(clip_id)["reject_reason"] == "file_missing"


def test_wrong_frame_size_is_rejected(tmp_path: Path) -> None:
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    small = tmp_path / "small.mp4"
    command = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=540x960:r=10:d=22.5"]
    command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=22.5", "-shortest", "-preset", "ultrafast"]
    subprocess.run([*command, str(small)], check=True, capture_output=True, timeout=120)
    clip_id = add_clip(add_creator(), small)
    gate.run_gate(clip_id, deps=Kit().deps)
    assert clip(clip_id)["reject_reason"] == "file_resolution"


@pytest.mark.parametrize("tier", ["restricted", "blocked"])
def test_a_forbidden_creator_never_gets_an_approved_clip(good_video: Path, tier: str) -> None:
    kit = Kit()
    for lane in ("hourly", "spike"):
        clip_id = add_clip(add_creator(tier, handle=f"{tier}-{lane}"), good_video, lane=lane, score=10)
        gate.run_gate(clip_id, deps=kit.deps)
        row = clip(clip_id)
        assert (row["status"], row["reject_reason"]) == ("rejected", "creator_forbidden")
    assert kit.p5_calls == 0 and count("posts") == 0


def test_auto_creator_daily_cap_counts_posted_and_waiting_clips(good_video: Path) -> None:
    kit = Kit()
    creator_id = add_creator("auto")
    now = datetime.now(UTC)
    two_days_ago = now - timedelta(days=2)
    add_clip(creator_id, None, status="posted", posted_at=now, **distinct(1))
    add_clip(creator_id, None, status="posted", posted_at=now, **distinct(2))
    add_clip(creator_id, None, status="approved", approved_at=two_days_ago, **distinct(3))
    add_clip(creator_id, None, status="posted", posted_at=two_days_ago, **distinct(4))
    add_clip(creator_id, None, status="rejected", **distinct(5))
    facts = store.creator_facts(creator_id, "00000000-0000-0000-0000-000000000000", now)
    assert (facts.tier, facts.clips_today, facts.prior_clips) == ("auto", 3, 4)

    fourth = add_clip(creator_id, good_video, **distinct(6))
    assert gate.run_gate(fourth, deps=kit.deps).status == "approved"  # 3 so far, the cap is 4
    fifth = add_clip(creator_id, good_video, **distinct(7))
    gate.run_gate(fifth, deps=kit.deps)
    assert clip(fifth)["reject_reason"] == "creator_daily_cap"


def test_yesterdays_posts_do_not_count_today() -> None:
    creator_id = add_creator("auto")
    midnight = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    add_clip(creator_id, None, status="posted", posted_at=midnight - timedelta(seconds=1), source_id="a")
    add_clip(creator_id, None, status="posted", posted_at=midnight, source_id="b")
    facts = store.creator_facts(
        creator_id, "00000000-0000-0000-0000-000000000000", midnight + timedelta(hours=5)
    )
    assert facts.clips_today == 1


# ── Duplicates ───────────────────────────────────────────────


def test_same_source_range_is_rejected_whatever_its_age(good_video: Path) -> None:
    creator_id = add_creator()
    long_ago = datetime.now(UTC) - timedelta(days=20)
    add_clip(
        creator_id,
        None,
        status="posted",
        posted_at=long_ago,
        approved_at=long_ago,
        created_at=long_ago,
        start_s=1010.0,
        end_s=1040.0,
        topic="old-topic-words",
    )
    clip_id = add_clip(creator_id, good_video)
    gate.run_gate(clip_id, deps=Kit().deps)
    assert clip(clip_id)["reject_reason"] == "duplicate_source_range"


def test_reworded_topic_of_the_same_moment_is_a_duplicate(good_video: Path) -> None:
    """P2 named one moment two ways (progress notes M05); the second clip comes from another video id."""
    creator_id = add_creator()
    first = add_clip(creator_id, good_video, topic="new-player-robs-man-on-ground", source_id="live-1")
    second = add_clip(
        creator_id,
        good_video,
        topic="player-caught-robbing-downed-player",
        source_id="vod-9",
        transcript={"words": [{"w": "different", "s": 1.0, "e": 1.4}, {"w": "words", "s": 2.0, "e": 2.4}]},
    )
    kit = Kit()
    assert gate.run_gate(first, deps=kit.deps).status == "approved"
    gate.run_gate(second, deps=kit.deps)
    assert clip(second)["reject_reason"] == "duplicate_topic"


def test_same_speech_from_another_source_is_a_duplicate(good_video: Path) -> None:
    kit = Kit()
    first = add_clip(add_creator(handle="one"), good_video, source_id="live-1", topic="alpha-beta-gamma")
    second = add_clip(add_creator(handle="two"), good_video, source_id="vod-9", topic="delta-epsilon-zeta")
    assert gate.run_gate(first, deps=kit.deps).status == "approved"
    gate.run_gate(second, deps=kit.deps)
    assert clip(second)["reject_reason"] == "duplicate_transcript"


def test_an_old_topic_is_no_duplicate_for_hourly_but_the_window_is_per_profile(good_video: Path) -> None:
    creator_id = add_creator()
    other = add_creator(handle="other")
    different = {"words": [{"w": f"w{i}", "s": float(i), "e": i + 0.4} for i in range(10)]}
    ten_hours_ago = datetime.now(UTC) - timedelta(hours=10)
    add_clip(
        other,
        None,
        status="posted",
        approved_at=ten_hours_ago,
        posted_at=ten_hours_ago,
        source_id="x",
        transcript=different,
    )
    kit = Kit()
    spike = add_clip(creator_id, good_video, lane="spike", source_id="y")
    assert gate.run_gate(spike, deps=kit.deps).decision.profile == "express"  # type: ignore[union-attr]
    hourly = add_clip(creator_id, good_video, source_id="z", transcript=different, start_s=5.0, end_s=26.0)
    gate.run_gate(hourly, deps=kit.deps)
    assert clip(hourly)["reject_reason"] == "duplicate_topic"


def test_of_two_clips_of_one_moment_in_the_gate_only_the_older_is_approved(good_video: Path) -> None:
    creator_id = add_creator()
    earlier = datetime.now(UTC) - timedelta(minutes=1)
    older = add_clip(creator_id, good_video, created_at=earlier)
    newer = add_clip(creator_id, good_video, start_s=1005.0, end_s=1026.0)
    kit = Kit()
    # Whichever is judged first.
    gate.run_gate(newer, deps=kit.deps)
    gate.run_gate(older, deps=kit.deps)
    assert clip(newer)["reject_reason"] == "duplicate_source_range"
    assert clip(older)["status"] == "approved"


def test_a_clip_is_never_approved_without_p5_when_its_reason_to_reject_disappears(
    good_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate skips P5 for a clip it will reject anyway. If that reason is gone by the time the verdict
    is stored (here: the clip it duplicated was thrown away in between), P5 must still be asked."""
    creator_id = add_creator()
    now = datetime.now(UTC)
    first = add_clip(creator_id, None, status="approved", approved_at=now)
    second = add_clip(creator_id, good_video, start_s=1005.0, end_s=1026.0)
    kit = Kit(p5={**P5_CLEAN, "other_watermark": True})
    real_decide = gate._decide
    calls: list[str] = []

    def decide_then_remove_the_duplicate(*args: Any) -> Any:
        decision = real_decide(*args)
        if not calls:
            db.execute("update clips set status = 'rejected' where id = %s", (first,))
        calls.append(decision.verdict.value)
        return decision

    monkeypatch.setattr(gate, "_decide", decide_then_remove_the_duplicate)
    outcome = gate.run_gate(second, deps=kit.deps)

    assert calls[0] == "reject"  # the first look: a duplicate, so P5 was not asked yet
    assert kit.p5_calls == 1  # asked after all
    assert outcome.status == "rejected" and clip(second)["reject_reason"] == "watermark"
    assert clip(second)["gate_result"]["inputs"]["p5"] == "run"


# ── Music frames from the render event ───────────────────────


def test_isolated_music_frame_follows_the_setting(good_video: Path) -> None:
    frames = [0] * 21
    frames[7] = 1
    creator_id = add_creator()
    strict = add_clip(creator_id, good_video, music_ratio=round(1 / 21, 4), music_frames=frames)
    gate.run_gate(strict, deps=Kit().deps)
    assert clip(strict)["reject_reason"] == "music_strict"  # seeded: every flagged frame counts

    settings.set("gate.music_min_run_frames", 2)
    lenient = add_clip(
        creator_id,
        good_video,
        music_ratio=round(1 / 21, 4),
        music_frames=frames,
        source_id="video-2",
        topic="some-other-topic",
        transcript={"words": [{"w": f"w{i}", "s": float(i), "e": i + 0.4} for i in range(10)]},
    )
    assert gate.run_gate(lenient, deps=Kit().deps).status == "approved"
    assert clip(lenient)["gate_result"]["checks"]["music"]["value"] == 0.0


# ── Express: hand-off, demotion ──────────────────────────────


def test_an_approved_spike_clip_is_handed_to_the_posting_engine_in_dry_run(
    good_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_env(monkeypatch, "production")
    seed("production")
    kit = Kit()
    creator_id = add_creator()
    clip_id = add_clip(creator_id, good_video, lane="spike", spike_id=add_spike(creator_id))
    outcome = gate.run_gate(clip_id, deps=kit.deps)

    row = clip(clip_id)
    assert (row["status"], row["lane"], row["gate_profile"]) == ("approved", "spike", "express")
    assert outcome.spike_run is not None and outcome.spike_run.known and outcome.handoff_error is None
    assert outcome.spike_run.mode is Mode.DRY  # launch.dry_run is true: decided, nothing sent
    assert kit.upload_post.uploads == {}
    assert count("posts", "dry_run") == len(outcome.spike_run.dry_run_ids) > 0
    assert count("posts", "not dry_run") == 0


def test_a_live_hand_off_reaches_only_the_fake_client(
    good_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_env(monkeypatch, "staging")
    seed("staging")
    settings.set("launch.dry_run", False)
    kit = Kit()
    creator_id = add_creator()
    clip_id = add_clip(creator_id, good_video, lane="spike", spike_id=add_spike(creator_id))
    outcome = gate.run_gate(clip_id, deps=kit.deps)

    assert outcome.spike_run is not None and outcome.spike_run.mode is Mode.LIVE
    assert len(kit.upload_post.uploads) == len(outcome.spike_run.post_ids) > 0
    assert clip(clip_id)["status"] in ("approved", "posted")


def test_under_the_emergency_stop_the_gate_still_judges_and_nothing_is_posted(
    good_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_env(monkeypatch, "staging")
    seed("staging")
    settings.set("launch.dry_run", False)
    settings.set("control.emergency_stop", True)
    kit = Kit()
    creator_id = add_creator()
    clip_id = add_clip(creator_id, good_video, lane="spike", spike_id=add_spike(creator_id))
    outcome = gate.run_gate(clip_id, deps=kit.deps)
    assert clip(clip_id)["status"] == "approved"
    assert outcome.spike_run is not None and outcome.spike_run.mode is Mode.STOPPED
    assert kit.upload_post.uploads == {} and count("posts") == 0


def test_a_failed_hand_off_keeps_the_approval_and_queues_a_retry(good_video: Path) -> None:
    kit = Kit()

    def broken() -> Any:
        raise RuntimeError("no Upload-Post key")

    kit.deps.upload_post = broken
    creator_id = add_creator()
    clip_id = add_clip(creator_id, good_video, lane="spike", spike_id=add_spike(creator_id))
    outcome = gate.run_gate(clip_id, deps=kit.deps)
    assert clip(clip_id)["status"] == "approved"
    assert outcome.handoff_error is not None and "no Upload-Post key" in outcome.handoff_error
    retry = db.fetch_one("select queue, payload from jobs where type = 'post.retry'")
    assert retry is not None and retry["queue"] == "express"
    assert retry["payload"] == {"kind": "spike", "clip_id": clip_id}
    assert len(clip_events(clip_id, "gate.handoff_failed")) == 1


def test_an_express_clip_that_only_fails_express_rules_moves_to_the_hourly_lane(good_video: Path) -> None:
    kit = Kit()
    creator_id = add_creator()
    clip_id = add_clip(creator_id, good_video, lane="spike", score=7, spike_id=add_spike(creator_id))
    outcome = gate.run_gate(clip_id, deps=kit.deps)

    row = clip(clip_id)
    assert (row["status"], row["lane"], row["gate_profile"]) == ("approved", "hourly", "hourly")
    assert float(row["rank_score"]) == pytest.approx(demoted_rank(7.0))
    assert row["gate_result"]["demoted"] is True
    assert row["gate_result"]["express"]["codes"] == ["score_too_low"]
    assert clip_events(clip_id, "clip.approved")[0]["data"]["express_codes"] == ["score_too_low"]
    # It waits for the hourly slot: no spike hand-off.
    assert outcome.spike_run is None and count("posts") == 0


def test_an_express_clip_that_fails_the_hourly_profile_too_is_rejected(good_video: Path) -> None:
    creator_id = add_creator()
    clip_id = add_clip(creator_id, good_video, lane="spike", score=6, spike_id=add_spike(creator_id))
    gate.run_gate(clip_id, deps=Kit().deps)
    row = clip(clip_id)
    assert (row["status"], row["lane"], row["reject_reason"]) == ("rejected", "spike", "score_too_low")
    assert row["gate_result"]["hourly_fallback"]["codes"] == ["score_too_low"]


def test_storage_failure_does_not_undo_the_approval(good_video: Path) -> None:
    kit = Kit()
    kit.storage.error = RuntimeError("bucket is down")
    clip_id = add_clip(add_creator(), good_video)
    outcome = gate.run_gate(clip_id, deps=kit.deps)
    assert outcome.status == "approved" and outcome.storage_path is None
    assert clip(clip_id)["status"] == "approved"
    assert len(clip_events(clip_id, "clip.upload_failed")) == 1


# ── Failures of the gate itself, the factory hook, the jobs ──


def _job(job_type: str, payload: dict[str, Any], attempts: int = 1, queue: str = "normal") -> jobs.Job:
    job_id = jobs.enqueue(queue, job_type, payload)  # type: ignore[arg-type]
    assert job_id is not None
    db.execute("update jobs set status = 'running', attempts = %s where id = %s", (attempts, job_id))
    job = jobs.get(job_id)
    assert job is not None
    return job


def test_a_p5_error_leaves_the_clip_in_gating(good_video: Path) -> None:
    kit = Kit(p5=AnthropicError("overloaded"))
    clip_id = add_clip(add_creator(), good_video)
    with pytest.raises(AnthropicError):
        gate.run_gate(clip_id, deps=kit.deps)
    assert clip(clip_id)["status"] == "gating" and clip_events(clip_id, "clip.") == []


def test_install_wires_the_factory_hook_and_the_job_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    from sixstars.services import worker

    monkeypatch.setattr(worker, "HANDLERS", {})
    monkeypatch.setattr(pipeline, "_after_render", {})
    gate.install()
    gate.install()  # twice is fine
    assert pipeline._after_render == {"clip.render": gate.after_render, "express.render": gate.after_render}
    assert set(worker.HANDLERS) == {"clip.gate", "clip.precheck", "clip.precheck_result"}


def test_the_factory_hook_gates_the_clip(good_video: Path) -> None:
    kit = Kit()
    gate.configure(kit.deps)
    clip_id = add_clip(add_creator(), good_video)
    gate.after_render(clip_id, _job("clip.render", {}))
    assert clip(clip_id)["status"] == "approved"


def test_the_hook_never_raises_and_queues_a_gate_job(good_video: Path) -> None:
    gate.configure(Kit(p5=AnthropicError("overloaded")).deps)
    clip_id = add_clip(add_creator(), good_video, lane="spike")
    gate.after_render(clip_id, _job("express.render", {}, queue="express"))
    assert clip(clip_id)["status"] == "gating"
    queued = db.fetch_one("select queue, payload, run_after from jobs where type = 'clip.gate'")
    assert queued is not None and queued["queue"] == "express" and queued["payload"] == {"clip_id": clip_id}


def test_the_gate_job_retries_then_rejects_with_gate_error(good_video: Path) -> None:
    gate.configure(Kit(p5=AnthropicError("overloaded")).deps)
    clip_id = add_clip(add_creator(), good_video)
    with pytest.raises(AnthropicError):
        gate.handle_gate(_job("clip.gate", {"clip_id": clip_id}, attempts=1))
    assert clip(clip_id)["status"] == "gating"

    gate.handle_gate(_job("clip.gate", {"clip_id": clip_id}, attempts=3))
    row = clip(clip_id)
    assert (row["status"], row["reject_reason"]) == ("rejected", ReasonCode.GATE_ERROR.value)
    assert clip_events(clip_id, "clip.rejected")[0]["level"] == "error"
    assert count("notify_outbox", "severity = 'warn'") == 1  # an operational failure, not a verdict


def test_the_gate_job_gates_a_clip(good_video: Path) -> None:
    gate.configure(Kit().deps)
    clip_id = add_clip(add_creator(), good_video)
    gate.handle_gate(_job("clip.gate", {"clip_id": clip_id}))
    assert clip(clip_id)["status"] == "approved"


def test_a_used_up_ai_budget_defers_the_gate(good_video: Path) -> None:
    settings.set("ai.daily_budget_usd", 0)
    gate.configure(Kit().deps)
    clip_id = add_clip(add_creator(), good_video)  # hourly clip: P5 runs under the VOD budget
    gate.after_render(clip_id, _job("clip.render", {}))
    assert clip(clip_id)["status"] == "gating"
    queued = db.fetch_one("select run_after from jobs where type = 'clip.gate'")
    assert queued is not None and queued["run_after"] > datetime.now(UTC)
    # An express clip is never stopped by the budget.
    creator_id = add_creator(handle="spiky")
    spike = add_clip(creator_id, good_video, lane="spike", source_id="other")
    kit = Kit()
    kit.deps.hand_off = False
    assert gate.run_gate(spike, deps=kit.deps).status == "approved"


def test_run_pending_gates_every_waiting_clip(good_video: Path) -> None:
    kit = Kit()
    creator_id = add_creator()
    first = add_clip(
        creator_id,
        good_video,
        source_id="a",
        topic="alpha-beta-gamma",
        transcript={"words": [{"w": f"a{i}", "s": float(i), "e": i + 0.4} for i in range(10)]},
    )
    second = add_clip(creator_id, good_video, source_id="b", score=3)
    outcomes = gate.run_pending(deps=kit.deps)
    assert {outcome.clip_id: outcome.status for outcome in outcomes} == {
        first: "approved",
        second: "rejected",
    }
    assert gate.run_pending(deps=kit.deps) == []


def test_resolve_precheck_refuses_a_state_that_is_no_outcome(good_video: Path) -> None:
    kit = Kit(oauth=True)
    clip_id = add_clip(add_creator(), good_video, music_ratio=0.05)
    held = gate.run_gate(clip_id, deps=kit.deps)
    assert held.decision is not None and held.decision.verdict is Verdict.HOLD
    with pytest.raises(ValueError, match="outcome"):
        gate.resolve_precheck(clip_id, PrecheckState.AVAILABLE, deps=kit.deps)
    assert CLIP_S == 21.0

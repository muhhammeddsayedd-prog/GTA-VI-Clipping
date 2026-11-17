"""The `sixstars gate` commands against the test database, with fakes for every outside service."""

import json
from pathlib import Path

import pytest
from gate_support import (
    P5_CLEAN,
    Kit,
    add_clip,
    add_creator,
    add_spike,
    clip,
    count,
    distinct,
    good_video,
    tables,
)
from typer.testing import CliRunner

from sixstars.cli import app
from sixstars.gate import gate

pytestmark = [pytest.mark.ffmpeg, pytest.mark.usefixtures("tables")]
__all__ = ["good_video", "tables"]  # fixtures
runner = CliRunner()


def test_gate_run_approves_and_prints_the_verdict(good_video: Path) -> None:
    gate.configure(Kit().deps)
    clip_id = add_clip(add_creator(), good_video)
    result = runner.invoke(app, ["gate", "run", clip_id])
    assert result.exit_code == 0, result.output
    assert f"{clip_id}: approve profile=hourly lane=hourly" in result.output
    assert clip(clip_id)["status"] == "approved"


def test_gate_run_prints_every_reason_of_a_rejection(good_video: Path) -> None:
    gate.configure(Kit().deps)
    clip_id = add_clip(add_creator(), good_video, score=5, music_ratio=0.4, hook="The ending")
    result = runner.invoke(app, ["gate", "run", clip_id])
    assert result.exit_code == 1
    for code in ("music_over_limit", "spoiler_in_text", "score_too_low"):
        assert f"  {code}: " in result.output
    again = runner.invoke(app, ["gate", "run", clip_id])
    assert again.exit_code == 2 and "not judged (status is rejected)" in again.output


def test_gate_run_with_an_unknown_clip() -> None:
    result = runner.invoke(app, ["gate", "run", "00000000-0000-0000-0000-000000000000"])
    assert result.exit_code == 2 and "does not exist" in result.output
    assert runner.invoke(app, ["gate", "run", "not-a-uuid"]).exit_code == 2


def test_gate_run_no_post_leaves_a_spike_clip_approved_without_the_hand_off(good_video: Path) -> None:
    kit = Kit()
    gate.configure(kit.deps)
    creator_id = add_creator()
    clip_id = add_clip(creator_id, good_video, lane="spike", spike_id=add_spike(creator_id))
    result = runner.invoke(app, ["gate", "run", clip_id, "--no-post"])
    assert result.exit_code == 0 and "lane=spike" in result.output
    assert (clip(clip_id)["status"], clip(clip_id)["lane"]) == ("approved", "spike")
    assert count("posts") == 0 and count("events", "type like 'posting.%' or type like 'post.%'") == 0
    assert gate.current_deps().hand_off is True  # the option does not stick


def test_gate_pending_judges_all_waiting_clips(good_video: Path) -> None:
    gate.configure(Kit().deps)
    creator_id = add_creator()
    add_clip(creator_id, good_video)
    add_clip(creator_id, good_video, score=2, **distinct(1))
    result = runner.invoke(app, ["gate", "pending"])
    assert result.exit_code == 0 and "2 clips judged" in result.output
    assert count("clips", "status = 'approved'") == 1 and count("clips", "status = 'rejected'") == 1


def test_gate_explain_shows_the_stored_checks_and_writes_nothing(good_video: Path) -> None:
    gate.configure(Kit().deps)
    clip_id = add_clip(add_creator(), good_video, score=5)
    waiting = runner.invoke(app, ["gate", "explain", clip_id])
    assert waiting.exit_code == 0 and "has not judged" in waiting.output
    runner.invoke(app, ["gate", "run", clip_id])
    events_before = count("events")
    result = runner.invoke(app, ["gate", "explain", clip_id])
    assert result.exit_code == 0
    assert "status=rejected" in result.output and "verdict=reject" in result.output
    assert "FAIL score: value=5.0 limit=7.0" in result.output and "ok   length:" in result.output
    assert count("events") == events_before
    missing = runner.invoke(app, ["gate", "explain", "00000000-0000-0000-0000-000000000000"])
    assert missing.exit_code == 2
    assert runner.invoke(app, ["gate", "explain", "not-a-uuid"]).exit_code == 2


def test_gate_check_file_measures_without_touching_clips(
    good_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = runner.invoke(app, ["gate", "check-file", str(good_video), "--duration", "21"])
    assert result.exit_code == 0, result.output
    measured = next(line for line in result.output.splitlines() if line.startswith('{"file"'))
    facts = json.loads(measured)["file"]
    assert (facts["width"], facts["height"], facts["has_audio"]) == (1080, 1920, True)
    assert facts["longest_black_s"] == 0.0 and count("clips") == 0
    assert runner.invoke(app, ["gate", "check-file", "no-such-file.mp4"]).exit_code == 2


def test_gate_check_file_with_p5(good_video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sixstars.clients.fakes import FakeAnthropicClient
    from sixstars.gate import visual

    seen: list[int] = []

    def fake_look(path: Path, clip_s: float, **kwargs: object) -> object:
        fake = FakeAnthropicClient({"p5_visual_check": {**P5_CLEAN, "platform_logos": True}})
        frames = visual.sample_frames(path, clip_s)
        seen.append(len(frames))
        return visual.check_frames(frames, client=fake)

    monkeypatch.setattr(visual, "look_at_file", fake_look)
    result = runner.invoke(app, ["gate", "check-file", str(good_video), "--p5"])
    assert result.exit_code == 1 and "P5: flags platform_logos" in result.output
    assert seen == [4]

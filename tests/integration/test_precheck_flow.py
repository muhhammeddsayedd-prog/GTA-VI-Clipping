"""The YouTube pre-check end to end against the test database and `FakeYouTubeClient`: unavailable (no
OAuth token, decision D103), available, passed, claimed, inconclusive, and the cases where it stops being
possible half way. No real upload happens."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gate_support import (
    Kit,
    add_clip,
    add_creator,
    add_spike,
    clip,
    clip_events,
    count,
    distinct,
    good_video,
    tables,
)

from sixstars import db, jobs, settings
from sixstars.clients._http import ApiError
from sixstars.gate import gate
from sixstars.gate.models import PrecheckState
from sixstars.posting.precedence import demoted_rank
from sixstars.precheck import state as precheck_state
from sixstars.precheck import youtube_precheck
from sixstars.precheck.youtube_precheck import handle_precheck, handle_precheck_result

pytestmark = [pytest.mark.ffmpeg, pytest.mark.usefixtures("tables")]
__all__ = ["good_video", "tables"]  # fixtures


def _job(job_type: str, attempts: int = 1) -> jobs.Job:
    """The queued job of that type, as a worker would claim it."""
    row = db.fetch_one(
        "select id from jobs where type = %s and status = 'queued' order by id desc", (job_type,)
    )
    assert row is not None, f"no queued {job_type} job"
    db.execute("update jobs set status = 'running', attempts = %s where id = %s", (attempts, row["id"]))
    job = jobs.get(int(row["id"]))
    assert job is not None
    return job


def _held(kit: Kit, video: Path, tier: str = "preferred", **overrides: Any) -> str:
    """A risky clip (music ratio 0.05) taken through the gate with the pre-check available."""
    gate.configure(kit.deps)
    clip_id = add_clip(add_creator(tier), video, **{"music_ratio": 0.05, **overrides})
    outcome = gate.run_gate(clip_id)
    assert outcome.status == "precheck_pending", outcome
    return clip_id


def test_without_an_oauth_token_the_precheck_is_unavailable_and_the_strict_rule_applies(
    good_video: Path,
) -> None:
    kit = Kit(oauth=False)
    now = datetime.now(UTC)
    assert precheck_state.current_state(kit.youtube, now) is PrecheckState.UNAVAILABLE

    creator_id = add_creator("auto")
    with_music = add_clip(creator_id, good_video, music_ratio=0.05)
    silent = add_clip(creator_id, good_video, **distinct(1))  # first clip of an auto creator: risky
    unknown = add_clip(creator_id, good_video, music_ratio=None, **distinct(2))
    assert gate.run_gate(with_music, deps=kit.deps).status == "rejected"
    assert gate.run_gate(silent, deps=kit.deps).status == "approved"
    assert gate.run_gate(unknown, deps=kit.deps).status == "rejected"

    assert clip(with_music)["reject_reason"] == "music_strict"
    assert clip(unknown)["reject_reason"] == "music_unknown"
    assert clip(with_music)["gate_result"]["precheck"] == "unavailable"
    assert clip(silent)["needs_precheck"] is False
    # Nothing was uploaded, queued or reported as an error.
    assert kit.youtube.calls_to("upload_private") == [] and count("jobs") == 0
    assert count("events", "level in ('warn', 'error', 'critical')") == 0 and count("notify_outbox") == 0


def test_when_a_token_appears_the_same_clip_is_prechecked_with_no_other_change(good_video: Path) -> None:
    creator_id = add_creator()
    before = add_clip(creator_id, good_video, music_ratio=0.05)
    after = add_clip(creator_id, good_video, music_ratio=0.05, **distinct(1))
    assert gate.run_gate(before, deps=Kit(oauth=False).deps).status == "rejected"
    kit = Kit(oauth=True)
    outcome = gate.run_gate(after, deps=kit.deps)
    assert outcome.status == "precheck_pending" and outcome.precheck_job_id is not None
    row = clip(after)
    assert row["needs_precheck"] is True and row["approved_at"] is None and row["reject_reason"] is None
    assert row["gate_result"]["codes"] == ["precheck_required"] and row["gate_result"]["risk"] == ["music"]
    assert clip_events(after, "clip.precheck_pending")[0]["data"]["verdict"] == "hold"
    queued = db.fetch_one("select queue, payload from jobs where type = 'clip.precheck'")
    assert queued == {"queue": "normal", "payload": {"clip_id": after}}
    # Held, not approved: not uploaded to Storage and invisible to posting.
    assert kit.storage.objects == {} and count("clips", "status = 'approved'") == 0


def test_a_passed_precheck_approves_the_clip_and_deletes_the_private_copy(good_video: Path) -> None:
    kit = Kit(oauth=True)
    clip_id = _held(kit, good_video)

    handle_precheck(_job("clip.precheck"))
    upload = kit.youtube.calls_to("upload_private")
    assert len(upload) == 1 and upload[0]["path"] == good_video and clip_id in upload[0]["title"]
    row = clip(clip_id)
    assert row["status"] == "precheck_pending" and row["precheck_video_id"] == "fakevid1"
    assert len(clip_events(clip_id, "clip.precheck_uploaded")) == 1
    result_job = db.fetch_one("select run_after, payload from jobs where type = 'clip.precheck_result'")
    assert result_job is not None and result_job["payload"] == {"clip_id": clip_id, "check": 1}
    wait = result_job["run_after"] - datetime.now(UTC)
    assert timedelta(seconds=1700) < wait < timedelta(seconds=1810)  # precheck.wait_s = 1800

    handle_precheck_result(_job("clip.precheck_result"))
    row = clip(clip_id)
    assert (row["status"], row["lane"], row["reject_reason"]) == ("approved", "hourly", None)
    assert row["approved_at"] is not None and float(row["rank_score"]) == 9.0
    assert row["precheck_result"]["outcome"] == "passed" and row["precheck_result"]["deleted"] is True
    assert row["gate_result"]["precheck"] == "passed" and row["gate_result"]["passed"] is True
    assert kit.youtube.uploaded == {} and kit.youtube.calls_to("delete_video") == [{"video_id": "fakevid1"}]
    assert row["storage_path"] in kit.storage.objects
    assert kit.p5_calls == 1  # the stored verdict is used again, P5 is not paid twice
    creator = db.fetch_one("select claims_count from creators where id = %s", (row["creator_id"],))
    assert creator == {"claims_count": 0}


@pytest.mark.parametrize(
    ("status_changes", "gone"),
    [
        ({"upload_status": "rejected", "rejection_reason": "copyright"}, False),
        ({"region_blocked": ("DE", "FR")}, False),
        ({}, True),
    ],
)
def test_a_claim_rejects_the_clip_and_counts_against_the_creator(
    good_video: Path, status_changes: dict[str, Any], gone: bool
) -> None:
    kit = Kit(oauth=True)
    clip_id = _held(kit, good_video)
    handle_precheck(_job("clip.precheck"))
    if gone:
        kit.youtube.uploaded.clear()
    else:
        processed = kit.youtube.get_status("fakevid1")
        assert processed is not None
        kit.youtube.statuses["fakevid1"] = replace(processed, **status_changes)
    handle_precheck_result(_job("clip.precheck_result"))

    row = clip(clip_id)
    assert (row["status"], row["reject_reason"]) == ("rejected", "precheck_claim")
    assert row["approved_at"] is None and row["storage_path"] is None
    assert row["precheck_result"]["outcome"] == "claimed"
    creator = db.fetch_one("select claims_count from creators where id = %s", (row["creator_id"],))
    assert creator == {"claims_count": 1}
    assert clip_events(clip_id, "clip.precheck_result")[0]["level"] == "warn"
    assert clip_events(clip_id, "clip.rejected")[0]["data"]["codes"] == ["precheck_claim"]
    assert count("notify_outbox") == 0
    if not gone:
        assert kit.youtube.calls_to("delete_video") == [{"video_id": "fakevid1"}]


def test_a_video_that_keeps_processing_is_read_again_then_rejected_as_inconclusive(good_video: Path) -> None:
    kit = Kit(oauth=True)
    clip_id = _held(kit, good_video)
    handle_precheck(_job("clip.precheck"))
    processed = kit.youtube.get_status("fakevid1")
    assert processed is not None
    kit.youtube.statuses["fakevid1"] = replace(processed, upload_status="uploaded")

    handle_precheck_result(_job("clip.precheck_result"))
    assert clip(clip_id)["status"] == "precheck_pending"
    second = db.fetch_one(
        "select payload from jobs where type = 'clip.precheck_result' and status = 'queued'"
    )
    assert second == {"payload": {"clip_id": clip_id, "check": 2}}
    handle_precheck_result(_job("clip.precheck_result"))
    handle_precheck_result(_job("clip.precheck_result"))  # check 3 of precheck.max_status_checks = 3

    row = clip(clip_id)
    assert (row["status"], row["reject_reason"]) == ("rejected", "precheck_inconclusive")
    assert kit.youtube.uploaded == {}  # the copy is deleted all the same
    creator = db.fetch_one("select claims_count from creators where id = %s", (row["creator_id"],))
    assert creator == {"claims_count": 0}


def test_a_spoiler_block_that_started_meanwhile_still_applies_after_a_passed_precheck(
    good_video: Path,
) -> None:
    """Every rule is applied again when the pre-check result arrives."""
    kit = Kit(oauth=True)
    clip_id = _held(kit, good_video)
    handle_precheck(_job("clip.precheck"))
    db.execute("update clips set spoiler = true where id = %s", (clip_id,))
    handle_precheck_result(_job("clip.precheck_result"))
    assert clip(clip_id)["reject_reason"] == "spoiler_blocked"


def test_if_the_token_disappears_before_the_upload_the_strict_rule_decides(good_video: Path) -> None:
    kit = Kit(oauth=True)
    with_music = _held(kit, good_video)
    kit.youtube.oauth = False
    handle_precheck(_job("clip.precheck"))
    row = clip(with_music)
    assert (row["status"], row["reject_reason"]) == ("rejected", "music_strict")
    assert kit.youtube.calls_to("upload_private") == []


def test_a_first_clip_without_music_is_approved_when_the_precheck_became_impossible(good_video: Path) -> None:
    kit = Kit(oauth=True)
    clip_id = _held(kit, good_video, tier="auto", music_ratio=0.0)
    assert clip(clip_id)["gate_result"]["risk"] == ["first_clip_of_auto_creator"]
    settings.set("precheck.enabled", False)
    handle_precheck(_job("clip.precheck"))
    row = clip(clip_id)
    assert row["status"] == "approved" and row["gate_result"]["precheck"] == "disabled"


def test_the_daily_maximum_stops_further_prechecks(good_video: Path) -> None:
    settings.set("precheck.daily_max", 1)
    kit = Kit(oauth=True)
    first = _held(kit, good_video)
    handle_precheck(_job("clip.precheck"))
    assert precheck_state.uploads_today(datetime.now(UTC)) == 1
    assert precheck_state.current_state(kit.youtube, datetime.now(UTC)) is PrecheckState.QUOTA_USED
    # The next risky clip is not held any more: the strict rule decides at once.
    second = add_clip(add_creator(handle="second"), good_video, music_ratio=0.05, **distinct(1))
    assert gate.run_gate(second).status == "rejected"
    assert clip(second)["reject_reason"] == "music_strict"
    assert clip(second)["gate_result"]["precheck"] == "quota_used"
    assert clip(first)["status"] == "precheck_pending"


def test_an_upload_that_keeps_failing_falls_back_to_the_strict_rule(good_video: Path) -> None:
    kit = Kit(oauth=True)
    clip_id = _held(kit, good_video)

    def failing(path: Path, title: str, description: str) -> str:
        raise ApiError("youtube", "POST /upload/youtube/v3/videos", 500, "backend error")

    kit.youtube.upload_private = failing  # type: ignore[method-assign]
    with pytest.raises(ApiError):
        handle_precheck(_job("clip.precheck", attempts=1))
    assert clip(clip_id)["status"] == "precheck_pending"
    db.execute("update jobs set status = 'queued' where type = 'clip.precheck'")
    handle_precheck(_job("clip.precheck", attempts=3))
    assert (clip(clip_id)["status"], clip(clip_id)["reject_reason"]) == ("rejected", "music_strict")


def test_a_status_that_cannot_be_read_ends_inconclusive(good_video: Path) -> None:
    kit = Kit(oauth=True)
    clip_id = _held(kit, good_video)
    handle_precheck(_job("clip.precheck"))

    def failing(video_id: str) -> None:
        raise ApiError("youtube", "GET /youtube/v3/videos", 500, "backend error")

    kit.youtube.get_status = failing  # type: ignore[method-assign, assignment]
    with pytest.raises(ApiError):
        handle_precheck_result(_job("clip.precheck_result", attempts=1))
    db.execute("update jobs set status = 'queued' where type = 'clip.precheck_result'")
    handle_precheck_result(_job("clip.precheck_result", attempts=3))
    assert clip(clip_id)["reject_reason"] == "precheck_inconclusive"


def test_a_failed_delete_does_not_change_the_verdict(good_video: Path) -> None:
    kit = Kit(oauth=True)
    clip_id = _held(kit, good_video)
    handle_precheck(_job("clip.precheck"))

    def failing(video_id: str) -> bool:
        raise ApiError("youtube", "DELETE /youtube/v3/videos", 403, "forbidden")

    kit.youtube.delete_video = failing  # type: ignore[method-assign]
    handle_precheck_result(_job("clip.precheck_result"))
    row = clip(clip_id)
    assert row["status"] == "approved" and row["precheck_result"]["deleted"] is False
    assert len(clip_events(clip_id, "clip.precheck_delete_failed")) == 1


def test_the_handlers_ignore_a_clip_that_is_not_waiting(good_video: Path) -> None:
    kit = Kit(oauth=True)
    gate.configure(kit.deps)
    clip_id = add_clip(add_creator(), good_video)
    assert gate.run_gate(clip_id).status == "approved"
    jobs.enqueue("normal", "clip.precheck", {"clip_id": clip_id})
    handle_precheck(_job("clip.precheck"))
    jobs.enqueue("normal", "clip.precheck_result", {"clip_id": clip_id, "check": 1})
    handle_precheck_result(_job("clip.precheck_result"))
    assert clip(clip_id)["status"] == "approved" and kit.youtube.calls == []


def test_a_second_precheck_job_does_not_upload_twice(good_video: Path) -> None:
    kit = Kit(oauth=True)
    clip_id = _held(kit, good_video)
    handle_precheck(_job("clip.precheck"))
    jobs.enqueue("normal", "clip.precheck", {"clip_id": clip_id})
    handle_precheck(_job("clip.precheck"))
    assert len(kit.youtube.calls_to("upload_private")) == 1


def test_an_express_clip_demoted_to_hourly_is_prechecked_when_risky(good_video: Path) -> None:
    kit = Kit(oauth=True)
    gate.configure(kit.deps)
    creator_id = add_creator("auto")  # first clip of an auto creator
    clip_id = add_clip(creator_id, good_video, lane="spike", score=7, spike_id=add_spike(creator_id))
    assert gate.run_gate(clip_id).status == "precheck_pending"
    row = clip(clip_id)
    assert (row["lane"], row["gate_profile"]) == ("hourly", "hourly") and row["gate_result"][
        "demoted"
    ] is True
    handle_precheck(_job("clip.precheck"))
    handle_precheck_result(_job("clip.precheck_result"))
    row = clip(clip_id)
    assert (row["status"], row["lane"]) == ("approved", "hourly") and count("posts") == 0
    assert (
        float(row["rank_score"]) == pytest.approx(demoted_rank(7.0)) and row["gate_result"]["demoted"] is True
    )
    assert row["gate_result"]["express"]["codes"] == ["score_too_low"]
    assert youtube_precheck.JOB_PRECHECK == "clip.precheck"

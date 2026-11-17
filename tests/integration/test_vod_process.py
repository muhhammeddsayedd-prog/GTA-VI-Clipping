"""The VOD lane against Postgres with fakes for chat, yt-dlp and Claude: one `vod.process` job from a fixture
chat to render jobs, retries and duplicates, the download-blocked state, budgets, the emergency stop, the
transcript fallback, the replay pass after a live stream and the clean-up job."""

import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from sixstars import config, db, jobs, settings, usage
from sixstars.ai import calls
from sixstars.ai.calls import BudgetExceededError
from sixstars.chat import synthetic, twitch_replay
from sixstars.chat.models import ReplayChat
from sixstars.clients.anthropic_client import InvalidModelOutput
from sixstars.clients.fakes import FakeAnthropicClient
from sixstars.factory import tools
from sixstars.factory.pipeline import RenderInput
from sixstars.vod import cleanup, health, process, sections, store
from sixstars.vod.common import Deps
from sixstars.vod.config import DEFAULTS, VodConfig

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BOT_CHECK = (
    "ERROR: [youtube] vodtest0001: Sign in to confirm you’re not a bot. Use --cookies-from-browser or "
    "--cookies for the authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-"
    "cookies-to-yt-dlp  for how to manually pass cookies."
)
MOMENT: dict[str, Any] = {
    "is_moment": True,
    "confidence": 0.9,
    "moment_type": "funny",
    "one_line": "Streamer drives into the canal",
    "noise_reason": None,
}
NOISE: dict[str, Any] = {
    "is_moment": False,
    "confidence": 0.05,
    "moment_type": "other",
    "one_line": "raid from another channel",
    "noise_reason": "raid",
}
NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
STATUS_EVENTS = (
    "video.processing",
    "video.done",
    "video.failed",
    "video.skipped",
    "video.deferred",
    "video.blocked",
    "video.retry",
)


class ScriptedClaude(FakeAnthropicClient):
    """Answers P1 for whatever spike ids it is asked about, and P3 with fixed moments."""

    def __init__(
        self, verdicts: Sequence[dict[str, Any]] = (MOMENT,), moments: Sequence[dict[str, Any]] = ()
    ):
        super().__init__()
        self.verdicts = list(verdicts)
        self.moments = list(moments)
        self.error: Exception | None = None
        self.p3_calls = 0
        self.invalid_p3_calls: set[int] = set()

    def complete_json[T: BaseModel](  # type: ignore[override]
        self, prompt_id: str, system: str, user_content: Any, schema: type[T], *args: Any, **kwargs: Any
    ) -> T:
        self._record("complete_json", prompt_id=prompt_id, user_content=user_content)
        if self.error is not None:
            raise self.error
        if prompt_id == calls.P3:
            self.p3_calls += 1
            if self.p3_calls in self.invalid_p3_calls:
                raise InvalidModelOutput("p3_vod_moment_picker: reply is not valid for Bound")
            covers = re.search(r"covers (\d+) s to (\d+) s", str(user_content))
            assert covers is not None
            low, high = float(covers.group(1)), float(covers.group(2))
            inside = [m for m in self.moments if m["start_s"] >= low and m["end_s"] <= high]
            return schema.model_validate({"moments": inside})
        ids = re.findall(r"### spike_id: (\S+)", str(user_content))
        answers = [
            {**self.verdicts[min(index, len(self.verdicts) - 1)], "spike_id": spike_id}
            for index, spike_id in enumerate(ids)
        ]
        if len(ids) == 1:
            return schema.model_validate({k: v for k, v in answers[0].items() if k != "spike_id"})
        return schema.model_validate({"results": answers})

    def asked(self, prompt_id: str) -> int:
        return sum(1 for _, kwargs in self.calls if kwargs["prompt_id"] == prompt_id)


class FakeYtDlp:
    """Stands in for the yt-dlp subprocess: writes the file a real run would write."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.section_results: list[tuple[int, str, str]] = []  # served first, then success
        self.captions: dict[str, Any] | None = None
        self.caption_result: tuple[int, str, str] | None = None

    def __call__(self, command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        self.commands.append(list(command))
        template = command[command.index("--output") + 1] if "--output" in command else ""
        if "--download-sections" in command:
            if self.section_results:
                return self.section_results.pop(0)
            Path(template.replace("%(ext)s", "mp4")).write_bytes(b"section")
            return 0, "", ""
        if "--write-subs" in command:
            if self.caption_result is not None:
                return self.caption_result
            if self.captions is not None:
                target = Path(template.replace("%(ext)s", "en-orig.json3"))
                target.write_text(json.dumps(self.captions), encoding="utf-8")
            return 0, "", ""
        raise AssertionError(f"unexpected yt-dlp call: {command[-3:]}")

    def count(self, flag: str) -> int:
        return sum(1 for command in self.commands if flag in command)

    def sections_asked(self) -> list[str]:
        return [c[c.index("--download-sections") + 1] for c in self.commands if "--download-sections" in c]


def youtube_chat() -> ReplayChat:
    """Three hours of calm chat with two reaction bursts (3000 s and 6000 s) and one in the last minutes."""
    messages = synthetic.merge(
        synthetic.flat_chat(9000, 2, platform="youtube", seed=5),
        synthetic.burst(3000, 40, 30, platform="youtube", seed=7),
        synthetic.burst(6000, 30, 25, platform="youtube", seed=8),
        synthetic.burst(8900, 30, 25, platform="youtube", seed=9),
    )
    return ReplayChat("youtube", "vodtest0001", messages, duration_s=9100.0, title="GTA RP night")


class Lane:
    """One test's world: a creator, fakes and helpers."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.claude = ScriptedClaude()
        self.ytdlp = FakeYtDlp()
        self.chat: ReplayChat | None = youtube_chat()
        self.chat_calls = 0
        self.now = NOW
        self.sleeps: list[float] = []
        row = db.fetch_one(
            "insert into creators (platform, channel_id, handle) "
            "values ('youtube', 'UCvodtest', 'vodtester') returning id::text as id"
        )
        assert row is not None
        self.creator_id = str(row["id"])

    def deps(self) -> Deps:
        def download(platform: str, source_id: str, raw: Path | None) -> ReplayChat | None:
            self.chat_calls += 1
            return self.chat

        return Deps(
            chat_downloader=download,
            ai_client=self.claude,
            runner=self.ytdlp,
            sleep=self.sleeps.append,
            now=lambda: self.now,
        )

    def video(self, source_id: str = "vodtest0001", **columns: Any) -> str:
        values = {"has_chat": True, "duration_s": 9100, "title": "GTA RP night", **columns}
        return store.ensure_video(
            creator_id=self.creator_id,
            platform=str(values.pop("platform", "youtube")),
            source_id=source_id,
            url=f"https://www.youtube.com/watch?v={source_id}",
            title=values["title"],
            duration_s=values["duration_s"],
            has_chat=values["has_chat"],
            live_session_id=values.get("live_session_id"),
        )

    def enqueue(self, video_id: str, job_type: str = "vod.process", **payload: Any) -> None:
        jobs.enqueue(
            "normal",
            job_type,
            {"video_id": video_id, **payload},
            priority=90,
            dedupe_key=f"vod:{video_id}",
        )

    def run_next(self) -> process.Outcome:
        """Claim the next due job and run it the way the worker does."""
        job = jobs.claim("normal", "test-worker")
        assert job is not None, "no runnable job"
        assert job.type in ("vod.process", "replay.scan")
        try:
            outcome = process.handle(job, self.deps())
        except Exception as exc:
            jobs.fail(job.id, f"{type(exc).__name__}: {exc}")
            raise
        jobs.complete(job.id)
        return outcome

    def cleanup(self) -> None:
        db.execute("delete from clips where creator_id = %s", (self.creator_id,))
        db.execute("update spikes set merged_into = null where creator_id = %s", (self.creator_id,))
        db.execute("delete from spikes where creator_id = %s", (self.creator_id,))
        db.execute(
            "delete from chat_files where video_id in (select id from videos where creator_id = %s)",
            (self.creator_id,),
        )
        db.execute("delete from videos where creator_id = %s", (self.creator_id,))
        db.execute("delete from live_sessions where creator_id = %s", (self.creator_id,))
        db.execute("delete from creators where id = %s", (self.creator_id,))


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Lane]:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.env.cache_clear()

    def probe(path: Path, ffprobe: str = "ffprobe") -> tools.MediaInfo:
        if not path.is_file() or path.read_bytes() != b"section":
            raise tools.FfmpegError("not a video")
        return tools.MediaInfo(width=1920, height=1080, duration_s=65.0, has_video=True, has_audio=True)

    monkeypatch.setattr(sections.tools, "probe", probe)
    # Migration 0010 raised the default to 3 (the fallback also tops up a thin chat). These tests each
    # exercise one path, so they keep the M08 value; the default has its own test below.
    settings.set("vod.fallback_below_spikes", 1)
    world = Lane(tmp_path)
    yield world
    world.cleanup()


def video_row(video_id: str) -> dict[str, Any]:
    row = db.fetch_one("select * from videos where id = %s", (video_id,))
    assert row is not None
    return row


def spikes(video_id: str) -> list[dict[str, Any]]:
    return db.fetch_all("select * from spikes where video_id = %s order by spike_offset_s", (video_id,))


def render_jobs() -> list[dict[str, Any]]:
    return db.fetch_all("select * from jobs where type = 'clip.render' order by id")


def claim_type(job_type: str) -> jobs.Job:
    """Claim the oldest queued job of one type (the queue itself would hand out render jobs first)."""
    row = db.fetch_one(
        """
        update jobs set status = 'running', attempts = attempts + 1
        where id = (select id from jobs where type = %s and status = 'queued' order by id limit 1)
        returning *
        """,
        (job_type,),
    )
    assert row is not None, f"no queued {job_type} job"
    return jobs.Job.from_row(row)


def make_due(job_type: str = "vod.process") -> None:
    db.execute("update jobs set run_after = now() where type = %s and status = 'queued'", (job_type,))


def age_block_markers(interval: str) -> None:
    """Pretend the last refusal (and its notification) happened `interval` ago."""
    db.execute(
        "update events set created_at = now() - %s::interval where type like 'vod.download_block%%'",
        (interval,),
    )
    db.execute("update notify_outbox set created_at = now() - %s::interval", (interval,))


def queued(job_type: str) -> list[dict[str, Any]]:
    return db.fetch_all("select * from jobs where type = %s and status = 'queued' order by id", (job_type,))


def video_events(video_id: str) -> list[str]:
    rows = db.fetch_all(
        "select type from events where entity_type = 'video' and entity_id = %s and type = any(%s) "
        "order by id",
        (video_id, list(STATUS_EVENTS)),
    )
    return [str(row["type"]) for row in rows]


# ── the happy path ───────────────────────────────────────────


def test_settings_of_the_migration_are_in_the_table() -> None:
    cfg = VodConfig.load()
    assert all(settings.get(key) == value for key, value in DEFAULTS.items())
    assert cfg.end_guard_s == settings.get_float("spikes.end_guard_s") == 300
    assert cfg.max_height == 1080 and cfg.max_spikes_per_video == 25


def test_a_job_with_a_fixture_chat_makes_spikes_and_render_jobs(lane: Lane) -> None:
    video_id = lane.video()
    lane.enqueue(video_id, creator_id=lane.creator_id, platform="youtube", source_id="vodtest0001")

    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.source == "chat"
    assert (outcome.spikes, outcome.confirmed, outcome.downloaded, outcome.queued) == (2, 2, 2, 2)
    assert outcome.messages == 1900
    video = video_row(video_id)
    assert video["status"] == "done" and video["attempts"] == 1 and video["skip_reason"] is None
    assert video_events(video_id) == ["video.processing", "video.done"]

    stored = spikes(video_id)
    # The burst in the last five minutes is left out: spikes.end_guard_s is read and passed on.
    assert [float(row["spike_offset_s"]) for row in stored] == [3000.0, 6000.0]
    assert all(row["mode"] == "replay" and row["status"] == "confirmed" for row in stored)
    assert all(float(row["claude_confidence"]) == 0.9 for row in stored)

    chat_file = db.fetch_one("select * from chat_files where video_id = %s", (video_id,))
    assert chat_file is not None and chat_file["messages_count"] == 1900 and chat_file["duration_s"] == 9100
    assert Path(chat_file["storage_path"]).is_file()

    # Only the padded windows are downloaded, in video seconds, never the whole video.
    assert sorted(lane.ytdlp.sections_asked()) == ["*2965.000-3030.000", "*5965.000-6030.000"]
    assert all("--download-sections" in c for c in lane.ytdlp.commands if "--skip-download" not in c)
    selector = next(c[c.index("-f") + 1] for c in lane.ytdlp.commands if "-f" in c)
    assert "[height<=1080]" in selector

    made = render_jobs()
    assert len(made) == 2 and {job["queue"] for job in made} == {"normal"}
    by_spike = {job["payload"]["spike_id"]: job for job in made}
    first = by_spike[str(stored[0]["id"])]
    assert first["priority"] == 55 and first["dedupe_key"] == f"clip.render:{stored[0]['id']}"
    payload = first["payload"]
    assert payload["source_path"] == str(store.sections_dir(video_id) / f"{stored[0]['id']}.mp4")
    assert Path(payload["source_path"]).is_file()
    # Positions are seconds in the downloaded file, which starts at the padded window start (2965 s).
    assert payload["source_offset_s"] == 2965.0
    assert (payload["window_start_s"], payload["window_end_s"]) == (15.0, 50.0)
    assert payload["spike_floor_s"] == 35.0 and (payload["pad_start_s"], payload["pad_end_s"]) == (0.0, 65.0)
    assert payload["creator_id"] == lane.creator_id and payload["creator"] == "vodtester"
    assert payload["lane"] == "hourly" and payload["mode"] == "normal" and payload["source_type"] == "vod"
    assert payload["platform"] == "youtube" and payload["source_id"] == "vodtest0001"
    assert payload["stream_title"] == "GTA RP night" and payload["upload"] is False
    assert payload["verdict"]["one_line"] == "Streamer drives into the canal"
    assert payload["chat"] and all("t_s" in line for line in payload["chat"])
    item = RenderInput.from_payload(payload)  # the factory accepts it as it is
    assert item.spike_id == str(stored[0]["id"]) and item.window_end_s == 50.0

    # Scratch is gone, the windows wait for their renders, and a clean-up job watches them.
    assert not store.work_dir(video_id).exists()
    assert len(queued("vod.cleanup")) == 1
    assert queued("vod.cleanup")[0]["payload"] == {"video_id": video_id}


def test_a_replay_s_tier_is_routed_to_the_hourly_lane_and_b_gets_a_lower_priority(lane: Lane) -> None:
    video_id = lane.video()
    lane.enqueue(video_id)
    lane.run_next()
    assert {row["tier"] for row in spikes(video_id)} == {"S"}  # confidence 0.9 keeps the S
    assert {job["payload"]["lane"] for job in render_jobs()} == {"hourly"}
    assert {job["priority"] for job in render_jobs()} == {55}

    other = lane.video("vodtest0002")
    lane.claude.verdicts = [{**MOMENT, "confidence": 0.65}]  # below spikes.s_min_confidence: S becomes A
    settings.set("vod.render_priority", {"S": 55, "A": 41, "B": 40})
    lane.enqueue(other)
    lane.run_next()
    assert {row["tier"] for row in spikes(other)} == {"A"}
    assert {job["priority"] for job in render_jobs() if job["payload"]["source_id"] == "vodtest0002"} == {41}


def test_rejected_spikes_are_not_downloaded(lane: Lane) -> None:
    lane.claude.verdicts = [MOMENT, NOISE]
    settings.set("vod.fallback_enabled", False)
    video_id = lane.video()
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert (outcome.spikes, outcome.confirmed, outcome.queued) == (2, 1, 1)
    assert sorted(row["status"] for row in spikes(video_id)) == ["confirmed", "rejected"]
    assert len(lane.ytdlp.sections_asked()) == 1


def test_max_spikes_per_video_keeps_the_best(lane: Lane) -> None:
    settings.set("vod.max_spikes_per_video", 1)
    video_id = lane.video()
    lane.enqueue(video_id)
    assert lane.run_next().spikes == 1
    assert [float(row["spike_offset_s"]) for row in spikes(video_id)] == [3000.0]  # the higher score


def test_the_caption_track_is_shown_to_the_confirmation(lane: Lane) -> None:
    lane.ytdlp.captions = {
        "events": [{"tStartMs": 2990000, "dDurationMs": 4000, "segs": [{"utf8": "oh no the canal"}]}]
    }
    video_id = lane.video()
    lane.enqueue(video_id)
    lane.run_next()
    prompt = str(lane.claude.calls[0][1]["user_content"])
    assert "Transcript of the window: oh no the canal" in prompt
    assert lane.ytdlp.count("--write-subs") == 1


def test_a_twitch_vod_goes_through_the_same_steps(lane: Lane) -> None:
    replay = twitch_replay.parse_file(FIXTURES / "chat" / "twitch_replay_sample.json")
    lane.chat = replay
    settings.set("spikes.end_guard_s", 0)  # the fixture ends right after its burst
    db.execute("update creators set platform = 'twitch' where id = %s", (lane.creator_id,))
    video_id = store.ensure_video(
        creator_id=lane.creator_id,
        platform="twitch",
        source_id="2900000001",
        url="https://www.twitch.tv/videos/2900000001",
        title=None,
        duration_s=None,
        has_chat=True,
    )
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.queued == 1
    video = video_row(video_id)
    assert video["title"] == "GTA V roleplay stream" and video["duration_s"] == 42450  # from the chat file
    assert lane.ytdlp.count("--write-subs") == 0  # Twitch has no caption track
    command = next(c for c in lane.ytdlp.commands if "--download-sections" in c)
    assert command[-1] == "https://www.twitch.tv/videos/2900000001"
    assert command[command.index("--download-sections") + 1] == "*35665.000-35755.000"
    payload = render_jobs()[0]["payload"]
    assert payload["platform"] == "twitch" and payload["source_offset_s"] == 35665.0


# ── retries and duplicates ───────────────────────────────────


def test_a_duplicated_job_changes_nothing(lane: Lane) -> None:
    video_id = lane.video()
    lane.enqueue(video_id)
    lane.run_next()
    before = (
        len(spikes(video_id)),
        len(render_jobs()),
        lane.claude.asked(calls.P1),
        len(lane.ytdlp.commands),
    )

    lane.enqueue(video_id)
    outcome = lane.run_next()

    assert outcome.status == "ignored" and "already done" in outcome.reason
    after = (len(spikes(video_id)), len(render_jobs()), lane.claude.asked(calls.P1), len(lane.ytdlp.commands))
    assert after == before


def test_a_retried_job_resumes_without_duplicates(lane: Lane) -> None:
    video_id = lane.video()
    lane.enqueue(video_id)
    settings.set("vod.download_workers", 1)
    settings.set("vod.download_failures_max", 1)
    # The first window comes through, the second fails with a passing network error.
    original = lane.ytdlp.__call__
    seen = {"sections": 0}

    def flaky(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        if "--download-sections" in command:
            seen["sections"] += 1
            if seen["sections"] == 2:
                lane.ytdlp.commands.append(list(command))
                return 1, "", "ERROR: unable to download video data: Connection reset by peer"
        return original(command, timeout_s)

    first_deps = lane.deps()
    first_deps.runner = flaky
    job = jobs.claim("normal", "test-worker")
    assert job is not None
    with pytest.raises(process.SectionsFailedError, match="1 window download"):
        process.handle(job, first_deps)
    assert jobs.fail(job.id, "SectionsFailedError") == "queued"

    video = video_row(video_id)
    assert video["status"] == "new" and video["skip_reason"].startswith("retry: SectionsFailedError")
    assert len(spikes(video_id)) == 2 and len(render_jobs()) == 1
    assert lane.claude.asked(calls.P1) == 1 and lane.chat_calls == 1

    make_due()
    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.queued == 1
    assert len(spikes(video_id)) == 2  # the same two spikes
    assert len(render_jobs()) == 2 and len({job["payload"]["spike_id"] for job in render_jobs()}) == 2
    assert lane.claude.asked(calls.P1) == 1  # verdicts were kept
    assert lane.chat_calls == 1  # the chat file was kept
    assert seen["sections"] == 2 and lane.ytdlp.count("--download-sections") == 3
    assert video_row(video_id)["attempts"] == 2
    assert video_events(video_id) == ["video.processing", "video.retry", "video.processing", "video.done"]


def test_the_last_attempt_closes_the_video_and_tells_the_owner(lane: Lane) -> None:
    video_id = lane.video()
    lane.ytdlp.section_results = [(1, "", "ERROR: unable to download video data: Connection reset")] * 9
    jobs.enqueue(
        "normal", "vod.process", {"video_id": video_id}, dedupe_key=f"vod:{video_id}", max_attempts=1
    )

    with pytest.raises(process.SectionsFailedError):
        lane.run_next()

    video = video_row(video_id)
    assert video["status"] == "failed" and "SectionsFailedError" in video["skip_reason"]
    assert {row["status"] for row in spikes(video_id)} == {"failed"}
    assert video_events(video_id) == ["video.processing", "video.failed"]
    outbox = db.fetch_all("select severity, title from notify_outbox")
    assert outbox == [{"severity": "warn", "title": process.FAILED_TITLE}]
    assert not store.video_dir(video_id).exists()  # nothing is left on disk


def test_a_video_that_is_gone_fails_at_once(lane: Lane) -> None:
    video_id = lane.video()
    lane.ytdlp.section_results = [(1, "", "ERROR: [youtube] vodtest0001: Private video. Sign in if")] * 3
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert outcome.status == "failed" and outcome.kind == "unavailable"
    assert video_row(video_id)["status"] == "failed"
    assert queued("vod.process") == []


# ── downloads blocked ────────────────────────────────────────


def test_a_bot_check_defers_the_video_and_notifies_once(lane: Lane) -> None:
    first, second = lane.video(), lane.video("vodtest0002")
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 20
    settings.set("vod.confirm_with_captions", False)
    lane.enqueue(first)

    outcome = lane.run_next()

    assert outcome.status == "deferred" and outcome.kind == "download_blocked"
    assert outcome.run_after == NOW + timedelta(seconds=1800)
    video = video_row(first)
    assert video["status"] == "new"  # waiting, not failed
    assert video["skip_reason"].startswith(
        "download_blocked: ERROR: [youtube] vodtest0001: Sign in to confirm"
    )
    assert video_events(first) == ["video.processing", "video.blocked"]
    # The same work is queued again for later as a fresh job: no attempt is used up.
    again = queued("vod.process")
    assert len(again) == 1 and again[0]["attempts"] == 0 and again[0]["dedupe_key"] == f"vod:{first}:again"
    assert again[0]["run_after"] == NOW + timedelta(seconds=1800)
    assert again[0]["payload"] == {"video_id": first}
    assert render_jobs() == [] and {row["status"] for row in spikes(first)} == {"confirmed"}
    outbox = db.fetch_all("select severity, title, body from notify_outbox")
    assert (
        len(outbox) == 1 and outbox[0]["severity"] == "warn" and outbox[0]["title"] == process.BLOCKED_TITLE
    )
    assert "PROXY_URL" in outbox[0]["body"] and "not a bot" in outbox[0]["body"]
    assert "1 video(s) are waiting" not in outbox[0]["body"] and "2 video(s) are waiting" in outbox[0]["body"]
    # The block is on record for discovery, which then queues no new YouTube videos.
    record = settings.get(health.STATE_KEY)["youtube"]
    assert (record["blocks"], record["since"], record["last_blocked_at"]) == (
        1,
        NOW.isoformat(),
        NOW.isoformat(),
    )
    assert health.blocked(health.load(), NOW, 7200) == {"youtube": NOW}

    # A second video of the same platform does not even try: no chat, no AI, no download.
    asked_before = (lane.claude.asked(calls.P1), len(lane.ytdlp.commands), lane.chat_calls)
    lane.enqueue(second)
    outcome = lane.run_next()
    assert outcome.status == "deferred" and outcome.kind == "download_blocked"
    assert (lane.claude.asked(calls.P1), len(lane.ytdlp.commands), lane.chat_calls) == asked_before
    assert video_row(second)["status"] == "new" and video_row(second)["attempts"] == 0
    assert len(db.fetch_all("select 1 from notify_outbox")) == 1  # still one message


def test_a_blocked_backlog_drains_once_downloads_work(lane: Lane) -> None:
    video_id = lane.video()
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 2
    lane.enqueue(video_id)
    assert lane.run_next().status == "deferred"

    # Half an hour later the retry is still refused: no second message within the hour, still not failed.
    age_block_markers("31 minutes")
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 2
    make_due()
    assert lane.run_next().status == "deferred"
    assert video_row(video_id)["status"] == "new"
    assert settings.get(health.STATE_KEY)["youtube"]["blocks"] == 2
    assert len(db.fetch_all("select 1 from notify_outbox")) == 1
    assert [job["dedupe_key"] for job in queued("vod.process")] == [f"vod:{video_id}"]  # the key flipped back

    # A proxy is added; the next retry after the back-off goes through.
    age_block_markers("2 hours")
    lane.ytdlp.section_results = []
    make_due()
    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.queued == 2
    assert video_row(video_id)["status"] == "done" and video_row(video_id)["skip_reason"] is None
    assert settings.get(health.STATE_KEY) == {}  # a window came through: the record of the block is gone
    assert lane.claude.asked(calls.P1) == 1  # the verdicts of the first attempt were kept
    assert "video.failed" not in video_events(video_id)


def test_a_proxy_or_cookies_added_after_a_refusal_let_the_next_video_start_at_once(
    lane: Lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate only honours a refusal recorded under the access this process has. After the owner mounts
    the cookies file (or sets the proxy) and restarts, a new video is tried right away."""
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 2
    first = lane.video()
    lane.enqueue(first)
    assert lane.run_next().status == "deferred"
    event = db.fetch_one("select data from events where type = 'vod.download_blocked' limit 1")
    assert event is not None
    assert event["data"]["access"] == "PROXY_URL not set, YOUTUBE_COOKIES_FILE not set"

    # same access: the next video waits at the gate, nothing is downloaded
    waiting = lane.video(source_id="vodtest0002")
    lane.enqueue(waiting)
    commands_before = len(lane.ytdlp.commands)
    outcome = lane.run_next()
    assert outcome.status == "deferred" and outcome.kind == "download_blocked"
    assert len(lane.ytdlp.commands) == commands_before

    # the cookies file arrives
    cookies = tmp_path / "youtube_cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(cookies))
    config.env.cache_clear()
    make_due()
    outcome = lane.run_next()
    assert outcome.status == "done" and outcome.queued == 2
    assert any("--cookies" in command for command in lane.ytdlp.commands)


def test_a_second_notification_is_sent_after_the_interval(lane: Lane) -> None:
    video_id = lane.video()
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 2
    lane.enqueue(video_id)
    lane.run_next()
    age_block_markers("61 minutes")
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 2
    make_due()
    assert lane.run_next().status == "deferred"
    assert len(db.fetch_all("select 1 from notify_outbox")) == 2


def test_a_bot_check_on_the_caption_request_blocks_before_any_ai_spend(lane: Lane) -> None:
    video_id = lane.video()
    lane.ytdlp.caption_result = (1, "", BOT_CHECK)
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert outcome.status == "deferred" and outcome.kind == "download_blocked"
    assert lane.claude.calls == [] and lane.ytdlp.count("--download-sections") == 0
    assert {row["status"] for row in spikes(video_id)} == {"detected"}


def test_windows_refused_with_403_and_none_through_count_as_a_block(lane: Lane) -> None:
    video_id = lane.video()
    forbidden = (1, "", "[https @ 0x1] HTTP error 403 Forbidden\nERROR: ffmpeg exited with code 1")
    lane.ytdlp.section_results = [forbidden] * 5
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert outcome.status == "deferred" and outcome.kind == "download_blocked"
    assert video_row(video_id)["status"] == "new"


# ── budgets, caps and the emergency stop ─────────────────────


def test_an_exhausted_ai_budget_defers_to_the_next_day(lane: Lane) -> None:
    settings.set("ai.daily_budget_usd", 1)
    usage.record("anthropic", calls.P2, model="claude-sonnet-5-5", cost_usd=1.5)
    video_id = lane.video()
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "deferred" and outcome.kind == "ai_budget"
    assert outcome.run_after is not None and outcome.run_after.date().isoformat() == "2026-11-18"
    video = video_row(video_id)
    assert video["status"] == "new" and video["skip_reason"].startswith("ai_budget")
    again = queued("vod.process")
    assert len(again) == 1 and again[0]["run_after"] == outcome.run_after and again[0]["attempts"] == 0
    assert lane.claude.calls == [] and render_jobs() == []
    assert {row["status"] for row in spikes(video_id)} == {"detected"}  # kept for the next day

    db.execute("truncate api_usage")
    make_due()
    assert lane.run_next().status == "done"
    assert len(spikes(video_id)) == 2 and len(render_jobs()) == 2


def test_a_budget_error_from_the_model_call_also_defers(lane: Lane) -> None:
    lane.claude.error = BudgetExceededError("p1_spike_confirm: ai.daily_budget_usd is used up for today")
    video_id = lane.video()
    lane.enqueue(video_id)
    assert lane.run_next().kind == "ai_budget"
    assert video_row(video_id)["status"] == "new"


def test_the_emergency_stop_queues_no_renders(lane: Lane) -> None:
    settings.set("control.emergency_stop", True)
    video_id = lane.video()
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "deferred" and outcome.kind == "emergency_stop"
    assert outcome.run_after == NOW + timedelta(seconds=600)
    assert render_jobs() == [] and lane.ytdlp.commands == [] and lane.claude.calls == []
    assert video_row(video_id)["status"] == "new" and video_row(video_id)["attempts"] == 0
    assert video_events(video_id) == ["video.deferred"]

    # Still stopped at the next try: the video keeps waiting and no second event is written.
    make_due()
    assert lane.run_next().status == "deferred"
    assert video_events(video_id) == ["video.deferred"]

    settings.set("control.emergency_stop", False)
    make_due()
    assert lane.run_next().status == "done"
    assert len(render_jobs()) == 2


def test_the_emergency_stop_set_during_downloads_stops_enqueuing(lane: Lane) -> None:
    settings.set("vod.download_workers", 1)
    video_id = lane.video()
    lane.enqueue(video_id)
    original = lane.ytdlp.__call__

    def stop_after_first(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        result = original(command, timeout_s)
        if "--download-sections" in command:
            settings.set("control.emergency_stop", True)
        return result

    deps = lane.deps()
    deps.runner = stop_after_first
    job = jobs.claim("normal", "test-worker")
    assert job is not None
    outcome = process.handle(job, deps)
    jobs.complete(job.id)

    assert outcome.status == "deferred" and outcome.kind == "emergency_stop"
    assert render_jobs() == []
    assert len(queued("vod.cleanup")) == 1  # the downloaded window is watched while it waits


def test_a_paused_lane_waits_until_the_given_time(lane: Lane) -> None:
    until = NOW + timedelta(hours=3)
    settings.set("vod.paused_until", until.isoformat())
    video_id = lane.video()
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert outcome.status == "deferred" and outcome.kind == "paused" and outcome.run_after == until
    assert lane.chat_calls == 0


def test_only_max_concurrent_videos_are_in_work(lane: Lane) -> None:
    busy, waiting = lane.video("vodtest0002"), lane.video()
    db.execute("update videos set status = 'processing' where id = %s", (busy,))
    lane.enqueue(waiting)

    outcome = lane.run_next()

    assert outcome.status == "deferred" and outcome.kind == "concurrency"
    assert outcome.run_after == NOW + timedelta(seconds=120)
    assert video_row(waiting)["status"] == "new" and lane.chat_calls == 0

    settings.set("vod.max_concurrent", 2)
    make_due()
    assert lane.run_next().status == "done"


def test_a_video_whose_worker_died_is_taken_over_only_when_stale(lane: Lane) -> None:
    video_id = lane.video()
    db.execute("update videos set status = 'processing' where id = %s", (video_id,))
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert outcome.status == "ignored" and "another worker" in outcome.reason
    assert spikes(video_id) == []

    settings.set("vod.processing_stale_s", 0)
    lane.enqueue(video_id)
    assert lane.run_next().status == "done"


def test_a_creator_who_forbids_clipping_is_skipped(lane: Lane) -> None:
    db.execute("update creators set tier = 'restricted' where id = %s", (lane.creator_id,))
    video_id = lane.video()
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert outcome.status == "skipped"
    video = video_row(video_id)
    assert video["status"] == "skipped" and video["skip_reason"] == "creator tier is restricted"
    assert video_events(video_id) == ["video.skipped"] and lane.chat_calls == 0


def test_a_job_for_an_unknown_video_ends_quietly(lane: Lane) -> None:
    lane.enqueue("00000000-0000-0000-0000-000000000000")
    assert lane.run_next().status == "ignored"
    jobs.enqueue("normal", "vod.process", {}, dedupe_key="vod:none")
    assert lane.run_next().status == "ignored"


# ── transcript fallback ──────────────────────────────────────


def caption_track() -> dict[str, Any]:
    events = [
        {"tStartMs": start * 1000, "dDurationMs": 4000, "segs": [{"utf8": f"line at {start} oh my god"}]}
        for start in range(0, 2400, 5)
    ]
    return {"events": events}


MOMENTS = [
    {"start_s": 600, "end_s": 640, "moment_type": "funny", "one_line": "He falls off", "confidence": 0.85},
    {"start_s": 1200, "end_s": 1230, "moment_type": "fail", "one_line": "Car explodes", "confidence": 0.6},
    {"start_s": 1800, "end_s": 1830, "moment_type": "other", "one_line": "Maybe", "confidence": 0.3},
]


def test_a_video_without_chat_uses_the_caption_track(lane: Lane) -> None:
    lane.chat = None
    lane.ytdlp.captions = caption_track()
    lane.claude.moments = MOMENTS
    video_id = lane.video(duration_s=2400)
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.source == "transcript (captions)"
    assert (outcome.spikes, outcome.confirmed, outcome.queued) == (3, 2, 2)
    assert lane.chat_calls == 2 and lane.sleeps == [20.0]  # no replay: one more try, then the fallback
    stored = spikes(video_id)
    assert [row["mode"] for row in stored] == ["transcript"] * 3
    assert [(row["status"], row["tier"]) for row in stored] == [
        ("confirmed", "A"),
        ("confirmed", "B"),
        ("rejected", "rejected"),
    ]
    assert stored[0]["claude_reason"] == "funny: He falls off" and float(stored[0]["window_end_s"]) == 640.0
    assert stored[0]["parts"]["pad_start"] == 585.0 and stored[0]["parts"]["source"] == "captions"
    # No media was downloaded for the transcript; only the two windows.
    assert sorted(lane.ytdlp.sections_asked()) == ["*1185.000-1245.000", "*585.000-655.000"]
    assert lane.ytdlp.count("--write-subs") == 1
    payload = {job["priority"]: job["payload"] for job in render_jobs()}
    assert set(payload) == {55, 40}
    assert payload[55]["source_offset_s"] == 585.0 and payload[55]["window_start_s"] == 15.0
    assert payload[55]["spike_floor_s"] == 35.0 and payload[55]["verdict"]["moment_type"] == "funny"
    picker = next(kwargs for _, kwargs in lane.claude.calls if kwargs["prompt_id"] == calls.P3)
    assert "[600-630] line at 600 oh my god" in str(picker["user_content"])

    fallback_events = db.fetch_all("select data from events where type = 'video.fallback_picked'")
    assert fallback_events[0]["data"] == {
        "source": "captions", "transcript_s": 2400.0, "transcribed_s": 0.0, "moments": 3, "kept": 2,
    }  # fmt: skip


def test_the_fallback_is_paid_for_once(lane: Lane) -> None:
    lane.chat = None
    lane.ytdlp.captions = caption_track()
    lane.claude.moments = MOMENTS
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 2
    video_id = lane.video(duration_s=2400)
    lane.enqueue(video_id)
    assert lane.run_next().status == "deferred"
    asked = lane.claude.asked(calls.P3)
    assert asked == 2  # one request per 30 minutes of transcript

    age_block_markers("2 hours")
    make_due()
    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.queued == 2
    assert lane.claude.asked(calls.P3) == asked and len(spikes(video_id)) == 3


def test_one_invalid_answer_of_the_picker_does_not_lose_the_other_parts(lane: Lane) -> None:
    lane.chat = None
    lane.ytdlp.captions = caption_track()
    lane.claude.moments = MOMENTS
    lane.claude.invalid_p3_calls = {1}  # the first 30 minutes get no valid answer
    video_id = lane.video(duration_s=2400)
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.spikes == 1  # only the moment of the second part
    assert [float(row["spike_offset_s"]) for row in spikes(video_id)] == [1800.0]
    row = db.fetch_one("select message, data from events where type = 'video.fallback_picked'")
    assert row is not None and "1 part(s)" in row["message"] and row["data"]["moments"] == 1


def test_a_chat_without_spikes_falls_back_to_the_transcript(lane: Lane) -> None:
    lane.chat = ReplayChat(
        "youtube", "vodtest0001", synthetic.flat_chat(2400, 1, platform="youtube"), duration_s=2400.0
    )
    lane.ytdlp.captions = caption_track()
    lane.claude.moments = MOMENTS[:1]
    video_id = lane.video(duration_s=2400)
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert outcome.source == "transcript (captions)" and outcome.queued == 1
    assert lane.claude.asked(calls.P1) == 0 and lane.claude.asked(calls.P3) == 2


def test_by_default_a_chat_with_fewer_than_three_spikes_is_topped_up_from_the_transcript(lane: Lane) -> None:
    """Migration 0010: `vod.fallback_below_spikes` = 3. The fixture chat confirms two spikes; the
    transcript adds its moments to them instead of the video ending with two clips."""
    settings.set("vod.fallback_below_spikes", DEFAULTS["vod.fallback_below_spikes"])
    assert VodConfig.load().fallback_below_spikes == 3
    lane.ytdlp.captions = caption_track()
    lane.claude.moments = MOMENTS
    video_id = lane.video()
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.source == "chat"
    stored = spikes(video_id)
    assert sorted(row["mode"] for row in stored) == ["replay"] * 2 + ["transcript"] * 3
    confirmed = [row["mode"] for row in stored if row["status"] != "rejected"]
    assert sorted(confirmed) == ["replay", "replay", "transcript", "transcript"]
    assert (outcome.spikes, outcome.confirmed, outcome.queued) == (5, 4, 4)
    assert lane.claude.asked(calls.P3) >= 1 and len(render_jobs()) == 4

    # with the M08 value the same video ends with the two chat clips and no transcript request
    settings.set("vod.fallback_below_spikes", 1)
    other = lane.video(source_id="vodtest0002")
    lane.enqueue(other)
    asked = lane.claude.asked(calls.P3)
    assert lane.run_next().queued == 2 and lane.claude.asked(calls.P3) == asked


def test_no_chat_no_captions_and_no_transcription_allowed_ends_without_clips(lane: Lane) -> None:
    lane.chat = None
    settings.set("vod.fallback_transcribe_max_s", 0)
    video_id = lane.video(has_chat=False)
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert outcome.status == "done" and outcome.queued == 0 and outcome.spikes == 0
    assert lane.chat_calls == 0  # has_chat = false: the chat is not even asked for
    assert lane.claude.calls == []
    row = db.fetch_one("select message, data from events where type = 'video.fallback_picked'")
    assert row is not None and row["data"]["source"] == "none" and "switched off" in row["message"]
    assert not store.video_dir(video_id).exists() and queued("vod.cleanup") == []


def test_the_daily_transcription_allowance_defers_the_video(lane: Lane) -> None:
    lane.chat = None
    settings.set("vod.fallback_transcribe_max_s", 5400)
    usage.record("vod", store.USAGE_FALLBACK_TRANSCRIBE, units=21600)
    video_id = lane.video(has_chat=False)
    lane.enqueue(video_id)
    outcome = lane.run_next()
    assert outcome.status == "deferred" and outcome.kind == "transcribe_budget"
    assert video_row(video_id)["status"] == "new"


# ── replay pass after a live stream ──────────────────────────


def test_replay_scan_skips_what_the_live_lane_already_clipped(lane: Lane) -> None:
    started = datetime(2026, 11, 17, 6, 0, 0, tzinfo=UTC)
    clipped = [
        {
            "start_wall": (started + timedelta(seconds=3040)).isoformat(),
            "end_wall": (started + timedelta(seconds=3080)).isoformat(),
            "clip_id": "live-clip",
        }
    ]
    session = db.fetch_one(
        """
        insert into live_sessions
          (creator_id, platform, source_id, channel_login, url, status, started_at, clipped_ranges,
           vod_source_id)
        values (%s, 'youtube', 'vodtest0001', 'UCvodtest', 'https://www.youtube.com/watch?v=vodtest0001',
                'ended', %s, %s::jsonb, 'vodtest0001')
        returning id::text as id
        """,
        (lane.creator_id, started, json.dumps(clipped)),
    )
    assert session is not None
    jobs.enqueue("normal", "replay.scan", {"live_session_id": session["id"]}, dedupe_key="replay:1")

    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.spikes == 1
    video = db.fetch_one("select * from videos where live_session_id = %s", (session["id"],))
    assert video is not None and video["source_id"] == "vodtest0001" and video["status"] == "done"
    stored = spikes(str(video["id"]))
    # The spike at 3000 s (window 2980-3015) is within 30 s of the clipped range 3040-3080.
    assert [float(row["spike_offset_s"]) for row in stored] == [6000.0]
    assert str(stored[0]["live_session_id"]) == session["id"]
    assert render_jobs()[0]["payload"]["lane"] == "hourly"


# ── clean-up ─────────────────────────────────────────────────


def test_cleanup_removes_windows_whose_render_has_ended(lane: Lane) -> None:
    video_id = lane.video()
    lane.enqueue(video_id)
    lane.run_next()
    first, second = (str(row["id"]) for row in spikes(video_id))
    folder = store.sections_dir(video_id)
    assert sorted(path.stem for path in folder.iterdir()) == sorted([first, second])
    db.execute("update spikes set status = 'clipped' where id = %s", (first,))
    db.execute("update spikes set status = 'rendering' where id = %s", (second,))
    job = claim_type("vod.cleanup")

    cleanup.handle_cleanup(job, lane.deps())
    jobs.complete(job.id)

    assert [path.stem for path in folder.iterdir()] == [second]
    again = queued("vod.cleanup")
    assert len(again) == 1 and again[0]["run_after"] == NOW + timedelta(seconds=900)

    db.execute("update spikes set status = 'rejected' where id = %s", (second,))
    job = claim_type("vod.cleanup")
    cleanup.handle_cleanup(job, lane.deps())
    jobs.complete(job.id)
    assert not store.video_dir(video_id).exists() and queued("vod.cleanup") == []


def test_cleanup_deletes_old_chat_files_and_keeps_the_numbers(lane: Lane) -> None:
    video_id = lane.video()
    lane.enqueue(video_id)
    lane.run_next()
    path = store.chat_path("youtube", "vodtest0001")
    assert path.is_file()
    assert cleanup.clean_chat_files(604800) == 0 and path.is_file()
    db.execute(
        "update chat_files set downloaded_at = now() - interval '8 days' where video_id = %s", (video_id,)
    )
    assert cleanup.clean_chat_files(604800) == 1
    row = db.fetch_one("select storage_path, messages_count from chat_files where video_id = %s", (video_id,))
    assert not path.exists() and row == {"storage_path": None, "messages_count": 1900}


def test_register_handlers_is_safe_to_call_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    from sixstars import vod
    from sixstars.services import worker

    monkeypatch.setattr(worker, "HANDLERS", {})
    vod.register_handlers()
    vod.register_handlers()
    assert sorted(worker.HANDLERS) == ["replay.scan", "vod.cleanup", "vod.process"]
    assert worker.HANDLERS["vod.process"] is process.handle_vod_process

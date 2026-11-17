"""The clip factory end to end against the test database: `render_clip` with a real ffmpeg render of a
synthetic source, fake transcription, fake Claude and fake Storage. Covers the clip row, the series number,
idempotent retries, rejections, the layout cache, the job handlers and the spike-based CLI input.
Skipped when ffmpeg is not installed.
"""

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from sixstars import config, db, jobs, settings
from sixstars.ai.calls import BudgetExceededError
from sixstars.ai.schemas import Layout as DetectedLayout
from sixstars.ai.schemas import PixelBox
from sixstars.clients.fakes import FakeAnthropicClient, FakeTranscriber
from sixstars.clients.transcribe import Transcript
from sixstars.clients.transcribe import Word as ClientWord
from sixstars.factory import commands, facecam, music, pipeline, tools
from sixstars.factory.config import FactoryConfig
from sixstars.factory.layout import Box, LayoutA, LayoutB
from sixstars.factory.lines import Word, group_lines
from sixstars.storage import FakeClipStorage

pytestmark = pytest.mark.ffmpeg

SPEECH = (
    "He did NOT expect that! No way, fifty thousand dollars. This fucking cop just crashed into the "
    "helicopter and exploded everywhere. Oh my god, look at that. I can not believe it, he is still alive. "
    "Chat, did you see that? That was insane. We are so done. Run, run, run! Get in the car right now."
)
SENTENCES = tuple(SPEECH.split())


def spoken_words(start_s: float = 1.0, step_s: float = 0.45) -> tuple[ClientWord, ...]:
    """One word every `step_s` seconds from `start_s`: about 27 seconds of speech."""
    return tuple(
        ClientWord(text, round(start_s + i * step_s, 3), round(start_s + i * step_s + 0.35, 3))
        for i, text in enumerate(SENTENCES)
    )


def line_of(word: str) -> int:
    """The subtitle line number that holds `word` in the fake transcript."""
    lines = group_lines([Word(w.w, w.s, w.e) for w in spoken_words()])
    return next(line.index for line in lines if word in line.text)


def p2_reply(start_s: float, end_s: float, **overrides: object) -> dict[str, object]:
    reply: dict[str, object] = {
        "start_s": start_s,
        "end_s": end_s,
        "hook": "He did NOT expect that 😭",
        "title": "Cop crashes into a helicopter",
        "caption": "Fifty thousand dollars gone in one second.",
        "hashtags": ["gta", "gtarp", "fail"],
        "commentary": None,
        "topic": "cop crashes into helicopter",
        "keywords": [{"line": line_of("helicopter"), "words": ["helicopter"]}],
        "spoiler": False,
        "safety": {},
        "score": 8,
        "score_parts": {"hook": 8, "clarity": 7, "payoff": 9, "rewatch": 8},
        "reject": False,
        "reject_reason": None,
    }
    reply.update(overrides)
    return reply


# The vision model sees the frame shrunk to 1280x720 and answers in those pixels; the code scales the
# boxes back to the 1920x1080 source. `FACECAM` is the scaled result.
P4_FACECAM = {
    "facecam": {"x": 960, "y": 480, "w": 320, "h": 240},
    "gameplay": {"x": 0, "y": 0, "w": 1280, "h": 720},
    "confidence": 0.9,
}
P4_NO_FACECAM = {"facecam": None, "gameplay": {"x": 0, "y": 0, "w": 1280, "h": 720}, "confidence": 0.92}
FACECAM = DetectedLayout(
    facecam=PixelBox(x=1440, y=720, w=480, h=360), gameplay=PixelBox(x=0, y=0, w=1920, h=1080), confidence=0.9
)


class FlagDetector:
    """A music detector that flags the listed frame numbers."""

    def __init__(self, music_frames: tuple[int, ...] = ()) -> None:
        self.music_frames = music_frames
        self.calls = 0

    def flags(self, frames: music.FloatArray) -> list[bool]:
        self.calls += 1
        return [index in self.music_frames for index in range(frames.shape[0])]


@pytest.fixture(scope="session")
def source_30s(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 30-second 1920x1080 30 fps test pattern with a 440 Hz tone (the `clip_source_30s.mp4` of M06)."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    path = tmp_path_factory.mktemp("factory source") / "clip_source_30s.mp4"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi"]
    command += ["-i", "testsrc2=size=1920x1080:rate=30:duration=30", "-f", "lavfi"]
    command += ["-i", "sine=frequency=440:sample_rate=48000:duration=30", "-c:v", "libx264"]
    command += ["-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=300)
    return path


@pytest.fixture
def data_dir(database: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data dir"
    monkeypatch.setenv("DATA_DIR", str(path))
    config.env.cache_clear()
    return path


@pytest.fixture
def creator_id(database: None) -> Iterator[str]:
    """A creator row for one test; everything hanging off it is deleted afterwards."""
    row = db.fetch_one(
        "insert into creators (platform, channel_id, handle) values ('youtube', %s, 'test_streamer') "
        "returning id",
        (f"factory-test-{uuid.uuid4()}",),
    )
    assert row is not None
    creator = str(row["id"])
    yield creator
    db.execute("update spikes set clip_id = null where creator_id = %s", (creator,))
    db.execute("delete from clips where creator_id = %s", (creator,))
    db.execute("delete from spikes where creator_id = %s", (creator,))
    db.execute("delete from creators where id = %s", (creator,))


def make_spike(creator: str, **columns: object) -> str:
    values: dict[str, object] = {
        "mode": "replay",
        "creator_id": creator,
        "tier": "A",
        "status": "confirmed",
        "spike_offset_s": 3612.0,
        "window_start_s": 3604.0,
        "window_end_s": 3626.0,
        "parts": Jsonb({"spike_floor": 3610.0, "pad_start": 3600.0, "pad_end": 3630.0}),
        "chat_sample": Jsonb(
            [{"t_rel": -1.5, "user": "a", "text": "LMAO"}, {"t_rel": 2.0, "text": "clip it"}]
        ),
    }
    values.update(columns)
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    row = db.fetch_one(f"insert into spikes ({names}) values ({marks}) returning id", tuple(values.values()))
    assert row is not None
    return str(row["id"])


def short_clips() -> None:
    """Let the writer make 6-second clips so most tests render quickly."""
    settings.set("spikes.min_len_s", 5)
    settings.set("gate.hourly", {"min_len": 5, "max_len": 60, "max_music_ratio": 0.1})
    settings.set("gate.express", {"min_len": 5, "max_len": 45, "max_music_ratio": 0.0})


def render_input(source: Path, creator: str, **overrides: object) -> pipeline.RenderInput:
    values: dict[str, object] = {
        "source_path": source,
        "window_start_s": 4.0,
        "window_end_s": 26.0,
        "spike_floor_s": 10.0,
        "creator": "test_streamer",
        "creator_id": creator,
        "platform": "youtube",
        "source_id": "vid123",
        "source_url": "https://youtube.com/watch?v=vid123",
        "stream_title": "GTA RP night",
        "pad_start_s": 0.0,
        "pad_end_s": 30.0,
        "source_offset_s": 3600.0,
    }
    values.update(overrides)
    return pipeline.RenderInput(**values)  # type: ignore[arg-type]


def make_deps(
    p2: object,
    p4: object = P4_FACECAM,
    music_frames: tuple[int, ...] = (),
    transcript: Transcript | None = None,
) -> pipeline.Deps:
    spoken = transcript or Transcript(language="en", words=spoken_words(), duration_s=30.0, provider="fake")
    return pipeline.Deps(
        transcriber=FakeTranscriber(spoken),
        ai_client=FakeAnthropicClient({"p2_clip_writer": p2, "p4_layout_detect": p4}),
        music_detector=FlagDetector(music_frames),
        storage=FakeClipStorage(),
    )


def clip_row(clip_id: str) -> dict[str, Any]:
    row = db.fetch_one("select * from clips where id = %s", (clip_id,))
    assert row is not None
    return row


def event_types(clip_id: str) -> list[str]:
    rows = db.fetch_all("select type from events where entity_id = %s order by id", (clip_id,))
    return [str(row["type"]) for row in rows]


def series_last_value() -> int:
    row = db.fetch_one("select last_value from clip_series_seq")
    assert row is not None
    return int(row["last_value"])


def integrated_loudness(video: Path) -> float:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(video), "-vn", "-af", "ebur128", "-f", "null", "-"],
        capture_output=True,
        check=True,
        timeout=120,
    )
    stderr = result.stderr.decode("utf-8", errors="replace")
    summary = stderr[stderr.rindex("Integrated loudness:") :]
    return float(summary.split("I:")[1].split("LUFS")[0])


# ── the acceptance render ────────────────────────────────────


def test_render_clip_normal_layout_a(source_30s: Path, creator_id: str, data_dir: Path) -> None:
    spike_id = make_spike(creator_id)
    deps = make_deps(p2_reply(5.0, 27.0), music_frames=(3,))
    item = render_input(source_30s, creator_id, spike_id=spike_id)

    clip_id = pipeline.render_clip(item, deps=deps)

    row = clip_row(clip_id)
    assert row["status"] == "gating" and row["series_no"] >= 1 and str(row["spike_id"]) == spike_id
    assert (row["lane"], row["gate_profile"], row["source_type"]) == ("hourly", "hourly", "vod")
    assert (row["source_platform"], row["source_id"]) == ("youtube", "vid123")
    # the writer's trim plus the room left around the words, as positions in the original video
    assert float(row["start_s"]) == pytest.approx(3600.0 + 4.95, abs=0.01), "up to the previous word"
    assert float(row["end_s"]) == pytest.approx(3600.0 + 27.0, abs=0.35)
    duration = float(row["duration_s"])
    assert duration == pytest.approx(float(row["end_s"]) - float(row["start_s"]), abs=0.05)
    # P2 outputs
    assert row["hook"] == "He did NOT expect that 😭" and row["title"] == "Cop crashes into a helicopter"
    assert row["hashtags"] == ["gta", "gtarp", "fail"] and row["topic"] == "cop-crashes-into-helicopter"
    assert row["keywords"] == {str(line_of("helicopter")): ["helicopter"]}
    assert row["spoiler"] is False and float(row["score"]) == 8 and row["score_parts"]["payoff"] == 9
    assert row["safety_flags"]["gambling"] is False and row["original_language"] == "en"
    # transcript in clip time: only the words of the final clip, the first one right after the cut lead
    words = row["transcript"]["words"]
    assert row["transcript"]["language"] == "en"
    assert words[0]["s"] == pytest.approx(0.10, abs=0.02) and words[-1]["s"] < duration
    assert "fucking" in [word["w"] for word in words]
    # analysis results for the gate
    assert float(row["music_ratio"]) == pytest.approx(1 / round(duration), abs=0.01)
    assert row["profanity_count"] == 1 and row["render_ms"] > 0 and row["storage_path"] is None

    output = Path(row["local_path"])
    assert output == data_dir / "ready" / f"{clip_id}.mp4" and output.stat().st_size == row["file_size_bytes"]
    assert not (data_dir / "render" / clip_id).exists(), "scratch directory must be deleted"
    media = tools.probe(output)
    assert (media.width, media.height, media.video_codec, media.audio_codec) == (1080, 1920, "h264", "aac")
    assert media.fps == pytest.approx(30.0) and media.sample_rate == 48000
    assert media.duration_s == pytest.approx(duration + 1.5, abs=0.1), "window plus the end card"
    assert integrated_loudness(output) == pytest.approx(-14.0, abs=1.0)

    spike = db.fetch_one("select status, clip_id from spikes where id = %s", (spike_id,))
    assert spike is not None and spike["status"] == "clipped" and str(spike["clip_id"]) == clip_id
    assert event_types(clip_id) == ["clip.render_started", "clip.rendered"]
    rendered = db.fetch_one(
        "select data from events where entity_id = %s and type = 'clip.rendered'", (clip_id,)
    )
    assert rendered is not None
    data = rendered["data"]
    assert data["layout"] == "A" and data["mode"] == "normal" and data["music"]["frames"][3] == 1
    assert (
        len(data["bleeps"]) == 1 and len(data["sfx_times_s"]) == 1 and data["transcriber"].startswith("fake")
    )
    assert set(data["timings_ms"]) == {"transcribe", "write", "music", "layout", "render"}

    # the layout was detected once and cached for the creator
    layout = db.fetch_one("select * from creator_layouts where creator_id = %s", (creator_id,))
    assert layout is not None and layout["facecam_box"] == {"x": 1440, "y": 720, "w": 480, "h": 360}
    assert (layout["source_width"], layout["source_height"]) == (1920, 1080)
    assert (layout["expires_at"] - layout["detected_at"]).days == 7
    assert facecam.cached_layout(creator_id, 1920, 1080) == LayoutA(facecam_box=Box(1440, 720, 480, 360))

    # what the collaborators were asked
    assert isinstance(deps.transcriber, FakeTranscriber) and isinstance(deps.ai_client, FakeAnthropicClient)
    assert [call["mode"] for call in deps.transcriber.calls_to("transcribe")] == ["local"]
    p2_call = next(c for c in deps.ai_client.calls_to("complete_json") if c["prompt_id"] == "p2_clip_writer")
    assert p2_call["ref_id"] == clip_id and "fucking" in p2_call["user_content"]
    assert "LMAO" not in p2_call["user_content"], "no chat was passed in this input"

    # a second call for the same spike is a no-op: same clip, same series number, nothing rendered
    sequence = series_last_value()
    modified = output.stat().st_mtime_ns
    assert pipeline.render_clip(item, deps=deps) == clip_id
    assert series_last_value() == sequence and output.stat().st_mtime_ns == modified
    assert len(deps.transcriber.calls_to("transcribe")) == 1
    assert event_types(clip_id) == ["clip.render_started", "clip.rendered"]

    # upload after the gate: once
    assert isinstance(deps.storage, FakeClipStorage)
    key = pipeline.upload_clip(clip_id, storage=deps.storage)
    assert key.startswith("clips/test/") and key.endswith(f"/{clip_id}.mp4")
    assert deps.storage.objects[key] == output.read_bytes() and clip_row(clip_id)["storage_path"] == key
    assert pipeline.upload_clip(clip_id, storage=FakeClipStorage()) == key, "already uploaded: not sent again"
    assert event_types(clip_id)[-1] == "clip.uploaded"


def test_failed_render_is_retried_under_the_same_series_number(
    source_30s: Path, creator_id: str, data_dir: Path
) -> None:
    short_clips()
    spike_id = make_spike(creator_id)
    item = render_input(source_30s, creator_id, spike_id=spike_id)
    before = series_last_value()

    broken = make_deps(RuntimeError("model overloaded"))
    with pytest.raises(RuntimeError, match="model overloaded"):
        pipeline.render_clip(item, deps=broken)
    row = db.fetch_one("select id, series_no, status, hook from clips where spike_id = %s", (spike_id,))
    assert row is not None and row["status"] == "rendering" and row["hook"] is None
    clip_id, series_no = str(row["id"]), row["series_no"]
    assert not (data_dir / "render" / clip_id).exists(), "scratch is deleted when the render fails too"
    assert not (data_dir / "ready" / f"{clip_id}.mp4").exists()

    assert pipeline.render_clip(item, deps=make_deps(p2_reply(6.0, 12.0))) == clip_id
    row = clip_row(clip_id)
    assert row["status"] == "gating" and row["series_no"] == series_no
    assert series_last_value() == series_no and series_no >= before, "one number for one clip"
    count = db.fetch_one("select count(*) as n from clips where spike_id = %s", (spike_id,))
    assert count is not None and count["n"] == 1
    assert event_types(clip_id).count("clip.render_started") == 1


def test_writer_rejection_and_silence_close_the_clip_without_rendering(
    source_30s: Path, creator_id: str, data_dir: Path
) -> None:
    short_clips()
    spike_id = make_spike(creator_id)
    rejected = p2_reply(6.0, 12.0, reject=True, reject_reason="sponsored read", safety={"sponsored": True})
    deps = make_deps(rejected)
    clip_id = pipeline.render_clip(render_input(source_30s, creator_id, spike_id=spike_id), deps=deps)
    row = clip_row(clip_id)
    assert row["status"] == "rejected" and row["reject_reason"] == "writer: sponsored read"
    assert row["hook"] and row["safety_flags"]["sponsored"] is True, "the P2 outputs are kept for the record"
    assert row["local_path"] is None and not (data_dir / "ready" / f"{clip_id}.mp4").exists()
    assert not (data_dir / "render" / clip_id).exists()
    spike = db.fetch_one("select status from spikes where id = %s", (spike_id,))
    assert spike is not None and spike["status"] == "rejected"
    assert event_types(clip_id) == ["clip.render_started", "clip.rejected"]
    assert isinstance(deps.music_detector, FlagDetector) and deps.music_detector.calls == 0

    silent = make_deps(p2_reply(6.0, 12.0), transcript=Transcript(language="en", words=(), duration_s=30.0))
    silent_id = pipeline.render_clip(
        render_input(source_30s, creator_id, clip_id=str(uuid.uuid4())), deps=silent
    )
    assert clip_row(silent_id)["reject_reason"] == "no_speech"
    assert isinstance(silent.ai_client, FakeAnthropicClient) and silent.ai_client.calls == []


def test_express_spike_clip_layout_b_and_the_layout_cache(
    source_30s: Path, creator_id: str, data_dir: Path
) -> None:
    short_clips()
    deps = make_deps(p2_reply(6.0, 12.5), p4=P4_NO_FACECAM, music_frames=(1,))
    first_id = str(uuid.uuid4())
    item = render_input(
        source_30s,
        creator_id,
        lane="spike",
        mode="express",
        source_type="live",
        clip_id=first_id,
        upload=True,
    )
    assert pipeline.render_clip(item, deps=deps) == first_id
    row = clip_row(first_id)
    assert (row["status"], row["lane"], row["gate_profile"]) == ("gating", "spike", "express")
    assert isinstance(deps.transcriber, FakeTranscriber) and isinstance(deps.storage, FakeClipStorage)
    assert [call["mode"] for call in deps.transcriber.calls_to("transcribe")] == ["hosted"]
    assert row["storage_path"] in deps.storage.objects, "upload=True uploads right after the render"
    rendered = db.fetch_one(
        "select data from events where entity_id = %s and type = 'clip.rendered'", (first_id,)
    )
    assert (
        rendered is not None and rendered["data"]["layout"] == "B" and rendered["data"]["mode"] == "express"
    )
    assert float(row["music_ratio"]) > 0, "music is reported even though an express clip is not ducked"
    media = tools.probe(Path(row["local_path"]))
    assert (media.width, media.height) == (1080, 1920)
    assert media.duration_s == pytest.approx(float(row["duration_s"]) + 1.5, abs=0.1)
    assert integrated_loudness(Path(row["local_path"])) == pytest.approx(-14.0, abs=1.0)
    assert facecam.cached_layout(creator_id, 1920, 1080) == LayoutB()

    # the next clip of the same creator reuses the cached layout: no second P4 call
    again = make_deps(p2_reply(6.0, 12.5), p4=RuntimeError("P4 must not be called"))
    second_id = pipeline.render_clip(
        render_input(source_30s, creator_id, clip_id=str(uuid.uuid4())), deps=again
    )
    assert clip_row(second_id)["status"] == "gating"
    assert clip_row(second_id)["series_no"] == row["series_no"] + 1
    assert isinstance(again.ai_client, FakeAnthropicClient)
    assert [call["prompt_id"] for call in again.ai_client.calls_to("complete_json")] == ["p2_clip_writer"]


# ── layout cache ─────────────────────────────────────────────


def test_layout_cache_validity(creator_id: str) -> None:
    assert facecam.cached_layout(creator_id, 1920, 1080) is None
    facecam.store_layout(creator_id, 1920, 1080, FACECAM, "clip-1@12.0s")
    assert facecam.cached_layout(creator_id, 1920, 1080) == LayoutA(facecam_box=Box(1440, 720, 480, 360))
    assert facecam.cached_layout(creator_id, 1280, 720) is None, "another resolution: detect again"
    db.execute(
        "update creator_layouts set expires_at = now() - interval '1 minute' where creator_id = %s",
        (creator_id,),
    )
    assert facecam.cached_layout(creator_id, 1920, 1080) is None, "expired"
    unsure = DetectedLayout(facecam=FACECAM.facecam, gameplay=FACECAM.gameplay, confidence=0.4)
    facecam.store_layout(creator_id, 1920, 1080, unsure, "clip-2@3.0s")
    row = db.fetch_one(
        "select confidence, expires_at > now() as live from creator_layouts where creator_id = %s",
        (creator_id,),
    )
    assert row is not None and float(row["confidence"]) == pytest.approx(0.4) and row["live"] is True
    assert facecam.cached_layout(creator_id, 1920, 1080) is None, "low confidence is never reused"


def test_factory_config_loads_from_the_settings_table(database: None) -> None:
    cfg = FactoryConfig.load("hourly")
    assert (cfg.params.width, cfg.params.height, cfg.params.fps) == (1080, 1920, 30)
    assert cfg.params.watermark == "@6starsclipping" and cfg.params.loudness_lufs == -14
    assert (cfg.min_len_s, cfg.max_len_s, cfg.context_pad_s, cfg.max_music_ratio) == (20.0, 60.0, 15.0, 0.1)
    assert FactoryConfig.load("spike").max_len_s == 45.0
    settings.set("factory.accent_color", "#00FF88")
    assert FactoryConfig.load("hourly").params.accent_color == "#00FF88"


# ── job handlers ─────────────────────────────────────────────


def claimed_job(job_type: str, payload: dict[str, Any], max_attempts: int = 3) -> jobs.Job:
    queue: jobs.Queue = "express" if job_type == "express.render" else "normal"
    jobs.enqueue(queue, job_type, payload, dedupe_key=f"{job_type}:test", max_attempts=max_attempts)
    job = jobs.claim(queue, "test-worker")
    assert job is not None
    return job


def test_handler_closes_the_clip_after_the_last_attempt(
    creator_id: str, data_dir: Path, tmp_path: Path
) -> None:
    spike_id = make_spike(creator_id)
    item = render_input(tmp_path / "missing.mp4", creator_id, spike_id=spike_id)

    first = claimed_job("clip.render", item.to_payload(), max_attempts=2)
    with pytest.raises(tools.FfmpegError):
        pipeline.handle_clip_render(first)
    row = db.fetch_one("select id, status from clips where spike_id = %s", (spike_id,))
    assert row is not None and row["status"] == "rendering", "attempts are left: the row stays open"
    assert jobs.fail(first.id, "FfmpegError") == "queued"
    db.execute("update jobs set run_after = now() where id = %s", (first.id,))

    last = jobs.claim("normal", "test-worker")
    assert last is not None and last.attempts == 2
    with pytest.raises(tools.FfmpegError):
        pipeline.handle_clip_render(last)
    closed = clip_row(str(row["id"]))
    assert closed["status"] == "rejected" and closed["reject_reason"].startswith("render_failed: FfmpegError")
    spike = db.fetch_one("select status from spikes where id = %s", (spike_id,))
    assert spike is not None and spike["status"] == "failed"
    assert event_types(str(row["id"])) == ["clip.render_started", "clip.render_failed"]
    outbox = db.fetch_one("select severity, title, body from notify_outbox order by id desc limit 1")
    assert outbox is not None and outbox["severity"] == "warn" and str(row["id"]) in outbox["body"]


def test_handler_defers_a_job_stopped_by_the_ai_budget(
    creator_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def over_budget(item: pipeline.RenderInput, **_: object) -> str:
        raise BudgetExceededError("p2_clip_writer: ai.daily_budget_usd is used up for today")

    monkeypatch.setattr(pipeline, "render_clip", over_budget)
    item = render_input(tmp_path / "a.mp4", creator_id)
    job = claimed_job("clip.render", item.to_payload())
    pipeline.handle_clip_render(job)  # returns normally: the worker marks this job done
    queued = db.fetch_all("select * from jobs where status = 'queued' and type = 'clip.render'")
    assert len(queued) == 1 and queued[0]["dedupe_key"] == "clip.render:test:budget"
    assert queued[0]["payload"] == json.loads(json.dumps(item.to_payload()))
    assert queued[0]["run_after"] == pipeline.next_budget_reset(queued[0]["created_at"])
    event = db.fetch_one("select level, entity_id from events where type = 'clip.render_deferred'")
    assert event is not None and event["level"] == "warn" and event["entity_id"] == str(job.id)


def test_handler_runs_the_registered_next_step_for_a_rendered_clip(
    creator_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = render_input(tmp_path / "a.mp4", creator_id, lane="spike", source_type="live")
    seen: list[tuple[pipeline.RenderInput, str]] = []
    after: list[tuple[str, str]] = []

    def fake_render(received: pipeline.RenderInput, **_: object) -> str:
        row = pipeline.claim_clip(received)
        db.execute("update clips set status = %s where id = %s", (status, row.id))
        seen.append((received, row.id))
        return row.id

    monkeypatch.setattr(pipeline, "render_clip", fake_render)
    pipeline.set_after_render("express.render", lambda clip_id, job: after.append((clip_id, job.type)))
    try:
        status = "gating"
        payload = {key: value for key, value in item.to_payload().items() if key != "mode"}
        pipeline.handle_express_render(claimed_job("express.render", payload))
        assert seen[0][0].mode == "express", "the job type decides the mode when the payload has none"
        assert after == [(seen[0][1], "express.render")]
        db.execute("update jobs set status = 'done'")
        status = "rejected"
        other = render_input(tmp_path / "a.mp4", creator_id, clip_id=str(uuid.uuid4()))
        pipeline.handle_express_render(claimed_job("express.render", other.to_payload()))
        assert len(after) == 1, "a rejected clip does not go on to the gate"
    finally:
        pipeline.set_after_render("express.render", None)


# ── spike-based input (CLI `clip render`) ────────────────────


def test_input_from_spike_and_render_spike(source_30s: Path, creator_id: str, data_dir: Path) -> None:
    video = db.fetch_one(
        """
        insert into videos (creator_id, platform, source_id, url, title)
        values (%s, 'youtube', %s, 'https://youtube.com/watch?v=abc', 'GTA RP night')
        returning id
        """,
        (creator_id, f"abc-{uuid.uuid4()}"),
    )
    assert video is not None
    try:
        spike_id = make_spike(creator_id, video_id=video["id"])
        item = commands.input_from_spike(spike_id, source_30s, source_offset_s=3600.0)
        assert (item.window_start_s, item.window_end_s, item.spike_floor_s) == (4.0, 26.0, 10.0)
        assert (item.pad_start_s, item.pad_end_s, item.source_offset_s) == (0.0, 30.0, 3600.0)
        assert (item.creator, item.creator_id, item.platform) == ("test_streamer", creator_id, "youtube")
        assert (item.lane, item.mode, item.source_type, item.stream_title) == (
            "hourly",
            "normal",
            "vod",
            "GTA RP night",
        )
        assert item.source_url == "https://youtube.com/watch?v=abc" and item.spike_id == spike_id
        assert [(line.t_s, line.text, line.user) for line in item.chat] == [
            (10.5, "LMAO", "a"),
            (14.0, "clip it", None),
        ]
        express = commands.input_from_spike(spike_id, source_30s, source_offset_s=3600.0, lane="spike")
        assert (express.lane, express.mode) == ("spike", "express")

        live = make_spike(
            creator_id, mode="live", tier="S", window_start_s=None, window_end_s=None, spike_offset_s=None
        )
        with pytest.raises(ValueError, match="pass the window"):
            commands.input_from_spike(live, source_30s)
        from_buffer = commands.input_from_spike(live, source_30s, window=(3.0, 25.0))
        assert (from_buffer.lane, from_buffer.mode, from_buffer.source_type) == ("spike", "express", "live")
        assert from_buffer.spike_floor_s is None and from_buffer.pad_start_s is None
        with pytest.raises(ValueError, match="does not exist"):
            commands.input_from_spike(str(uuid.uuid4()), source_30s)

        # `sixstars clip render <spike_id> --source ...` with fakes behind render_clip
        short_clips()
        deps = make_deps(p2_reply(6.0, 12.0))
        real_render = pipeline.render_clip
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                commands, "render_clip", lambda item, force=False: real_render(item, deps=deps, force=force)
            )
            assert commands.render_spike(spike_id, source=source_30s, source_offset_s=3600.0) == 0
        row = db.fetch_one("select status from clips where spike_id = %s", (spike_id,))
        assert row is not None and row["status"] == "gating"
        assert isinstance(deps.ai_client, FakeAnthropicClient)
        p2_call = next(
            c for c in deps.ai_client.calls_to("complete_json") if c["prompt_id"] == "p2_clip_writer"
        )
        assert "LMAO" in p2_call["user_content"], "the chat sample of the spike reaches the clip writer"
    finally:
        db.execute("update spikes set clip_id = null, video_id = null where creator_id = %s", (creator_id,))
        db.execute("delete from clips where creator_id = %s", (creator_id,))
        db.execute("delete from videos where id = %s", (video["id"],))

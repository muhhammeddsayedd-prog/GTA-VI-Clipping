"""The pure parts of the M06 wiring: settings, assets, music rule, layout choice, job payloads, the CLI
helpers, ffprobe parsing, emoji font switching and the Storage client. No network, no ffmpeg, no database."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest
import respx

from sixstars import storage
from sixstars.ai.calls import ChatLine
from sixstars.ai.schemas import Layout as DetectedLayout
from sixstars.ai.schemas import PixelBox, SpikeVerdict
from sixstars.clients._http import ApiError, NotConfiguredError
from sixstars.factory import commands, facecam, music, pipeline, tools
from sixstars.factory.assets import ASSETS_ENV, assets_dir, load_assets
from sixstars.factory.bleep import Span
from sixstars.factory.config import OFFLINE_DEFAULTS, FactoryConfig, gate_profile_for
from sixstars.factory.escape import ass_text_emoji
from sixstars.factory.layout import Box, LayoutA, LayoutB
from sixstars.factory.lines import Line, Word
from sixstars.factory.params import FactoryParams
from sixstars.factory.window import ClipWindow
from sixstars.factory.writer import WriterInput

REPO = Path(__file__).resolve().parents[3]

# ── config ───────────────────────────────────────────────────


def settings_values(**extra: object) -> dict[str, object]:
    values: dict[str, object] = dict(OFFLINE_DEFAULTS)
    values.update(extra)
    return values


def test_config_narrows_the_length_limits_by_the_lane_gate_profile() -> None:
    hourly = FactoryConfig.from_mapping(settings_values(), "hourly")
    assert (hourly.gate_profile, hourly.min_len_s, hourly.max_len_s, hourly.max_music_ratio) == (
        "hourly", 20.0, 60.0, 0.1,
    )  # fmt: skip
    express = FactoryConfig.from_mapping(settings_values(), "spike")
    assert (express.gate_profile, express.min_len_s, express.max_len_s, express.max_music_ratio) == (
        "express", 15.0, 45.0, 0.0,
    )  # fmt: skip
    assert hourly.context_pad_s == 15.0 and hourly.whisper_model == "medium"
    assert isinstance(hourly.params, FactoryParams) and hourly.params.watermark == "@6starsclipping"


def test_config_without_a_gate_profile_uses_the_spike_limits() -> None:
    values = settings_values()
    del values["gate.hourly"]
    cfg = FactoryConfig.from_mapping(values, "hourly")
    assert (cfg.min_len_s, cfg.max_len_s, cfg.max_music_ratio) == (15.0, 60.0, 0.0)
    # a gate profile that contradicts the spike limits is ignored rather than producing an empty range
    odd = FactoryConfig.from_mapping(
        settings_values(**{"gate.hourly": {"min_len": 90, "max_len": 120}}), "hourly"
    )
    assert (odd.min_len_s, odd.max_len_s) == (15.0, 60.0)


def test_config_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        FactoryConfig.from_mapping(settings_values(**{"spikes.min_len_s": 0}), "hourly")
    with pytest.raises(TypeError):
        FactoryConfig.from_mapping(settings_values(**{"vod.whisper_model": 3}), "hourly")
    with pytest.raises(KeyError):
        FactoryConfig.from_mapping({"spikes.min_len_s": 15, "spikes.max_len_s": 60}, "hourly")


def test_offline_defaults_follow_the_seed_migration() -> None:
    """The offline defaults of `clip render-file` must not drift from the seeded settings."""
    seed = (REPO / "supabase" / "migrations" / "0002_seed_settings.sql").read_text(encoding="utf-8")
    for key, value in OFFLINE_DEFAULTS.items():
        if key.startswith("gate.") or key == "factory.watermark":
            continue  # gate profiles are checked below; the watermark was changed by migration 0003
        assert f"('{key}', '{json.dumps(value)}'::jsonb" in seed, key
    cfg = FactoryConfig.offline("spike", {"factory.crf": 18, "vod.whisper_model": "small"})
    assert cfg.params.crf == 18 and cfg.whisper_model == "small" and cfg.max_len_s == 45.0
    assert gate_profile_for("spike") == "express" and gate_profile_for("hourly") == "hourly"


# ── assets ───────────────────────────────────────────────────


def test_bundled_assets_exist_and_resolve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ASSETS_ENV, raising=False)
    assert assets_dir() == REPO / "assets"
    assets = load_assets("Montserrat  black")
    assert assets.font_file == REPO / "assets" / "fonts" / "Montserrat-Black.ttf"
    assert assets.fonts.fonts_dir == REPO / "assets" / "fonts" and assets.fonts.font_file == assets.font_file
    assert assets.sfx_pop is not None and assets.sfx_pop.name == "pop.wav"
    assert assets.logo is not None and assets.logo.name == "star.png"
    assert (REPO / "assets" / "fonts" / "NotoEmoji.ttf").is_file()
    for licence in ("fonts/OFL-Montserrat.txt", "fonts/OFL-NotoEmoji.txt", "sfx/LICENSES.md"):
        assert (REPO / "assets" / licence).is_file(), licence
    assert assets.profanity().is_profane("fucking") and not assets.profanity().is_profane("duck")
    # another font is looked up in the system fonts; a directory without assets gives empty assets
    assert load_assets("Arial Black").font_file is None
    monkeypatch.setenv(ASSETS_ENV, str(tmp_path))
    empty = load_assets("Montserrat Black")
    assert assets_dir() == tmp_path
    assert (empty.font_file, empty.sfx_pop, empty.logo, empty.fonts.fonts_dir) == (None, None, None, None)
    assert not empty.profanity().is_profane("fucking")


# ── music ────────────────────────────────────────────────────

LABELS = ["Speech", "Music", "Song", "Singing", "Rapping", "Pop music", "Hip hop music", "Vehicle"]


def probabilities(*rows: dict[str, float]) -> music.FloatArray:
    table = np.zeros((len(rows), len(LABELS)), dtype=np.float32)
    for i, row in enumerate(rows):
        for label, value in row.items():
            table[i, LABELS.index(label)] = value
    return table


def test_frame_flags_follow_the_documented_thresholds() -> None:
    table = probabilities(
        {"Speech": 0.9, "Music": 0.5},  # exactly 0.5 is not above the threshold
        {"Music": 0.51},
        {"Speech": 0.8, "Singing": 0.41},
        {"Rapping": 0.4, "Song": 0.4, "Vehicle": 0.99},
        {"Hip hop music": 0.45},
        {},
    )
    assert music.frame_flags(table, LABELS) == [False, True, True, False, True, False]
    with pytest.raises(ValueError):
        music.frame_flags(table[:, :3], LABELS)
    with pytest.raises(ValueError):
        music.frame_flags(table, ["x"] * len(LABELS))


def test_split_frames_pads_a_long_tail_and_drops_a_short_one() -> None:
    rate = music.SAMPLE_RATE
    assert music.split_frames(np.ones(int(rate * 2.6), dtype=np.float32)).shape == (3, rate)
    padded = music.split_frames(np.ones(int(rate * 1.5), dtype=np.float32))
    assert padded.shape == (2, rate) and padded[1, rate // 2 - 1] == 1.0 and padded[1, rate // 2] == 0.0
    assert music.split_frames(np.ones(int(rate * 2.4), dtype=np.float32)).shape == (2, rate)
    assert music.split_frames(np.zeros(100, dtype=np.float32)).shape == (0, rate)


def test_music_analysis_ratio_spans_and_json() -> None:
    analysis = music.MusicAnalysis(frames=(False, True, True, False, True))
    assert analysis.ratio == pytest.approx(0.6)
    assert analysis.spans(4.6) == [Span(1.0, 3.0), Span(4.0, 4.6)]
    assert analysis.spans(1.5) == [Span(1.0, 1.5)]
    assert analysis.as_json() == {
        "ratio": 0.6,
        "frame_s": 1.0,
        "frames": [0, 1, 1, 0, 1],
        "detector": "panns-cnn14",
    }
    assert music.MusicAnalysis(frames=()).ratio == 0.0


def test_duck_spans_only_for_clips_under_the_music_limit() -> None:
    some = music.MusicAnalysis(frames=(True, True) + (False,) * 38)  # 5%
    assert music.duck_spans(some, 0.1, 40.0) == [Span(0.0, 2.0)]
    assert music.duck_spans(some, 0.0, 40.0) == [], (
        "over the express limit: the gate rejects, nothing is ducked"
    )
    assert music.duck_spans(music.MusicAnalysis(frames=(False,) * 20), 0.1, 20.0) == []
    assert music.duck_spans(None, 0.1, 20.0) == []


def test_a_single_flagged_frame_is_not_ducked() -> None:
    # speech over a game sound is flagged for one second now and then; real music lasts longer
    lone = music.MusicAnalysis(
        frames=(False, True, False, False, True, True, True, False, False, True) + (False,) * 40
    )
    assert lone.ratio == pytest.approx(0.1)
    assert music.duck_spans(lone, 0.1, 50.0) == [Span(4.0, 7.0)]


def test_analyse_without_audio_reports_no_music(tmp_path: Path) -> None:
    analysis = music.analyse(tmp_path / "silent.mp4", ClipWindow(0, 10), has_audio=False)
    assert analysis.frames == () and analysis.ratio == 0.0


def test_panns_detector_refuses_to_import_without_its_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(music.MusicDetectorUnavailableError, match="class_labels_indices.csv"):
        music.PannsDetector().flags(np.zeros((1, music.SAMPLE_RATE), dtype=np.float32))


# ── layout choice ────────────────────────────────────────────


def test_choose_layout() -> None:
    cam, full = Box(1434, 0, 486, 273), Box(0, 0, 1920, 1080)
    assert facecam.choose_layout(cam, full, 0.93, 1920, 1080) == LayoutA(facecam_box=cam, gameplay_box=None)
    panel = Box(0, 0, 1400, 800)
    assert facecam.choose_layout(cam, panel, 0.9, 1920, 1080) == LayoutA(facecam_box=cam, gameplay_box=panel)
    assert facecam.choose_layout(cam, full, 0.59, 1920, 1080) == LayoutB(), "low confidence: no facecam"
    assert facecam.choose_layout(None, full, 0.99, 1920, 1080) == LayoutB()
    assert facecam.choose_layout(Box(10, 10, 60, 40), full, 0.99, 1920, 1080) == LayoutB(), (
        "an icon, not a webcam"
    )


def test_box_from_json_clamps_and_rejects() -> None:
    assert facecam.box_from_json({"x": 1434, "y": 0, "w": 486, "h": 273}, 1920, 1080) == Box(
        1434, 0, 486, 273
    )
    assert facecam.box_from_json({"x": 1800, "y": 1000, "w": 500, "h": 500}, 1920, 1080) == Box(
        1800, 1000, 120, 80
    )
    for bad in (
        None,
        "box",
        {"x": 1},
        {"x": "a", "y": 0, "w": 1, "h": 1},
        {"x": 1919, "y": 0, "w": 9, "h": 9},
    ):
        assert facecam.box_from_json(bad, 1920, 1080) is None


def test_layout_from_detection() -> None:
    detected = DetectedLayout(
        facecam=PixelBox(x=0, y=23, w=289, h=201), gameplay=PixelBox(x=0, y=0, w=1280, h=720), confidence=0.93
    )
    layout = facecam.layout_from_detection(detected, 1280, 720)
    assert layout == LayoutA(facecam_box=Box(0, 23, 289, 201), gameplay_box=None)
    none = DetectedLayout(facecam=None, gameplay=PixelBox(x=0, y=0, w=1280, h=720), confidence=0.9)
    assert facecam.layout_from_detection(none, 1280, 720) == LayoutB()


# ── job payload and handlers ─────────────────────────────────


def render_input(**overrides: object) -> pipeline.RenderInput:
    values: dict[str, object] = {
        "source_path": Path("/data/vod/spike 42.mp4"),
        "window_start_s": 15.0,
        "window_end_s": 48.5,
        "spike_floor_s": 27.0,
        "creator": "speedyrp",
        "creator_id": "5d7b4c60-0000-4000-8000-000000000001",
        "platform": "twitch",
        "source_id": "v123",
        "source_url": "https://twitch.tv/videos/123",
        "lane": "spike",
        "mode": "express",
        "source_type": "live",
        "stream_title": "GTA RP | day 3",
        "spike_id": "5d7b4c60-0000-4000-8000-000000000002",
        "pad_start_s": 0.0,
        "pad_end_s": 63.5,
        "source_offset_s": 3600.0,
        "verdict": SpikeVerdict(is_moment=True, confidence=0.9, moment_type="fail", one_line="He crashes"),
        "chat": (ChatLine(t_s=30.5, text="LMAO", user="a"), ChatLine(t_s=31.0, text="clip it")),
    }
    values.update(overrides)
    return pipeline.RenderInput(**values)  # type: ignore[arg-type]


def test_render_input_round_trips_through_a_json_payload() -> None:
    item = render_input()
    payload = json.loads(json.dumps(item.to_payload()))
    assert pipeline.RenderInput.from_payload(payload) == item


def test_render_input_payload_defaults_follow_the_job_type() -> None:
    minimal = {"source_path": "/data/a.mp4", "window_start_s": 1, "window_end_s": 31, "creator": "x"}
    normal = pipeline.RenderInput.from_payload(minimal, default_mode="normal")
    assert (normal.mode, normal.lane, normal.platform, normal.source_type) == (
        "normal",
        "hourly",
        "youtube",
        "vod",
    )
    assert normal.spike_floor_s is None and normal.chat == () and normal.verdict is None and not normal.upload
    express = pipeline.RenderInput.from_payload(minimal, default_mode="express")
    assert (express.mode, express.lane) == ("express", "spike")
    with pytest.raises(ValueError):
        pipeline.RenderInput.from_payload({"source_path": "/data/a.mp4"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"lane": "daily"},
        {"mode": "turbo"},
        {"source_type": "clip"},
        {"platform": "kick"},
        {"window_end_s": 15.0},
        {"window_start_s": -1.0},
    ],
)
def test_render_input_rejects_bad_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        render_input(**overrides)


def test_transcription_modes() -> None:
    assert pipeline.transcription_modes("normal") == ("local", "hosted")
    assert pipeline.transcription_modes("express") == ("hosted", "local")


def test_next_budget_reset_is_shortly_after_utc_midnight() -> None:
    now = datetime(2026, 11, 17, 23, 59, 30, tzinfo=UTC)
    assert pipeline.next_budget_reset(now) == datetime(2026, 11, 18, 0, 2, 0, tzinfo=UTC)
    assert pipeline.next_budget_reset(datetime(2026, 11, 17, 0, 0, 0, tzinfo=UTC)).day == 18


def test_register_handlers_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from sixstars.services import worker

    monkeypatch.setattr(worker, "HANDLERS", {})
    pipeline.register_handlers()
    pipeline.register_handlers()
    assert {
        "clip.render": pipeline.handle_clip_render,
        "express.render": pipeline.handle_express_render,
    } == worker.HANDLERS
    monkeypatch.setattr(worker, "HANDLERS", {})
    pipeline.register_handlers(("clip.render",))
    assert list(worker.HANDLERS) == ["clip.render"]


def test_windows_stay_inside_the_file() -> None:
    cfg = FactoryConfig.from_mapping(settings_values(), "hourly")
    proposed, padded = pipeline._windows(render_input(pad_start_s=None, pad_end_s=None), cfg, 55.0)
    assert (proposed.start_s, proposed.end_s) == (15.0, 48.5)
    assert (padded.start_s, padded.end_s) == (0.0, 55.0), "context pad of 15 s, clamped to the file"
    proposed, padded = pipeline._windows(render_input(pad_start_s=20.0, pad_end_s=40.0), cfg, 100.0)
    assert (padded.start_s, padded.end_s) == (15.0, 48.5), "the pad never excludes the proposed window"
    proposed, padded = pipeline._windows(render_input(), cfg, 30.0)
    assert proposed.end_s == 30.0 and padded.end_s == 30.0


# ── CLI helpers ──────────────────────────────────────────────


def test_parse_box_and_overrides() -> None:
    assert commands.parse_box("1434, 0,486,273") == Box(1434, 0, 486, 273)
    for bad in ("1,2,3", "a,b,c,d", "0,0,0,10"):
        with pytest.raises(ValueError):
            commands.parse_box(bad)
    assert commands.parse_overrides(
        ["factory.crf=18", 'factory.font="Arial Black"', "factory.bleep=false"]
    ) == {
        "factory.crf": 18,
        "factory.font": "Arial Black",
        "factory.bleep": False,
    }
    with pytest.raises(ValueError):
        commands.parse_overrides(["factory.crf"])
    with pytest.raises(json.JSONDecodeError):
        commands.parse_overrides(["factory.font=Arial"])


def test_manual_writing_uses_the_window_and_options_as_given() -> None:
    lines = [
        Line(0, (Word("He", 1.0, 1.2), Word("did", 1.2, 1.4), Word("NOT", 1.4, 1.9))),
        Line(1, (Word("no", 3.0, 3.2), Word("way", 3.2, 3.6))),
    ]
    item = WriterInput(
        creator="x", platform="youtube", stream_title="", lines=lines, window_s=40.0,
        proposed=ClipWindow(1.0, 31.0), spike_floor_s=1.0, min_len_s=20.0, max_len_s=60.0,
    )  # fmt: skip
    writing = commands.manual_writing(item, "He did NOT expect that", ["not", "way", "absent"])
    assert (writing.start_s, writing.end_s, writing.hook) == (1.0, 31.0, "He did NOT expect that")
    assert writing.keyword_map() == {0: ["not"], 1: ["way"]} and not writing.reject
    assert commands.manual_writing(item, "", []).hook == "Watch this"


# ── ffprobe parsing ──────────────────────────────────────────


def test_parse_probe() -> None:
    info = tools.parse_probe(
        {
            "streams": [
                {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000"},
                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                 "avg_frame_rate": "60/1", "duration": "79.9"},
                {"codec_type": "video", "codec_name": "mjpeg", "width": 320, "height": 180},
            ],
            "format": {"duration": "80.000000"},
        }
    )  # fmt: skip
    assert info == tools.MediaInfo(
        width=1920, height=1080, duration_s=80.0, has_video=True, has_audio=True, video_codec="h264",
        audio_codec="aac", fps=60.0, sample_rate=48000,
    )  # fmt: skip
    audio_only = tools.parse_probe({"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {}})
    assert not audio_only.has_video and audio_only.width == 0 and audio_only.duration_s == 0.0
    odd = tools.parse_probe(
        {"streams": [{"codec_type": "video", "avg_frame_rate": "0/0", "duration": "N/A"}]}
    )
    assert odd.fps == 0.0 and odd.duration_s == 0.0


def test_run_reports_failures_and_timeouts() -> None:
    import sys

    assert tools.run([sys.executable, "-c", "print('ok')"], timeout_s=30, what="echo").stdout.strip() == b"ok"
    with pytest.raises(tools.FfmpegError, match="exit code 3: .*boom"):
        tools.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
            timeout_s=30,
            what="x",
        )
    with pytest.raises(tools.FfmpegError, match="timed out"):
        tools.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=0.2, what="sleep")
    with pytest.raises(tools.FfmpegError, match="could not start"):
        tools.run(["no-such-binary-6stars"], timeout_s=5, what="missing")


# ── emoji ────────────────────────────────────────────────────


def test_emoji_runs_are_drawn_in_the_bundled_emoji_font() -> None:
    assert ass_text_emoji("New guy's excuse 😭") == "New guy's excuse {\\fnNoto Emoji}😭{\\fn}"
    assert ass_text_emoji("😳💀 wow ⚠️!") == "{\\fnNoto Emoji}😳💀{\\fn} wow {\\fnNoto Emoji}⚠️{\\fn}!"
    assert ass_text_emoji("plain {text}") == "plain \\{text\\}", "text without emoji is only escaped"
    assert "\\fn" not in ass_text_emoji("100% · LIVE — né"), "punctuation and accents stay in the text font"


# ── Storage client ───────────────────────────────────────────

BASE = "https://proj.supabase.test"


def make_storage() -> storage.ClipStorage:
    return storage.ClipStorage(supabase_url=BASE + "/", service_role_key="service-key")


def test_clip_key_and_split_key() -> None:
    when = datetime(2026, 11, 17, 23, 30, tzinfo=UTC)
    assert storage.clip_key("abc", when, "staging") == "clips/staging/2026/11/17/abc.mp4"
    assert storage.split_key("clips/staging/2026/11/17/abc.mp4") == ("clips", "staging/2026/11/17/abc.mp4")
    for bad in ("clips", "/clips/", ""):
        with pytest.raises(ValueError):
            storage.split_key(bad)


@respx.mock
def test_upload_sends_the_file_with_upsert(tmp_path: Path) -> None:
    clip = tmp_path / "c1.mp4"
    clip.write_bytes(b"video-bytes")
    route = respx.post(f"{BASE}/storage/v1/object/clips/staging/2026/11/17/c1.mp4").mock(
        return_value=httpx.Response(200, json={"Key": "clips/staging/2026/11/17/c1.mp4", "Id": "x"})
    )
    key = make_storage().upload(clip, "clips/staging/2026/11/17/c1.mp4")
    assert key == "clips/staging/2026/11/17/c1.mp4"
    request = route.calls.last.request
    assert (
        request.headers["authorization"] == "Bearer service-key"
        and request.headers["apikey"] == "service-key"
    )
    assert request.headers["x-upsert"] == "true"
    body = request.read()
    assert b"video-bytes" in body and b"video/mp4" in body and b'name="cacheControl"' in body


@respx.mock
def test_upload_retries_a_server_error_then_raises(tmp_path: Path, sleeps: list[float]) -> None:
    clip = tmp_path / "c1.mp4"
    clip.write_bytes(b"x")
    route = respx.post(f"{BASE}/storage/v1/object/clips/a/c1.mp4").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"Key": "clips/a/c1.mp4"})]
    )
    assert make_storage().upload(clip, "clips/a/c1.mp4") == "clips/a/c1.mp4"
    assert route.call_count == 2 and len(sleeps) == 1
    respx.post(f"{BASE}/storage/v1/object/clips/a/big.mp4").mock(
        return_value=httpx.Response(413, json={"message": "The object exceeded the maximum allowed size"})
    )
    with pytest.raises(ApiError, match="maximum allowed size"):
        make_storage().upload(clip, "clips/a/big.mp4")


@respx.mock
def test_delete_and_signed_url() -> None:
    delete = respx.delete(f"{BASE}/storage/v1/object/clips").mock(
        side_effect=[httpx.Response(200, json=[{"name": "a/c1.mp4"}]), httpx.Response(200, json=[])]
    )
    store = make_storage()
    assert store.delete("clips/a/c1.mp4") is True
    assert json.loads(delete.calls.last.request.content) == {"prefixes": ["a/c1.mp4"]}
    assert store.delete("clips/a/c1.mp4") is False
    sign = respx.post(f"{BASE}/storage/v1/object/sign/clips/a/c1.mp4").mock(
        return_value=httpx.Response(200, json={"signedURL": "/object/sign/clips/a/c1.mp4?token=t"})
    )
    assert store.signed_url("clips/a/c1.mp4", 600) == f"{BASE}/storage/v1/object/sign/clips/a/c1.mp4?token=t"
    assert json.loads(sign.calls.last.request.content) == {"expiresIn": 600}
    respx.post(f"{BASE}/storage/v1/object/sign/clips/a/none.mp4").mock(
        return_value=httpx.Response(200, json={})
    )
    with pytest.raises(storage.StorageError):
        store.signed_url("clips/a/none.mp4")


def test_storage_needs_its_configuration() -> None:
    store = storage.ClipStorage()
    assert not store.is_configured()
    with pytest.raises(NotConfiguredError):
        store.signed_url("clips/a/c1.mp4")
    assert make_storage().is_configured()


def test_fake_storage_keeps_objects(tmp_path: Path) -> None:
    clip = tmp_path / "c1.mp4"
    clip.write_bytes(b"abc")
    fake = storage.FakeClipStorage()
    assert fake.upload(clip, "clips/test/c1.mp4") == "clips/test/c1.mp4"
    assert fake.objects == {"clips/test/c1.mp4": b"abc"}
    assert "clips/test/c1.mp4" in fake.signed_url("clips/test/c1.mp4")
    assert fake.delete("clips/test/c1.mp4") and not fake.delete("clips/test/c1.mp4")


# ── CLI ──────────────────────────────────────────────────────


def test_clip_commands_report_usage_errors(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from sixstars.cli import app

    runner = CliRunner()
    no_source = runner.invoke(app, ["clip", "render", "spike-1"])
    assert no_source.exit_code == 2 and "--source is needed" in no_source.output
    missing = runner.invoke(app, ["clip", "render", "spike-1", "--source", str(tmp_path / "none.mp4")])
    assert missing.exit_code == 2 and "no such file" in missing.output
    base = [
        "clip",
        "render-file",
        str(tmp_path / "none.mp4"),
        "--start",
        "1",
        "--end",
        "20",
        "--creator",
        "x",
    ]
    assert runner.invoke(app, base).exit_code == 2
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    bad_lane = runner.invoke(app, [*base[:2], str(video), *base[3:], "--lane", "daily"])
    assert bad_lane.exit_code == 2 and "lane must be one of" in bad_lane.output
    bad_set = runner.invoke(app, [*base[:2], str(video), *base[3:], "--set", "factory.crf"])
    assert bad_set.exit_code == 2 and "key=json" in bad_set.output


def test_move_into_place_also_works_across_file_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished = tmp_path / "scratch" / "render.mp4"
    finished.parent.mkdir()
    finished.write_bytes(b"clip")
    target = tmp_path / "ready dir" / "c1.mp4"
    pipeline._move_into_place(finished, target)
    assert target.read_bytes() == b"clip" and not finished.exists()

    finished.write_bytes(b"clip 2")
    real_replace = pipeline.os.replace

    def replace_on_one_file_system(source: Path, destination: Path) -> None:
        if Path(source).parent != Path(destination).parent:
            raise OSError(18, "Invalid cross-device link")
        real_replace(source, destination)

    monkeypatch.setattr(pipeline.os, "replace", replace_on_one_file_system)
    pipeline._move_into_place(finished, target)
    assert target.read_bytes() == b"clip 2" and not finished.exists()
    assert list(target.parent.iterdir()) == [target], "no partial file is left behind"

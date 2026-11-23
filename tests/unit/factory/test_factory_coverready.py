"""Cover-ready video (cover engine C1, docs/29a stage 0): pure maths, captions, layout and render spec."""

from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from sixstars.factory import coverready
from sixstars.factory.bleep import Span
from sixstars.factory.coverready import CoverVideo, CoverVideoRules
from sixstars.factory.layout import (
    C_FACECAM_H,
    REF_HEIGHT,
    REF_WIDTH,
    Box,
    FaceFraming,
    GameLift,
    Geometry,
    LayoutA,
    face_crop_box,
    gameplay_band,
    layout_filters,
    lift_filter,
)
from sixstars.factory.lines import Chunk, Line, Word
from sixstars.factory.params import FactoryParams, FontFiles
from sixstars.factory.render import (
    RenderSpec,
    SourceInfo,
    build_audio_graph,
    build_render_command,
    build_video_filters,
)
from sixstars.factory.subtitles import SubtitleStyle, chunk_events, phrase_chunks
from sixstars.factory.window import ClipWindow

FACECAM = Box(1440, 720, 480, 360)


# ── rules ────────────────────────────────────────────────────


def test_rules_defaults_and_clamps() -> None:
    assert CoverVideoRules.from_flag({"enabled": True}) == CoverVideoRules()
    rules = CoverVideoRules.from_flag(
        {"facecam_h": 400, "face_target": 2, "phrase_words": 1, "dark_gamma": 9, "open_s": "x"}
    )
    assert rules.facecam_h == C_FACECAM_H  # never smaller than today's panel
    assert rules.face_target == 0.8
    assert rules.phrase_words == 2
    assert rules.dark_gamma == 1.6
    assert rules.open_s == 2.0


# ── peak ─────────────────────────────────────────────────────


def test_peak_prefers_the_anchor_then_the_payoff_then_chat() -> None:
    chat = [9.0, 9.2, 9.4, 9.6]  # busiest second 9 -> 6.5 after the 3 s lag
    assert coverready.choose_peak(20.0, anchor_s=4.0, payoff_s=8.0, chat_times=chat) == (4.0, "anchor")
    assert coverready.choose_peak(20.0, anchor_s=25.0, payoff_s=8.0, chat_times=chat) == (8.0, "payoff")
    assert coverready.choose_peak(20.0, anchor_s=None, payoff_s=-1.0, chat_times=chat) == (6.5, "chat")
    assert coverready.choose_peak(20.0, chat_times=[9.0, 9.5]) == (None, "")


# ── windows ──────────────────────────────────────────────────


def test_phrase_windows_merge_and_clip() -> None:
    assert coverready.phrase_windows(20.0, 10.0, 2.0, 1.0) == ((0.0, 2.0), (9.0, 11.0))
    assert coverready.phrase_windows(20.0, 2.5, 2.0, 1.0) == ((0.0, 3.5),)
    assert coverready.phrase_windows(20.0, 19.5, 2.0, 1.0) == ((0.0, 2.0), (18.5, 20.0))
    assert coverready.phrase_windows(1.5, None, 2.0, 1.0) == ((0.0, 1.5),)
    assert coverready.in_windows(1.99, ((0.0, 2.0),)) and not coverready.in_windows(2.0, ((0.0, 2.0),))


# ── face crop ────────────────────────────────────────────────


@pytest.mark.parametrize("panel_h", [672, 768, 770])
def test_face_crop_centres_the_face_at_the_target_share(panel_h: int) -> None:
    face = Box(1640, 760, 70, 80)  # small face in the top half of the webcam box
    crop = face_crop_box(FaceFraming(face, 0.40, 120), FACECAM, REF_WIDTH, panel_h)
    assert crop.x >= FACECAM.x and crop.y >= FACECAM.y
    assert crop.x + crop.w <= FACECAM.x + FACECAM.w and crop.y + crop.h <= FACECAM.y + FACECAM.h
    assert crop.w % 2 == 0 and crop.h % 2 == 0 and crop.x % 2 == 0 and crop.y % 2 == 0
    assert crop.w / crop.h == pytest.approx(REF_WIDTH / panel_h, rel=0.02)
    assert coverready.face_share(face.h, crop.h) == pytest.approx(0.40, abs=0.02)
    # The whole face is inside the crop, its centre near the middle.
    assert crop.x <= face.x and face.x + face.w <= crop.x + crop.w
    assert crop.y <= face.y and face.y + face.h <= crop.y + crop.h
    assert abs((crop.x + crop.w / 2) - (face.x + face.w / 2)) <= 2


def test_face_crop_respects_the_minimum_and_the_box() -> None:
    tiny = Box(1660, 900, 10, 12)
    crop = face_crop_box(FaceFraming(tiny, 0.40, 180), FACECAM, REF_WIDTH, 768)
    assert crop.h >= 178  # min_crop_px (even)
    huge = Box(1450, 730, 460, 340)
    crop = face_crop_box(FaceFraming(huge, 0.40, 180), FACECAM, REF_WIDTH, 768)
    assert crop.w <= FACECAM.w and crop.h <= FACECAM.h  # capped at the box
    edge = Box(1890, 1050, 30, 28)  # face in the bottom-right corner: the crop slides, never leaves the box
    crop = face_crop_box(FaceFraming(edge, 0.40, 120), FACECAM, REF_WIDTH, 768)
    assert crop.x + crop.w <= FACECAM.x + FACECAM.w and crop.y + crop.h <= FACECAM.y + FACECAM.h


def test_median_box_and_sample_times() -> None:
    boxes = [Box(10, 10, 20, 20), Box(12, 11, 22, 21), Box(100, 100, 50, 50)]
    assert coverready.median_box(boxes) == Box(12, 11, 22, 21)
    assert coverready.median_box([]) is None
    times = coverready.sample_times(20.0, 7.3, 5)
    assert times == sorted(times) and 7.3 in times and 1.0 in times and all(0 <= t < 20 for t in times)


# ── lift ─────────────────────────────────────────────────────


def test_lift_only_for_dark_openings() -> None:
    rules = CoverVideoRules()
    assert coverready.lift_for(80.0, rules, 20.0) is None
    assert coverready.lift_for(None, rules, 20.0) is None
    assert coverready.lift_for(30.0, rules, 20.0) == GameLift(1.15, 1.5, 0.5)
    assert coverready.lift_for(30.0, rules, 1.0) == GameLift(1.15, 1.0, 0.5)


def test_lift_filter_eases_back_by_until() -> None:
    text = lift_filter(GameLift(1.15, 1.5, 0.5))
    assert text.startswith("eq=gamma=") and "eval=frame" in text
    assert "clip((1.5-t)/0.5\\,0\\,1)" in text  # commas escaped for the filter graph


# ── json ─────────────────────────────────────────────────────


def test_cover_json_round_trip_and_shift() -> None:
    cover = CoverVideo(
        facecam_h=768,
        framing=FaceFraming(Box(1640, 760, 70, 80), 0.4, 180),
        phrase_windows=((0.0, 2.0), (9.0, 11.0)),
        lift=GameLift(1.15, 1.5, 0.5),
        peak_s=10.0,
        peak_source="anchor",
    )
    assert CoverVideo.from_json(cover.to_json()) == cover
    assert CoverVideo.from_json(None) is None
    assert CoverVideo.from_json({"facecam_h": "x"}) is None
    story = coverready.shifted(cover, CoverVideoRules(), 5.0, 40.0)
    assert story.peak_s == 15.0 and story.phrase_windows == ((0.0, 2.0), (14.0, 16.0))
    assert story.lift is None and story.framing == cover.framing
    gone = coverready.shifted(cover, CoverVideoRules(), -12.0, 40.0)
    assert gone.peak_s is None and gone.phrase_windows == ((0.0, 2.0),)


# ── layout and gate band ─────────────────────────────────────


def test_geometry_with_a_taller_facecam() -> None:
    default = Geometry.for_canvas(REF_WIDTH, REF_HEIGHT)
    tall = Geometry.for_canvas(REF_WIDTH, REF_HEIGHT, 768)
    assert default.c_facecam_h == 672 and tall.c_facecam_h == 768
    assert tall.c_gameplay_h == REF_HEIGHT - 768 and tall.c_seam_y == 765
    assert Geometry.for_canvas(720, 1280, 768).c_facecam_h == 512


def test_gameplay_band_moves_down_under_a_taller_facecam() -> None:
    assert gameplay_band("C") == gameplay_band("C", 672)
    top, bottom = gameplay_band("C", 768)
    assert top * REF_HEIGHT == pytest.approx(1056) and bottom == gameplay_band("C")[1]
    assert gameplay_band("A", 768) == gameplay_band("A")


def test_layout_filters_use_the_face_crop_and_the_lift() -> None:
    layout = LayoutA(facecam_box=FACECAM, cam_top=True)
    geometry = Geometry.for_canvas(REF_WIDTH, REF_HEIGHT, 768)
    framing = FaceFraming(Box(1640, 760, 70, 80), 0.4, 120)
    plain = layout_filters(layout, source_w=1920, source_h=1080, geometry=geometry, fps=30, duration_s=20)
    framed = layout_filters(
        layout,
        source_w=1920,
        source_h=1080,
        geometry=geometry,
        fps=30,
        duration_s=20,
        framing=framing,
        lift=GameLift(1.15, 1.5, 0.5),
    )
    crop = face_crop_box(framing, FACECAM, REF_WIDTH, 768)
    face_chain = next(chain for chain in framed if chain.startswith("[v_face_in]"))
    assert f"crop=w={crop.w}:h={crop.h}:x={crop.x}:y={crop.y}" in face_chain
    assert "scale=w=1080:h=768" in face_chain
    game_chain = next(chain for chain in framed if chain.startswith("[v_game_in]"))
    assert "eq=gamma=" in game_chain
    assert not any("eq=gamma=" in chain for chain in plain)


# ── captions ─────────────────────────────────────────────────


def _texts(line_words: dict[int, list[str]]) -> object:
    def text_of(line_index: int, words: tuple[int, ...]) -> str:
        return " ".join(line_words[line_index][i] for i in words)

    return text_of


def test_phrase_chunks_merge_inside_windows_only() -> None:
    words = {0: ["oh", "my", "god", "they", "are", "here", "now"], 1: ["run", "run"]}
    chunks = [
        Chunk(0, (0, 1), 0.0, 0.5),
        Chunk(0, (2, 3), 0.5, 1.0),
        Chunk(0, (4, 5), 1.0, 1.5),
        Chunk(0, (6,), 1.5, 1.8),
        Chunk(1, (0,), 5.0, 5.3, repeat=2),
        Chunk(1, (0,), 8.0, 8.3),
    ]
    out = phrase_chunks(
        chunks,
        windows=((0.0, 2.0),),
        max_words=6,
        max_chars=26,
        text_of=_texts(words),  # type: ignore[arg-type]
        fits=lambda _text: True,
    )
    assert out[0] == Chunk(0, (0, 1, 2, 3, 4, 5), 0.0, 1.5, phrase=True)  # six words, the seventh waits
    assert out[1] == Chunk(0, (6,), 1.5, 1.8, phrase=True)
    assert out[2].repeat == 2 and not out[2].phrase  # X2 chunks stay as they are
    assert out[3] == chunks[5]  # outside the window


def test_phrase_chunks_respect_chars_and_one_row() -> None:
    words = {0: ["absolutely", "unbelievable", "situation", "here"]}
    chunks = [Chunk(0, (0,), 0.0, 0.4), Chunk(0, (1,), 0.4, 0.8), Chunk(0, (2,), 0.8, 1.2)]
    out = phrase_chunks(
        chunks,
        windows=((0.0, 2.0),),
        max_words=6,
        max_chars=26,
        text_of=_texts(words),  # type: ignore[arg-type]
        fits=lambda _text: True,
    )
    assert [c.words for c in out] == [(0, 1), (2,)]  # 23 chars, then 33 > 26
    narrow = phrase_chunks(
        chunks,
        windows=((0.0, 2.0),),
        max_words=6,
        max_chars=99,
        text_of=_texts(words),  # type: ignore[arg-type]
        fits=lambda text: len(text) < 12,
    )
    assert [c.words for c in narrow] == [(0,), (1,), (2,)]


def test_chunk_events_show_a_still_phrase_in_the_windows(params: FactoryParams) -> None:
    line = Line(
        0,
        tuple(
            Word(w, i * 0.4, i * 0.4 + 0.35)
            for i, w in enumerate(["we", "got", "six", "stars", "bro", "run"])
        ),
    )
    late = Line(1, (Word("later", 6.0, 6.4), Word("words", 6.4, 6.8)))
    style = SubtitleStyle(size=150, chunk_words=2, chunk_chars=14, caps=True, active_color="#FFD400")
    plain = chunk_events(
        [line, late], layout="C", params=params, duration_s=10.0, keywords={}, profanity=None, style=style
    )
    ready = chunk_events(
        [line, late],
        layout="C",
        params=params,
        duration_s=10.0,
        keywords={},
        profanity=None,
        style=replace(style, phrase_windows=((0.0, 2.0),)),
    )
    first = ready[0]
    assert "WE" in first and "RUN" in first and "\\t(" not in first  # whole phrase, no pop
    assert ready[0].split(",")[1] == "0:00:00.00"
    assert len(plain) > len(ready)
    assert ready[-1] == plain[-1] or "LATER" in ready[-2] + ready[-1]  # outside the window: unchanged look


# ── render spec: length, cuts and audio never change ─────────


def _spec(params: FactoryParams, **overrides: object) -> RenderSpec:
    values: dict[str, object] = {
        "source": SourceInfo(PurePosixPath("/data/vod/s.mp4"), 1920, 1080),
        "window": ClipWindow(12.0, 30.0),
        "layout": LayoutA(facecam_box=FACECAM, cam_top=True),
        "ass_path": PurePosixPath("/data/render/c1/clip.ass"),
        "output_path": PurePosixPath("/data/ready/c1.mp4"),
        "series_no": 7,
        "params": params,
        "fonts": FontFiles(fonts_dir=PurePosixPath("/app/assets/fonts")),
        "bleeps": (Span(4.0, 4.5),),
    }
    values.update(overrides)
    return RenderSpec(**values)  # type: ignore[arg-type]


def test_cover_ready_spec_keeps_length_cuts_and_audio(params: FactoryParams) -> None:
    plain = _spec(params)
    ready = _spec(
        params,
        facecam_ref_h=768,
        framing=FaceFraming(Box(1640, 760, 70, 80), 0.4, 180),
        lift=GameLift(1.15, 1.5, 0.5),
    )
    assert build_audio_graph(ready) == build_audio_graph(plain)
    assert (
        ready.clip_duration_s == plain.clip_duration_s and ready.output_duration_s == plain.output_duration_s
    )
    plain_cmd, ready_cmd = build_render_command(plain), build_render_command(ready)
    assert plain_cmd[: plain_cmd.index("-filter_complex")] == ready_cmd[: ready_cmd.index("-filter_complex")]
    video = build_video_filters(ready)
    assert any("vstack" in chain for chain in video)
    assert any("trim=duration=18" in chain for chain in video)

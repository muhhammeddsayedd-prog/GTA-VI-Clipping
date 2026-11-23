"""Render v2 R2 (D119): the face-aware cam panel (`layout.cam_window`, `cam_panel_filters`), the face detector
helpers (`factory.faces`) and the no-face fallback (`pipeline.frame_cam`). Pure, except the YuNet smoke test
(OpenCV on a blank image, no network)."""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sixstars.factory import faces
from sixstars.factory.faces import Face, FaceDetectRules, FakeFaceDetector
from sixstars.factory.layout import (
    Box,
    CamWindow,
    Geometry,
    LayoutG,
    LayoutG0,
    LayoutV2Rules,
    cam_panel_filters,
    cam_window,
    layout_filters,
)
from sixstars.factory.window import ClipWindow

REPO = Path(__file__).resolve().parents[3]
RULES = LayoutV2Rules(enabled=True, scaler="lanczos")
PANEL_W, PANEL_H = 1080, 720
EDGE_PX = 4  # the R5 gate fails a face within 4 px of the panel edge

# Cam boxes of production creator_layouts (development-plan/M19 section 6), source 1920x1080, with a face
# where a streamer's face sits in such a cam.
CASES = {
    "jerma_square": (Box(0, 651, 248, 246), Box(80, 700, 80, 100)),
    "missmikkaa_tall": (Box(1402, 472, 518, 608), Box(1590, 560, 150, 190)),
    "kai_wide": (Box(0, 432, 393, 222), Box(150, 470, 90, 115)),
    "cam_16_9": (Box(1440, 810, 480, 270), Box(1620, 870, 100, 120)),
    "face_at_box_edge": (Box(0, 651, 248, 246), Box(0, 651, 70, 90)),
    "tiny_face": (Box(0, 432, 393, 222), Box(180, 520, 20, 26)),
    "face_fills_the_cam": (Box(1402, 472, 518, 608), Box(1410, 480, 500, 590)),
}


def _frame(name: str) -> tuple[Box, Box, CamWindow]:
    cam, face = CASES[name]
    return cam, face, cam_window(face, cam, PANEL_W, PANEL_H, RULES, 1920, 1080)


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_whole_face_is_in_the_panel_with_room_around_it(name: str) -> None:
    _cam, _face, framing = _frame(name)
    f = framing.face
    assert f.x >= EDGE_PX and f.y >= EDGE_PX, name
    assert f.x + f.w <= PANEL_W - EDGE_PX and f.y + f.h <= PANEL_H - EDGE_PX, name


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_face_height_share_stays_within_the_limits(name: str) -> None:
    _cam, _face, framing = _frame(name)
    assert RULES.cam_face_h_min - 0.01 <= framing.face_share <= RULES.cam_face_h_max + 0.01, name


@pytest.mark.parametrize("name", sorted(CASES))
def test_only_cam_box_pixels_are_taken_and_the_crop_is_even(name: str) -> None:
    cam, _face, framing = _frame(name)
    c = framing.crop
    assert cam.x <= c.x and cam.y <= c.y and c.x + c.w <= cam.x + cam.w and c.y + c.h <= cam.y + cam.h
    assert all(v % 2 == 0 for v in (c.x, c.y, c.w, c.h)), c
    p = framing.placed
    assert p.x >= 0 and p.y >= 0 and p.x + p.w <= PANEL_W and p.y + p.h <= PANEL_H


def test_a_roomy_cam_is_framed_without_fill_at_the_middle_share() -> None:
    _cam, _face, framing = _frame("cam_16_9")
    assert framing.fill is False and framing.placed == Box(0, 0, PANEL_W, PANEL_H)
    assert framing.face_share == pytest.approx(0.5, abs=0.02)
    centre_x = framing.face.x + framing.face.w / 2
    assert centre_x == pytest.approx(PANEL_W / 2, abs=4), "the face is centred"
    centre_y = framing.face.y + framing.face.h / 2
    assert centre_y == pytest.approx(RULES.cam_face_center_y * PANEL_H, abs=4)


def test_a_narrow_cam_takes_the_largest_face_share_it_can_hold() -> None:
    _cam, _face, framing = _frame("missmikkaa_tall")  # 518 px wide: the 0.5 window would need 570 px
    assert 0.5 < framing.face_share <= RULES.cam_face_h_max + 0.01
    assert framing.crop.w >= 500, "almost the whole cam width is shown sharp"


def test_a_small_square_cam_is_framed_on_the_face_with_a_blurred_fill() -> None:
    _cam, _face, framing = _frame("jerma_square")
    assert framing.fill is True and framing.placed != Box(0, 0, PANEL_W, PANEL_H)
    assert framing.face_share == pytest.approx(RULES.cam_face_h_max, abs=0.01)


def test_a_face_too_wide_for_the_middle_share_is_never_cut() -> None:
    cam = Box(0, 0, 400, 300)
    wide = cam_window(Box(20, 60, 360, 120), cam, PANEL_W, PANEL_H, RULES, 1920, 1080)
    assert wide.face.x >= 0 and wide.face.x + wide.face.w <= PANEL_W
    assert wide.face_share < RULES.cam_face_h_min, "a smaller face, not a cut one"


def test_a_face_outside_the_cam_box_is_refused() -> None:
    with pytest.raises(ValueError):
        cam_window(Box(900, 100, 50, 50), Box(0, 0, 300, 300), PANEL_W, PANEL_H, RULES)


# ── the panel filter ─────────────────────────────────────────


GEOMETRY = Geometry.for_canvas(1080, 1920, RULES)


def test_a_framed_panel_without_fill_is_one_sharpened_crop() -> None:
    cam, _face, framing = _frame("cam_16_9")
    chains = cam_panel_filters(
        cam, framing, GEOMETRY, RULES, source_w=1920, source_h=1080, scaler="lanczos", source="in", out="cam"
    )
    assert len(chains) == 1 and chains[0].startswith("[in]crop=")
    assert "unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=0.5" in chains[0]
    assert "scale=w=1080:h=720:flags=lanczos" in chains[0] and chains[0].endswith("[cam]")


def test_a_framed_panel_with_fill_lays_the_sharp_crop_over_a_dark_blur() -> None:
    cam, _face, framing = _frame("jerma_square")
    chains = cam_panel_filters(
        cam, framing, GEOMETRY, RULES, source_w=1920, source_h=1080, scaler="lanczos", source="in", out="cam"
    )
    joined = ";".join(chains)
    assert chains[0] == "[in]split=2[cam_fill_in][cam_sharp_in]"
    assert "boxblur" in joined and "drawbox=color=black@0.3" in joined
    assert f"overlay=x={framing.placed.x}:y={framing.placed.y}[cam]" in joined
    no_sharpen = cam_panel_filters(
        cam,
        framing,
        GEOMETRY,
        LayoutV2Rules(cam_unsharp=0),
        source_w=1920,
        source_h=1080,
        scaler="lanczos",
        source="in",
        out="cam",
    )
    assert "unsharp" not in ";".join(no_sharpen)


def test_layout_g_uses_the_face_framing_when_it_has_a_face() -> None:
    cam, face = CASES["kai_wide"]
    plain = ";".join(
        layout_filters(LayoutG(cam), source_w=1920, source_h=1080, geometry=GEOMETRY, fps=30, v2=RULES)
    )
    framed = ";".join(
        layout_filters(
            LayoutG(cam, face_box=face), source_w=1920, source_h=1080, geometry=GEOMETRY, fps=30, v2=RULES
        )
    )
    assert "unsharp" not in plain and "unsharp" in framed
    assert framed.endswith("[v_face][v_game]vstack=inputs=2[v_layout]")


# ── face detection helpers ───────────────────────────────────


def test_the_seeded_face_rules_parse() -> None:
    text = (REPO / "supabase" / "migrations" / "0054_render_v2_settings.sql").read_text(encoding="utf-8")
    seeded = json.loads(text.split("('facecam.detect',", 1)[1].split("'", 2)[1])
    assert FaceDetectRules.from_setting(seeded) == FaceDetectRules()
    assert FaceDetectRules.from_setting({"check_frames": 2, "min_face_hits": 3}) == FaceDetectRules()
    assert FaceDetectRules.from_setting({"check_frames": 8}).check_frames == 8


def test_sample_times_spread_over_the_window() -> None:
    assert faces.sample_times(ClipWindow(10.0, 20.0), 5) == pytest.approx([11, 13, 15, 17, 19])


def test_the_median_face_uses_the_largest_scored_face_of_each_frame() -> None:
    a, b, c = Box(10, 10, 40, 50), Box(12, 11, 42, 52), Box(100, 100, 60, 70)
    per_frame = [
        [Face(a, 0.9)],
        [Face(c, 0.5), Face(b, 0.8)],  # the large face scores too low; the next one counts
        [],
        [Face(Box(11, 12, 41, 51), 0.95)],
    ]
    box, hits = faces.median_face(per_frame, 0.6)
    assert hits == 3 and box == Box(11, 11, 41, 51)
    assert faces.median_face([[], []], 0.6) == (None, 0)


def test_clip_box_keeps_boxes_inside_the_image() -> None:
    assert faces.clip_box(-5, -5, 20, 20, 100, 100) == Box(0, 0, 15, 15)
    assert faces.clip_box(95, 95, 20, 20, 100, 100) == Box(95, 95, 5, 5)
    assert faces.clip_box(200, 200, 5, 5, 100, 100) is None


def test_the_frame_command_crops_and_pipes_raw_bgr() -> None:
    command = faces.frame_command(Path("in.mp4"), 3.25, Box(10, 20, 300, 200), ffmpeg="ff")
    assert command[command.index("-ss") + 1] == "3.250"
    assert "crop=300:200:10:20" in command and command[-3:] == ["-pix_fmt", "bgr24", "-"]


def _fake_grab(count: int, size: tuple[int, int]) -> Any:
    def grab(source: Path, times: list[float], crop: Box, *, ffmpeg: str) -> list[Any]:
        assert (crop.w, crop.h) == size
        return [np.zeros((crop.h, crop.w, 3), dtype=np.uint8) for _ in times[:count]]

    return grab


def test_find_cam_face_returns_the_face_in_source_pixels() -> None:
    cam = Box(1402, 472, 518, 608)
    detector = FakeFaceDetector(faces=[Face(Box(188, 88, 150, 190), 0.92)])
    check = faces.find_cam_face(
        Path("x.mp4"),
        ClipWindow(0, 10),
        cam,
        FaceDetectRules(),
        detector=detector,
        grab=_fake_grab(5, (518, 608)),
    )
    assert check.face_box == Box(1590, 560, 150, 190) and (check.hits, check.frames) == (5, 5)
    assert detector.seen == [(518, 608)] * 5


def test_the_bundled_yunet_model_loads_and_finds_no_face_in_a_blank_frame() -> None:
    detector = faces.YuNetDetector(REPO / "assets" / faces.YUNET_MODEL, 0.6)
    assert detector.detect(np.full((246, 248, 3), 127, dtype=np.uint8)) == []
    assert (
        (REPO / "assets" / "models" / "LICENSE-yunet.txt")
        .read_text(encoding="utf-8")
        .startswith("MIT License")
    )


# ── the fallback to G0 ───────────────────────────────────────


def _job(layout: LayoutG | LayoutG0, detector: FakeFaceDetector, enabled: bool = True) -> Any:
    from sixstars.factory.pipeline import RenderJob

    return RenderJob(
        source_path=Path("src.mp4"),
        media=None,  # type: ignore[arg-type]
        window=ClipWindow(0, 10),
        lines=(),
        layout=layout,
        params=None,  # type: ignore[arg-type]
        assets=None,  # type: ignore[arg-type]
        output_path=Path("out.mp4"),
        scratch_dir=Path("scratch"),
        series_no=1,
        layout_v2=LayoutV2Rules(enabled=enabled),
        face_detector=detector,
    )


@pytest.fixture
def fake_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        faces,
        "grab_frames",
        lambda source, times, crop, *, ffmpeg: [np.zeros((crop.h, crop.w, 3), np.uint8)] * 5,
    )


def test_a_cam_with_a_face_is_framed_around_it(fake_frames: None) -> None:
    from sixstars.factory.pipeline import frame_cam

    cam = Box(0, 432, 393, 222)
    job, check = frame_cam(_job(LayoutG(cam), FakeFaceDetector([Face(Box(150, 38, 90, 115), 0.9)])))
    assert job.layout == LayoutG(cam, face_box=Box(150, 470, 90, 115))
    assert check is not None and check["hits"] == 5 and "fallback" not in check


def test_a_cam_without_a_face_is_drawn_as_g0_never_with_an_empty_panel(fake_frames: None) -> None:
    from sixstars.factory.pipeline import frame_cam

    overlays = (Box(1500, 0, 400, 300),)
    job, check = frame_cam(_job(LayoutG(Box(958, 93, 322, 184), overlay_boxes=overlays), FakeFaceDetector()))
    assert job.layout == LayoutG0(overlays)
    assert check is not None and check["fallback"] == "G0" and check["face_box"] is None


def test_no_check_when_the_switch_is_off_or_the_layout_is_not_g(fake_frames: None) -> None:
    from sixstars.factory.pipeline import frame_cam

    detector = FakeFaceDetector()
    job = _job(LayoutG(Box(0, 0, 300, 200)), detector, enabled=False)
    assert frame_cam(job) == (job, None)
    g0 = _job(LayoutG0(), detector)
    assert frame_cam(g0) == (g0, None) and detector.seen == []


def test_a_check_that_cannot_run_falls_back_to_g0(monkeypatch: pytest.MonkeyPatch) -> None:
    from sixstars.factory.pipeline import frame_cam

    def broken(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("face model missing")

    monkeypatch.setattr(faces, "find_cam_face", broken)
    job, check = frame_cam(_job(LayoutG(Box(0, 0, 300, 200)), FakeFaceDetector()))
    assert job.layout == LayoutG0() and check is not None and check["fallback"] == "G0"

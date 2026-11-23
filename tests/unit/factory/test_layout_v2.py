"""Render v2 R1 (D119, development-plan/M19): the geometry of layouts G and G0, the slide-clear gameplay crop,
their filter graphs, the master switch in `framed_layout`, the recipe and the gate band. Pure."""

import json
import re
from collections.abc import Callable
from pathlib import Path, PurePath

import pytest

from sixstars.factory.config import FactoryConfig
from sixstars.factory.layout import (
    REF_HEIGHT,
    Box,
    Geometry,
    LayoutA,
    LayoutB,
    LayoutCard,
    LayoutG,
    LayoutG0,
    LayoutMontage,
    LayoutPov,
    LayoutV2Rules,
    gameplay_band,
    gameplay_crop_v2,
    layout_filters,
    overlap_area,
)
from sixstars.factory.params import FactoryParams
from sixstars.factory.pipeline import framed_layout
from sixstars.factory.render import RenderSpec, SourceInfo, build_video_filters
from sixstars.factory.style import StyleRules
from sixstars.factory.styles import DEFAULT_STYLES
from sixstars.factory.variants import layout_from_recipe, recipe_json
from sixstars.factory.window import ClipWindow
from sixstars.gate.store import _layout_kind

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "supabase" / "migrations" / "0054_render_v2_settings.sql"
ON = LayoutV2Rules(enabled=True, scaler="lanczos")
GEOMETRY = Geometry.for_canvas(1080, 1920, ON)
# Source 1920x1080; the G gameplay crop is 972x1080 (aspect 1080:1200), centred at x=474.
W, H = 1920, 1080
G_W = 972
CENTRE = 474


def _crop(avoid: tuple[Box, ...], max_slide: float = 0.25, aspect_h: int = 1200) -> Box:
    return gameplay_crop_v2(avoid, 1080, aspect_h, W, H, max_slide)


# ── the setting ──────────────────────────────────────────────


def _migration_value(key: str) -> dict[str, object]:
    text = MIGRATION.read_text(encoding="utf-8")
    raw = text.split(f"('{key}',", 1)[1].split("'", 2)[1]
    value: dict[str, object] = json.loads(raw)
    return value


def test_the_seeded_setting_matches_the_documented_defaults_and_is_off() -> None:
    seeded = _migration_value("factory.layout_v2")
    rules = LayoutV2Rules.from_setting(seeded)
    assert rules == LayoutV2Rules(scaler="lanczos"), "only the scaler differs from ffmpeg's default"
    assert rules.enabled is False
    assert set(seeded) == set(LayoutV2Rules.__dataclass_fields__)


def test_the_other_render_v2_settings_are_seeded_as_specified() -> None:
    assert _migration_value("factory.text_v2")["mark_text"] == "6 Stars Clips"
    assert _migration_value("facecam.detect")["min_confidence"] == 0.75
    assert _migration_value("gate.render_v2")["min_game_share_g"] == 0.60


def test_a_bad_setting_gives_the_defaults_with_the_switch_off() -> None:
    assert LayoutV2Rules.from_setting(None) == LayoutV2Rules()
    assert LayoutV2Rules.from_setting({"enabled": True, "cam_h": 1900}) == LayoutV2Rules()
    assert LayoutV2Rules.from_setting({"enabled": True, "scaler": "nearest;x"}) == LayoutV2Rules()
    odd = LayoutV2Rules.from_setting({"enabled": "yes", "cam_h": 700.5, "max_slide": 0.1})
    assert odd.enabled is False and odd.cam_h == 720 and odd.max_slide == 0.1


def test_the_factory_config_reads_the_switch_and_the_scaler() -> None:
    off = FactoryConfig.offline("hourly")
    assert off.layout_v2 == LayoutV2Rules() and off.scaler == "bicubic", "before migration 0054"
    seeded = FactoryConfig.offline("hourly", {"factory.layout_v2": _migration_value("factory.layout_v2")})
    assert seeded.scaler == "lanczos" and seeded.layout_v2.enabled is False
    on = FactoryConfig.offline("hourly", {"factory.layout_v2": {"enabled": True, "cam_h": 800}})
    assert on.layout_v2.enabled and on.layout_v2.cam_h == 800


# ── geometry ─────────────────────────────────────────────────


def test_g_is_a_720_cam_over_1200_of_gameplay_and_g0_is_4_by_5_at_285() -> None:
    g = GEOMETRY
    assert (g.g_cam_h, g.g_game_h) == (720, 1200)
    assert (g.g0_game_h, g.g0_game_y) == (1350, 285)
    assert g.seam_y("G") == 720
    small = Geometry.for_canvas(720, 1280, ON)
    assert small.g_cam_h + small.g_game_h == 1280 and small.g_cam_h == 480
    assert small.g0_game_y + small.g0_game_h <= 1280


def test_the_gate_band_of_g_and_g0_lies_inside_their_gameplay() -> None:
    top, bottom = gameplay_band("G", ON)
    assert 720 / REF_HEIGHT < top < bottom <= 1.0
    top0, bottom0 = gameplay_band("G0", ON)
    assert 285 / REF_HEIGHT <= top0 < bottom0 <= (285 + 1350) / REF_HEIGHT
    taller = gameplay_band("G", LayoutV2Rules(cam_h=900))
    assert taller[0] > top and taller[1] > taller[0]


# ── gameplay crop ────────────────────────────────────────────


def test_nothing_to_avoid_keeps_the_centred_crop() -> None:
    assert _crop(()) == Box(CENTRE, 0, G_W, H)


def test_a_wide_cam_on_the_left_pushes_the_crop_right() -> None:
    cam = Box(0, 700, 600, 380)
    crop = _crop((cam,))
    assert crop.x == 600 and overlap_area(crop, cam) == 0


def test_a_cam_on_the_right_pushes_the_crop_left() -> None:
    cam = Box(1402, 472, 518, 608)  # MissMikkaa, production creator_layouts
    crop = _crop((cam,))
    assert crop.x == 430 and overlap_area(crop, cam) == 0


def test_a_cam_bottom_right_over_the_centre_moves_the_crop_as_far_as_needed() -> None:
    cam = Box(1100, 600, 820, 480)
    crop = _crop((cam,))
    assert crop.x == 128 and overlap_area(crop, cam) == 0


def test_a_chat_box_on_the_right_and_a_cam_on_the_left_leave_one_gap() -> None:
    cam, chat = Box(0, 700, 500, 380), Box(1500, 0, 420, 1080)
    crop = _crop((cam, chat))
    assert crop.x == 500 and overlap_area(crop, cam) == overlap_area(crop, chat) == 0


def test_with_nothing_clear_the_least_overlap_nearest_the_centre_wins() -> None:
    left, right = Box(0, 0, 700, 1080), Box(1300, 0, 620, 1080)
    crop = _crop((left, right))
    assert crop.x == CENTRE  # every x in 328..700 overlaps 372 px of width; the centre is nearest


def test_the_slide_never_goes_past_max_slide() -> None:
    cam = Box(1402, 472, 518, 608)
    crop = _crop((cam,), max_slide=0.01)  # reach 19 px: the crop cannot clear the cam
    assert crop.x == 456  # the even position closest to clearing it inside 455..493
    assert abs(crop.x - CENTRE) <= 0.01 * W + 1


@pytest.mark.parametrize("max_slide", [0.0, 0.07, 0.25, 0.5])
def test_offsets_are_even_and_the_crop_stays_in_the_frame(max_slide: float) -> None:
    for x in range(0, 1900, 37):
        crop = _crop((Box(x, 100, 20 + x % 300, 300),), max_slide=max_slide)
        assert crop.x % 2 == 0 and 0 <= crop.x <= W - crop.w and (crop.w, crop.h) == (G_W, H)


# ── filter graphs ────────────────────────────────────────────


def test_g_stacks_the_cam_panel_over_the_gameplay_with_lanczos() -> None:
    chains = layout_filters(
        LayoutG(Box(1402, 472, 518, 608)),
        source_w=W,
        source_h=H,
        geometry=GEOMETRY,
        fps=30,
        scaler="lanczos",
        max_slide=0.25,
    )
    assert chains[0] == "[0:v]fps=fps=30,split=2[v_face_in][v_game_in]"
    assert chains[1].startswith("[v_face_in]crop=") and "scale=w=1080:h=720:flags=lanczos" in chains[1]
    assert (
        chains[2]
        == "[v_game_in]crop=w=972:h=1080:x=430:y=0,scale=w=1080:h=1200:flags=lanczos,setsar=1[v_game]"
    )
    assert chains[3] == "[v_face][v_game]vstack=inputs=2[v_layout]"


def test_g0_lays_4_by_5_gameplay_over_the_blurred_frame_at_285() -> None:
    chains = layout_filters(
        LayoutG0((Box(0, 0, 600, 300),)), source_w=W, source_h=H, geometry=GEOMETRY, fps=30, scaler="lanczos"
    )
    joined = ";".join(chains)
    assert "boxblur" in joined and "[v_bg][v_game]overlay=x=0:y=285[v_layout]" in joined
    assert "crop=w=864:h=1080:x=600:y=0,scale=w=1080:h=1350:flags=lanczos" in joined


def _spec(layout: LayoutG | LayoutG0, params: FactoryParams) -> RenderSpec:
    return RenderSpec(
        source=SourceInfo(PurePath("src.mp4"), W, H),
        window=ClipWindow(0.0, 10.0),
        layout=layout,
        ass_path=PurePath("clip.ass"),
        output_path=PurePath("out.mp4"),
        series_no=1,
        params=params,
        layout_v2=ON,
    )


@pytest.mark.parametrize("layout", [LayoutG(Box(0, 651, 248, 246)), LayoutG0()], ids=["G", "G0"])
def test_g_and_g0_have_no_seam_and_no_progress_bar(
    layout: LayoutG | LayoutG0, params_factory: Callable[..., FactoryParams]
) -> None:
    graph = ";".join(build_video_filters(_spec(layout, params_factory(end_card_s=0))))
    assert "drawbox=x=0:y=" not in graph, "seam line"
    assert "_bar]" not in graph and "x=-w+w*t" not in graph, "progress bar"
    assert re.search(r"\[v_layout\]ass=", graph), "text goes straight onto the frame"


# ── the master switch ────────────────────────────────────────


CAM = Box(0, 651, 248, 246)


@pytest.mark.parametrize("style_id", sorted(DEFAULT_STYLES))
def test_with_the_switch_on_every_style_draws_g_or_g0(style_id: str) -> None:
    entry = DEFAULT_STYLES[style_id]
    assert isinstance(entry, dict)
    style = StyleRules.from_setting(entry["rules"])
    for found in (
        LayoutA(CAM),
        LayoutCard(CAM),
        LayoutPov(CAM),
        LayoutMontage(CAM),
        LayoutA(CAM, cam_top=True),
    ):
        assert framed_layout(found, style, ON) == LayoutG(CAM), style_id
    for found in (LayoutB(), LayoutCard(), LayoutPov()):
        assert framed_layout(found, style, ON) == LayoutG0(), style_id


def test_with_the_switch_off_nothing_changes_and_g_falls_back() -> None:
    off = LayoutV2Rules()
    style = StyleRules(facecam_top=True)
    assert framed_layout(LayoutA(CAM), style, off) == LayoutA(CAM, cam_top=True)
    assert framed_layout(LayoutA(CAM), style, None) == LayoutA(CAM, cam_top=True)
    assert framed_layout(LayoutB(), style, off) == LayoutB()
    assert framed_layout(LayoutG(CAM), style, off) == LayoutA(CAM, cam_top=True)
    assert framed_layout(LayoutG0(), style, off) == LayoutB()
    detected = LayoutG(CAM, face_box=Box(50, 700, 100, 120), overlay_boxes=(Box(1500, 0, 400, 400),))
    assert framed_layout(detected, style, ON) is detected, "a detected G keeps its face and overlays"


# ── recipe and gate ──────────────────────────────────────────


def _recipe(layout: LayoutA | LayoutB | LayoutG | LayoutG0) -> dict[str, object]:
    raw = recipe_json(
        lines=(),
        layout=layout,
        duration_s=10.0,
        series_no=1,
        mode="normal",
        lane="hourly",
        creator="c",
        hook=None,
        keywords={},
        duck=(),
        payoff_s=None,
        payoff_emoji=None,
    )
    return json.loads(json.dumps(raw))  # type: ignore[no-any-return]


def test_the_recipe_keeps_kind_face_and_overlays() -> None:
    g = LayoutG(
        CAM, face_box=Box(50, 700, 100, 120), overlay_boxes=(Box(1500, 0, 400, 400), Box(0, 0, 10, 10))
    )
    assert layout_from_recipe(_recipe(g)) == g
    g0 = LayoutG0((Box(1500, 0, 400, 400),))
    assert layout_from_recipe(_recipe(g0)) == g0
    assert layout_from_recipe(_recipe(LayoutG0())) == LayoutB(), "no overlays: stored like layout B"
    assert layout_from_recipe(_recipe(LayoutA(CAM))) == LayoutA(CAM)
    assert layout_from_recipe(_recipe(LayoutB())) == LayoutB()


def test_an_old_recipe_without_a_kind_is_drawn_as_g_or_g0_when_the_switch_is_on() -> None:
    old = {"layout": {"facecam_box": {"x": 0, "y": 651, "w": 248, "h": 246}, "gameplay_box": None}}
    assert framed_layout(layout_from_recipe(old), StyleRules(), ON) == LayoutG(CAM)
    assert framed_layout(layout_from_recipe({"layout": None}), StyleRules(), ON) == LayoutG0()


def test_the_gate_reads_the_new_kinds() -> None:
    assert _layout_kind("G") == "G" and _layout_kind("G0") == "G0" and _layout_kind("X") is None

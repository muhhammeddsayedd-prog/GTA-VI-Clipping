"""Design styles (development-plan/M18 Phase A): the catalogue, the style director, layout C (facecam on
top), the title of a title-only style, the variant recipe and the per-render file checks. No network."""

from types import SimpleNamespace

from sixstars.factory.bleep import Span
from sixstars.factory.director import AccountSlot, DirectorRules, plan_styles, reference_account
from sixstars.factory.layout import (
    C_FACECAM_H,
    CAPTION_Y_C,
    Box,
    Geometry,
    LayoutA,
    LayoutB,
    gameplay_band,
    layout_filters,
)
from sixstars.factory.lines import Line, Word
from sixstars.factory.params import FactoryParams
from sixstars.factory.pipeline import framed_layout, has_emoji, title_text
from sixstars.factory.style import CAM_TOP_TITLE, CAM_TOP_VIRAL, VIRAL_STYLE, StyleRules
from sixstars.factory.styles import DEFAULT_STYLES, LEGACY_STYLE_ID, StyleCatalog
from sixstars.factory.subtitles import build_ass, caption_bottom
from sixstars.factory.variants import (
    copy_command,
    file_problems,
    layout_from_recipe,
    lines_from_recipe,
    recipe_json,
    variant_key,
)
from sixstars.factory.window import ClipWindow

ACCOUNTS = [
    AccountSlot("instagram_main", "instagram", ("hourly", "spike")),
    AccountSlot("tiktok_main", "tiktok", ("hourly", "spike")),
    AccountSlot("youtube_main", "youtube", ("hourly", "spike")),
]
# The Phase A tests use the two styles that need nothing of the clip; Phase B has its own tests.
# cam_top_viral is off since Render v2 R0 (D119); the planner tests switch it on as a second style.
CATALOG = StyleCatalog.from_setting(
    {"cam_top_viral": {**DEFAULT_STYLES["cam_top_viral"], "enabled": True}}  # type: ignore[dict-item]
    | {"cam_top_title": DEFAULT_STYLES["cam_top_title"]}
)
CAM = Box(0, 300, 260, 166)


# ── catalogue ────────────────────────────────────────────────


def test_the_default_catalogue_has_the_two_phase_a_styles_with_the_facecam_on_top() -> None:
    assert CATALOG.ids == ("cam_top_title", "cam_top_viral"), "sorted by id"
    viral, title = CATALOG.rules_for("cam_top_viral"), CATALOG.rules_for("cam_top_title")
    assert viral.facecam_top and title.facecam_top
    assert viral == StyleRules.from_setting(CAM_TOP_VIRAL) and viral.captions
    assert title == StyleRules.from_setting(CAM_TOP_TITLE)
    assert not title.captions and title.title_whole_clip


def test_without_the_setting_the_catalogue_is_the_single_legacy_style() -> None:
    for raw in (None, "nonsense", {}, {"x": {"enabled": False}}, {"x": "not an object"}):
        catalog = StyleCatalog.from_setting(raw, VIRAL_STYLE)
        assert catalog.ids == (LEGACY_STYLE_ID,)
        assert catalog.rules_for(LEGACY_STYLE_ID) == StyleRules.from_setting(VIRAL_STYLE)
        assert not catalog.rules_for(LEGACY_STYLE_ID).facecam_top


def test_malformed_entries_are_left_out_and_unknown_ids_fall_back_to_the_first_style() -> None:
    raw = {
        "good": {"rules": {"facecam_top": True}},
        "bad_lane": {"lanes": ["night"]},
        "bad_accounts": {"accounts": 3},
        "bad_weight": {"weight": True},
    }
    catalog = StyleCatalog.from_setting(raw)
    assert catalog.ids == ("good",)
    assert catalog.rules_for("gone") == catalog.rules_for("good")


def test_an_entry_limits_lanes_and_accounts() -> None:
    raw = {
        "a": {"lanes": ["hourly"], "accounts": ["tiktok_main"]},
        "b": {"weight": 0},
        "c": {"enabled": False},
        "d": {},
    }
    catalog = StyleCatalog.from_setting(raw)
    assert [e.id for e in catalog.usable("hourly", "tiktok_main")] == ["a", "d"]
    assert StyleCatalog.from_setting({"z": {}, "m": {}}).ids == ("m", "z")
    assert [e.id for e in catalog.usable("spike", "tiktok_main")] == ["d"]
    assert [e.id for e in catalog.usable("hourly", "youtube_main")] == ["d"]


# ── director ─────────────────────────────────────────────────


def test_the_plan_is_reproducible_and_gives_the_accounts_different_styles() -> None:
    plan = plan_styles("clip-1", "hourly", ACCOUNTS, CATALOG, DirectorRules())
    again = plan_styles("clip-1", "hourly", list(reversed(ACCOUNTS)), CATALOG, DirectorRules())
    assert plan == again
    assert set(plan.by_account) == {"instagram_main", "tiktok_main", "youtube_main"}
    # Two styles for three accounts: the first two accounts get both, the third repeats one.
    first_two = [plan.by_account["instagram_main"], plan.by_account["tiktok_main"]]
    assert sorted(first_two) == ["cam_top_title", "cam_top_viral"]
    assert plan.reference_account == "instagram_main"
    assert plan.reference_style == plan.by_account["instagram_main"]
    assert plan.styles[0] == plan.reference_style and set(plan.styles) == {"cam_top_title", "cam_top_viral"}


def test_an_account_does_not_get_the_style_of_its_last_clip_when_another_is_allowed() -> None:
    for clip in range(20):
        plan = plan_styles(
            f"clip-{clip}",
            "hourly",
            ACCOUNTS[:1],
            CATALOG,
            DirectorRules(no_repeat_n=1),
            recent={"instagram_main": ["cam_top_viral"]},
        )
        assert plan.by_account["instagram_main"] == "cam_top_title"
        assert plan.choices["instagram_main"].dropped_repeat == ("cam_top_viral",)


def test_rules_that_would_leave_nothing_keep_every_style() -> None:
    catalog = StyleCatalog.from_setting({"only": {}})
    plan = plan_styles("c", "spike", ACCOUNTS, catalog, DirectorRules(), recent={"tiktok_main": ["only"]})
    assert set(plan.by_account.values()) == {"only"} and plan.styles == ("only",)


def test_without_diversity_and_with_weights_the_pick_follows_the_weights() -> None:
    catalog = StyleCatalog.from_setting({"heavy": {"weight": 1000}, "light": {"weight": 0.001}})
    rules = DirectorRules(no_repeat_n=0, diverse_across_accounts=False)
    plans = [plan_styles(f"c{n}", "hourly", ACCOUNTS, catalog, rules) for n in range(30)]
    assert all(set(plan.by_account.values()) == {"heavy"} for plan in plans)


def test_the_reference_account_posts_the_clips_lane_and_no_accounts_still_gives_a_style() -> None:
    accounts = [AccountSlot("a_hourly", "tiktok", ("hourly",)), AccountSlot("b_spike", "tiktok", ("spike",))]
    assert reference_account(accounts, "spike") == "b_spike"
    assert reference_account(accounts, "hourly") == "a_hourly"
    plan = plan_styles("c", "hourly", [], CATALOG, DirectorRules())
    assert plan.reference_account is None and plan.reference_style == "cam_top_title"


def test_director_rules_read_from_the_setting() -> None:
    assert DirectorRules.from_setting(None) == DirectorRules()
    rules = DirectorRules.from_setting({"no_repeat_n": 3, "diverse_across_accounts": False})
    assert rules == DirectorRules(3, False)
    assert (
        DirectorRules.from_setting({"no_repeat_n": -1, "diverse_across_accounts": "yes"}) == DirectorRules()
    )


# ── layout C ─────────────────────────────────────────────────


def test_layout_c_puts_the_facecam_on_top() -> None:
    g = Geometry.for_canvas(1080, 1920)
    assert (g.c_facecam_h, g.c_gameplay_h, g.c_seam_y) == (C_FACECAM_H, 1920 - C_FACECAM_H, 669)
    assert g.seam_y("C") == 669 and g.seam_y("A") == g.a_seam_y
    layout = LayoutA(facecam_box=CAM, cam_top=True)
    assert layout.kind == "C" and LayoutA(facecam_box=CAM).kind == "A"
    chains = layout_filters(layout, source_w=1920, source_h=1080, geometry=g, fps=30)
    assert chains[-1] == "[v_face][v_game]vstack=inputs=2[v_layout]"
    assert any("h=672" in chain for chain in chains if chain.startswith("[v_face_in]"))
    assert any("h=1248" in chain for chain in chains if chain.startswith("[v_game_in]"))
    plain = layout_filters(LayoutA(facecam_box=CAM), source_w=1920, source_h=1080, geometry=g, fps=30)
    assert plain[-1] == "[v_game][v_face]vstack=inputs=2[v_layout]"


def test_the_style_decides_where_a_found_facecam_goes() -> None:
    found = LayoutA(facecam_box=CAM)
    assert framed_layout(found, StyleRules(facecam_top=True)).kind == "C"
    assert framed_layout(LayoutA(facecam_box=CAM, cam_top=True), StyleRules()).kind == "A"
    assert framed_layout(LayoutB(), StyleRules(facecam_top=True)).kind == "B"


def test_the_gate_band_of_layout_c_lies_between_the_title_and_the_captions() -> None:
    top, bottom = gameplay_band("C")
    assert top * 1920 >= C_FACECAM_H + 40  # below the title on the seam
    assert bottom * 1920 <= CAPTION_Y_C - 150  # above the caption and the credit line
    assert gameplay_band(None) == gameplay_band("A") == gameplay_band("B")


def test_layout_c_captions_sit_in_the_lower_gameplay(params: FactoryParams) -> None:
    bottom = caption_bottom("C", params)
    assert bottom - 78 > C_FACECAM_H and bottom <= 1920 - 420


# ── title-only style ─────────────────────────────────────────


def test_a_whole_clip_title_gets_the_payoff_emoji_when_the_hook_has_none() -> None:
    assert title_text("NPC has a pet alligator", "😭", whole_clip=True) == "NPC has a pet alligator 😭"
    assert title_text("NPC has a pet alligator 😭", "💀", whole_clip=True) == "NPC has a pet alligator 😭"
    assert title_text("Hook", "😭", whole_clip=False) == "Hook"
    assert title_text(None, "😭", whole_clip=True) is None
    assert has_emoji("wait 😭") and not has_emoji("GTA 6 can't, it's 100% real!")


def test_captions_off_draws_no_caption_events(params: FactoryParams, clip_lines_10s: list[Line]) -> None:
    with_captions = build_ass(clip_lines_10s, layout="C", params=params, duration_s=10.0)
    without = build_ass(
        clip_lines_10s,
        layout="C",
        params=params,
        duration_s=10.0,
        captions=False,
        hook="Hook line",
        hook_whole_clip=True,
        hook_top=600.0,
    )
    assert ",Caption," in with_captions and ",Caption," not in without
    hook_lines = [line for line in without.splitlines() if ",Hook," in line]
    assert hook_lines and all("0:00:10.00" in line for line in hook_lines)


# ── variants ─────────────────────────────────────────────────


def test_the_recipe_round_trips() -> None:
    lines = (Line(0, (Word("no", 0.1, 0.3), Word("way", 0.3, 0.6))), Line(1, (Word("bro", 1.0, 1.2),)))
    recipe = recipe_json(
        lines=lines,
        layout=LayoutA(facecam_box=CAM, gameplay_box=Box(300, 0, 1620, 1080), cam_top=True),
        duration_s=12.5,
        series_no=7,
        mode="normal",
        lane="hourly",
        creator="speed",
        hook="Hook",
        keywords={0: ["way"]},
        duck=(Span(1.0, 2.0),),
        payoff_s=10.0,
        payoff_emoji="😭",
    )
    assert lines_from_recipe(recipe) == lines
    # The layout comes back unframed: the variant's own style decides A or C.
    assert layout_from_recipe(recipe) == LayoutA(facecam_box=CAM, gameplay_box=Box(300, 0, 1620, 1080))
    no_cam = recipe_json(
        lines=lines,
        layout=LayoutB(),
        duration_s=1.0,
        series_no=1,
        mode="express",
        lane="spike",
        creator="",
        hook=None,
        keywords={},
        duck=(),
        payoff_s=None,
        payoff_emoji=None,
    )
    assert layout_from_recipe(no_cam) == LayoutB()


def test_the_source_copy_is_frame_exact_and_keeps_audio_when_there_is_some() -> None:
    from pathlib import Path

    command = copy_command(
        Path("in.mp4"), ClipWindow(12.5, 40.0), Path("out.mkv"), has_audio=True, ffmpeg="ff"
    )
    assert command[:6] == ["ff", "-hide_banner", "-nostdin", "-y", "-ss", "12.500"]
    assert "-t" in command and command[command.index("-t") + 1] == "27.500"
    assert "flac" in command and "libx264" in command
    silent = copy_command(Path("in.mp4"), ClipWindow(0, 5), Path("out.mkv"), has_audio=False, ffmpeg="ff")
    assert "-an" in silent and "flac" not in silent


def test_variant_keys_and_file_checks() -> None:
    assert variant_key("clips/staging/2026/11/19/abc.mp4", "cam_top_title") == (
        "clips/staging/2026/11/19/abc.cam_top_title.mp4"
    )
    good = SimpleNamespace(
        width=1080,
        height=1920,
        has_video=True,
        has_audio=True,
        duration_s=20.0,
        longest_black_s=0.0,
        longest_freeze_s=0.0,
    )
    limits = {
        "width": 1080,
        "height": 1920,
        "expected_s": 20.0,
        "tolerance_s": 1.0,
        "max_black_s": 4.0,
        "max_freeze_s": 4.0,
    }
    assert file_problems(good, **limits) == []
    assert file_problems(None, **limits) == ["file missing"]
    frozen = SimpleNamespace(**{**vars(good), "longest_freeze_s": 5.0, "width": 720})
    assert len(file_problems(frozen, **limits)) == 2

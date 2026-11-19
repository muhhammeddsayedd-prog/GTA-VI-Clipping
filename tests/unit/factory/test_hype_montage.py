"""The hype montage (development-plan/M18 Phase E): the B-roll timeline and picks, the motion reading,
layout M, the B-roll inputs, the two-tier captions and the style's place in the catalogue. No network."""

from pathlib import Path, PurePath

from sixstars.factory import montage
from sixstars.factory.director import clip_facts
from sixstars.factory.layout import (
    M_CREDIT_BOTTOM,
    M_FADE_H,
    M_SEAM_Y,
    M_TOP,
    Box,
    Geometry,
    LayoutA,
    LayoutB,
    LayoutMontage,
    gameplay_band,
    layout_filters,
)
from sixstars.factory.lines import Line, Word
from sixstars.factory.params import FactoryParams
from sixstars.factory.pipeline import framed_layout
from sixstars.factory.render import RenderSpec, SourceInfo, broll_cuts, build_render_command
from sixstars.factory.style import HYPE_MONTAGE, StyleRules
from sixstars.factory.styles import DEFAULT_STYLES, StyleCatalog
from sixstars.factory.subtitles import SubtitleStyle, build_ass, credit_bottom
from sixstars.factory.variants import recipe_json
from sixstars.factory.window import ClipWindow

CAM = Box(1440, 720, 480, 360)
RULES = StyleRules.from_setting(HYPE_MONTAGE)


# ── timeline and B-roll ──────────────────────────────────────


def test_the_timeline_cuts_away_every_other_slot_but_not_near_the_payoff() -> None:
    slots = montage.montage_timeline(20.0, 3.0, payoff_s=None)
    assert [(s.start_s, s.end_s) for s in slots] == [(3.0, 6.0), (9.0, 12.0), (15.0, 18.0)]
    guarded = montage.montage_timeline(20.0, 3.0, payoff_s=12.0)
    assert [(s.start_s, s.end_s) for s in guarded] == [(3.0, 6.0), (9.0, 10.5), (15.0, 18.0)], "cut short"
    close = montage.montage_timeline(20.0, 3.0, payoff_s=10.0)
    assert [(s.start_s, s.end_s) for s in close] == [(3.0, 6.0), (15.0, 18.0)], "9-8.5 is no slot at all"
    short_end = montage.montage_timeline(8.5, 3.0, payoff_s=None)
    assert [(s.start_s, s.end_s) for s in short_end] == [(3.0, 6.0)]
    assert len(montage.montage_timeline(300.0, 3.0, None)) == montage.MAX_CUTS
    assert montage.montage_timeline(20.0, 0.0, None) == []


def test_broll_is_the_busiest_footage_outside_the_clip_and_never_a_still() -> None:
    padded, clip = ClipWindow(0.0, 40.0), ClipWindow(15.0, 25.0)
    motion = [(t / 2, 1.0) for t in range(80)]  # everything a still...
    motion = [(t, 20.0 if 2 <= t < 5 or 30 <= t < 33 or 16 <= t < 19 else v) for t, v in motion]
    picks = montage.pick_broll(motion, clip, padded, length_s=3.0, count=3)
    assert len(picks) == 2, "the busy stretch inside the clip is not B-roll, the stills are not taken"
    assert all(p.end_s <= clip.start_s or p.start_s >= clip.end_s for p in picks)
    assert {(p.start_s, p.end_s) for p in picks} == {(2.0, 5.0), (30.0, 33.0)}
    assert montage.pick_broll(motion, clip, padded, length_s=3.0, count=0) == []


def test_the_motion_reading_is_parsed_on_the_file_clock() -> None:
    log = (
        "[Parsed_metadata_6 @ 0x1] frame:0    pts:0       pts_time:0\n"
        "[Parsed_metadata_6 @ 0x1] lavfi.signalstats.YAVG=0\n"
        "[Parsed_metadata_6 @ 0x1] frame:1    pts:1       pts_time:0.5\n"
        "[Parsed_metadata_6 @ 0x1] lavfi.signalstats.YAVG=12.75\n"
    )
    assert montage.parse_motion(log, offset_s=100.0) == [(100.0, 0.0), (100.5, 12.75)]
    command = montage.motion_command(Path("in.mp4"), ClipWindow(10.0, 70.0), ffmpeg="ff")
    assert command[:7] == ["ff", "-hide_banner", "-nostdin", "-nostats", "-ss", "10.000", "-t"]
    assert "tblend=all_mode=difference" in command[command.index("-vf") + 1]


def test_the_seam_fade_is_dark_in_the_middle_and_clear_at_the_edges(tmp_path: Path) -> None:
    from PIL import Image

    fade = montage.draw_seam_fade(tmp_path / "fade.png", width=1080, height=M_FADE_H)
    with Image.open(fade.path) as image:
        assert image.size == (1080, M_FADE_H)
        assert (
            image.getpixel((500, 0))[3] == 0
            and image.getpixel((500, M_FADE_H // 2))[3] == montage.FADE_MAX_ALPHA
        )


# ── layout M ─────────────────────────────────────────────────


def test_the_montage_needs_a_facecam() -> None:
    assert framed_layout(LayoutA(facecam_box=CAM), RULES) == LayoutMontage(facecam_box=CAM)
    assert framed_layout(LayoutB(), RULES) == LayoutB()
    assert framed_layout(LayoutMontage(facecam_box=CAM), StyleRules()) == LayoutA(facecam_box=CAM)


def test_layout_m_stacks_gameplay_over_face_and_lays_the_broll_over_the_gameplay() -> None:
    from sixstars.factory.layout import BrollCut

    g = Geometry.for_canvas(1080, 1920)
    cuts = (BrollCut(2, 100.0, 3.0, 3.0), BrollCut(3, 40.0, 9.0, 2.5))
    chains = layout_filters(
        LayoutMontage(facecam_box=CAM),
        source_w=1920,
        source_h=1080,
        geometry=g,
        fps=30,
        duration_s=20.0,
        broll=cuts,
    )
    joined = ";".join(chains)
    assert "[2:v]trim=duration=3,setpts=PTS-STARTPTS+3/TB" in joined
    assert "enable=between(t\\,9\\,11.5)" in joined or "enable=between(t,9,11.5)" in joined
    assert chains[-1].startswith("[m_game_2][m_face]vstack=inputs=2,pad=w=1080:h=1920:x=0:y=240")


def test_the_broll_inputs_come_after_the_sound_effects(params: FactoryParams) -> None:
    from sixstars.factory.audio import SfxTrack

    spec = RenderSpec(
        source=SourceInfo(PurePath("src.mp4"), 1920, 1080),
        window=ClipWindow(50.0, 70.0),
        layout=LayoutMontage(facecam_box=CAM),
        ass_path=PurePath("clip.ass"),
        output_path=PurePath("out.mp4"),
        series_no=1,
        params=params,
        sfx_path=PurePath("pop.wav"),
        sfx_times_s=(1.0,),
        sfx_tracks=(SfxTrack(PurePath("whoosh.wav"), (2.7,)),),
        broll=((100.0, 3.0, 3.0),),
    )
    assert [cut.input_index for cut in broll_cuts(spec)] == [3], "source 0, pop 1, whoosh 2"
    command = build_render_command(spec)
    inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
    assert inputs == ["src.mp4", "pop.wav", "whoosh.wav", "src.mp4"]
    assert command[command.index("-ss", command.index("whoosh.wav")) + 1] == "100"


def test_the_gate_band_of_layout_m_stays_on_the_gameplay(params: FactoryParams) -> None:
    top, bottom = (round(x * 1920) for x in gameplay_band("M"))
    assert M_TOP < M_CREDIT_BOTTOM < top and bottom < M_SEAM_Y - M_FADE_H // 2
    assert credit_bottom("M", params) == M_CREDIT_BOTTOM


# ── two-tier captions ────────────────────────────────────────


def test_a_keyword_chunk_shows_big_and_glowing_with_the_rest_small_above(params: FactoryParams) -> None:
    style = SubtitleStyle.from_rules(RULES)
    lines = [
        Line(0, (Word("you", 1.0, 1.3), Word("can", 1.3, 1.6))),
        Line(1, (Word("scuba", 2.0, 2.4), Word("dive", 2.4, 2.9))),
    ]
    ass = build_ass(lines, layout="M", params=params, duration_s=5.0, style=style, keywords={1: ["scuba"]})
    captions = [line for line in ass.splitlines() if ",Caption," in line]
    plain = [c for c in captions if "YOU" in c]
    tiered = [c for c in captions if "0:00:02.00" in c]
    assert len(plain) == 1 and "\\i1" not in plain[0]
    assert len(tiered) == 3, "halo, crisp keyword, small lead"
    assert sum("\\blur" in c and c.endswith("SCUBA") for c in tiered) == 1
    assert sum("\\blur" not in c and c.endswith("SCUBA") for c in tiered) == 1
    assert any(c.endswith("DIVE") and "\\fs50" in c for c in tiered)
    every = build_ass(
        lines[1:], layout="M", params=params, duration_s=5.0, style=style, keywords={1: ["scuba", "dive"]}
    )
    assert len([c for c in every.splitlines() if ",Caption," in c]) == 2, (
        "no lead when every word is a keyword"
    )


# ── catalogue, facts, recipe ─────────────────────────────────


def test_the_montage_is_an_hourly_style_that_needs_a_facecam_and_footage_around_the_clip() -> None:
    entry = StyleCatalog.from_setting(DEFAULT_STYLES).get("hype_montage")
    assert entry is not None and entry.lanes == ("hourly",) and entry.requires == ("facecam", "broll")
    assert (
        entry.rules == RULES and RULES.caption_tiers and not RULES.show_title and RULES.montage_every_s == 3.0
    )
    assert entry.fits(clip_facts({}, has_facecam=True, broll=True))
    assert not entry.fits(clip_facts({}, has_facecam=True))


def test_the_recipe_says_where_the_clip_starts_in_a_wider_copy() -> None:
    recipe = recipe_json(
        lines=(),
        layout=LayoutA(facecam_box=CAM),
        duration_s=20.0,
        series_no=1,
        mode="normal",
        lane="hourly",
        creator="c",
        hook=None,
        keywords={},
        duck=(),
        payoff_s=None,
        payoff_emoji=None,
        window_start_s=45.0,
        broll=True,
    )
    assert recipe["window_start_s"] == 45.0 and recipe["broll"] is True

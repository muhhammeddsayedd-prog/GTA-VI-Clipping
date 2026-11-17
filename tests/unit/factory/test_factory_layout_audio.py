"""Layout geometry, crop maths, layout filter chains and the audio chain (bleep plan, loudnorm)."""

import pytest

from sixstars.factory.audio import (
    LoudnormMeasurement,
    SfxPlan,
    bleep_filters,
    build_audio_filters,
    loudnorm_filter,
    parse_loudnorm_report,
    spans_expression,
)
from sixstars.factory.bleep import Span
from sixstars.factory.layout import (
    Box,
    Geometry,
    LayoutA,
    LayoutB,
    fit_crop,
    gameplay_crop,
    layout_filters,
    scale_filters,
)

GEOMETRY = Geometry.for_canvas(1080, 1920)


def test_geometry_matches_docs_at_the_reference_size() -> None:
    g = GEOMETRY
    assert (g.a_gameplay_h, g.a_facecam_h, g.a_seam_y, g.a_seam_h) == (1150, 770, 1147, 6)
    assert (g.b_gameplay_h, g.b_gameplay_y, g.b_blur_radius) == (1350, 285, 30)
    assert (g.progress_y, g.progress_h, g.watermark_right, g.watermark_y) == (1500, 8, 1040, 240)


def test_geometry_scales_to_a_smaller_canvas() -> None:
    g = Geometry.for_canvas(720, 1280)
    assert g.a_gameplay_h + g.a_facecam_h == 1280 and g.a_gameplay_h % 2 == 0
    assert g.a_gameplay_h == 766 and g.b_gameplay_h == 900 and g.b_gameplay_y == 190
    assert g.progress_y == 1000 and g.watermark_right == 693


@pytest.mark.parametrize(
    ("box", "aspect", "source", "expected"),
    [
        (Box(0, 0, 1920, 1080), (1080, 1150), (1920, 1080), Box(452, 0, 1014, 1080)),
        (Box(0, 0, 1920, 1080), (1080, 1350), (1920, 1080), Box(528, 0, 864, 1080)),
        (Box(0, 0, 1920, 1080), (1080, 770), (1920, 1080), Box(202, 0, 1514, 1080)),
        (Box(1440, 720, 480, 360), (1080, 770), (1920, 1080), Box(1440, 728, 480, 342)),
        (Box(100, 100, 300, 900), (1080, 770), (1920, 1080), Box(100, 444, 300, 212)),
        (Box(1800, 1000, 400, 400), (1, 1), (1920, 1080), Box(1820, 1000, 80, 80)),
        (Box(0, 0, 1280, 720), (1080, 1150), (1280, 720), Box(302, 0, 676, 720)),
        (Box(0, 0, 608, 1080), (1080, 1350), (608, 1080), Box(0, 160, 608, 760)),
    ],
)
def test_fit_crop(box: Box, aspect: tuple[int, int], source: tuple[int, int], expected: Box) -> None:
    crop = fit_crop(box, *aspect, *source)
    assert crop == expected
    assert crop.w % 2 == 0 and crop.h % 2 == 0 and crop.x % 2 == 0 and crop.y % 2 == 0
    assert crop.x + crop.w <= source[0] and crop.y + crop.h <= source[1]
    assert abs(crop.w / crop.h - aspect[0] / aspect[1]) < 0.02


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        (LayoutA(Box(1440, 720, 480, 360)), Box(426, 0, 1014, 1080)),  # slides left, off the facecam
        (LayoutA(Box(0, 0, 480, 360)), Box(480, 0, 1014, 1080)),  # slides right
        (LayoutA(Box(1500, 0, 400, 300)), Box(452, 0, 1014, 1080)),  # no overlap: stays centred
        (LayoutA(Box(700, 400, 500, 500)), Box(452, 0, 1014, 1080)),  # centre cam: cannot avoid, stays
        (LayoutA(Box(1440, 720, 480, 360), gameplay_box=Box(0, 0, 1440, 1080)), Box(212, 0, 1014, 1080)),
    ],
)
def test_gameplay_crop(layout: LayoutA, expected: Box) -> None:
    assert gameplay_crop(layout, 1080, 1150, 1920, 1080) == expected


def test_invalid_boxes_are_rejected() -> None:
    for args in [(-1, 0, 10, 10), (0, 0, 0, 10), (0, 0, 10, -5)]:
        with pytest.raises(ValueError):
            Box(*args)


def test_scale_filters_plain_and_zoomed() -> None:
    assert scale_filters(1080, 1150, 10.0, zoom=False) == ["scale=w=1080:h=1150", "setsar=1"]
    zoomed = scale_filters(1080, 1150, 10.0, zoom=True)
    factor = "(1+0.08*clip((t-6)/4\\,0\\,1))"
    assert zoomed == [
        f"scale=w=2*trunc(1080*{factor}/2):h=2*trunc(1150*{factor}/2):eval=frame",
        f"crop=w=1080:h=1150:x=max(0\\,(2*trunc(1080*{factor}/2)-1080)/2):y=max(0\\,(2*trunc(1150*{factor}/2)-1150)/2)",
        "setsar=1",
    ]


def test_layout_a_chains() -> None:
    chains = layout_filters(
        LayoutA(Box(1440, 720, 480, 360)),
        source_w=1920,
        source_h=1080,
        geometry=GEOMETRY,
        fps=30,
        duration_s=10.0,
        zoom=False,
    )
    assert chains == [
        "[0:v]fps=fps=30,split=2[v_game_in][v_face_in]",
        "[v_game_in]crop=w=1014:h=1080:x=426:y=0,scale=w=1080:h=1150,setsar=1[v_game]",
        "[v_face_in]crop=w=480:h=342:x=1440:y=728,scale=w=1080:h=770,setsar=1[v_face]",
        "[v_game][v_face]vstack=inputs=2[v_layout]",
    ]


def test_layout_b_chains() -> None:
    chains = layout_filters(
        LayoutB(), source_w=1920, source_h=1080, geometry=GEOMETRY, fps=30, duration_s=10.0, zoom=False
    )
    assert chains == [
        "[0:v]fps=fps=30,split=2[v_bg_in][v_game_in]",
        "[v_bg_in]scale=w=270:h=480:force_original_aspect_ratio=increase,crop=w=270:h=480,"
        "boxblur=luma_radius=8:luma_power=3,scale=w=1080:h=1920:flags=bilinear,"
        "drawbox=color=black@0.3:thickness=fill,setsar=1[v_bg]",
        "[v_game_in]crop=w=864:h=1080:x=528:y=0,scale=w=1080:h=1350,setsar=1[v_game]",
        "[v_bg][v_game]overlay=x=0:y=285[v_layout]",
    ]


def test_layout_zoom_applies_to_gameplay_only() -> None:
    chains = layout_filters(
        LayoutA(Box(1440, 720, 480, 360)),
        source_w=1920,
        source_h=1080,
        geometry=GEOMETRY,
        fps=30,
        duration_s=10.0,
    )
    assert "eval=frame" in chains[1] and "eval=frame" not in chains[2]


# ---------------------------------------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spans", "expected"),
    [
        ([Span(4.85, 5.45)], "between(t,4.85,5.45)"),
        ([Span(0.0, 0.3), Span(2.0, 2.5)], "between(t,0,0.3)+between(t,2,2.5)"),
        ([Span(1.23456, 2.0004)], "between(t,1.235,2)"),
    ],
)
def test_spans_expression(spans: list[Span], expected: str) -> None:
    assert spans_expression(spans) == expected


def test_spans_expression_needs_spans() -> None:
    with pytest.raises(ValueError):
        spans_expression([])


def test_bleep_filters_mute_the_spans_and_gate_a_tone_into_them() -> None:
    chains = bleep_filters([Span(4.85, 5.45), Span(7.0, 7.4)], 10.0, "a_src", "a_bleep")
    inside = "between(t\\,4.85\\,5.45)+between(t\\,7\\,7.4)"
    assert chains == [
        f"[a_src]volume=volume=0:enable={inside}[a_bleep_muted]",
        "sine=frequency=1000:sample_rate=48000:duration=10,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume=volume=-20dB,volume=volume=0:enable=not({inside})[a_bleep_tone]",
        "[a_bleep_muted][a_bleep_tone]amix=inputs=2:duration=first:normalize=0[a_bleep]",
    ]


def test_bleep_filters_without_spans_pass_audio_through() -> None:
    assert bleep_filters([], 10.0, "a_src", "a_bleep") == ["[a_src]anull[a_bleep]"]


def test_loudnorm_filter_variants() -> None:
    assert loudnorm_filter(-14) == "loudnorm=I=-14:TP=-1.5:LRA=11"
    assert loudnorm_filter(-16.5, report=True) == "loudnorm=I=-16.5:TP=-1.5:LRA=11:print_format=json"
    measured = LoudnormMeasurement(-23.1, -4.2, 5.5, -33.4, 0.3)
    assert loudnorm_filter(-14, measured) == (
        "loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-23.1:measured_TP=-4.2:measured_LRA=5.5:"
        "measured_thresh=-33.4:offset=0.3:linear=true"
    )


LOUDNORM_STDERR = """
size=N/A time=00:00:10.00 bitrate=N/A speed= 300x
[Parsed_loudnorm_7 @ 000001]
{
\t"input_i" : "-20.76",
\t"input_tp" : "-17.75",
\t"input_lra" : "0.00",
\t"input_thresh" : "-30.76",
\t"output_i" : "-14.02",
\t"output_tp" : "-11.01",
\t"output_lra" : "0.00",
\t"output_thresh" : "-24.02",
\t"normalization_type" : "dynamic",
\t"target_offset" : "0.02"
}
"""


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (LOUDNORM_STDERR, LoudnormMeasurement(-20.76, -17.75, 0.0, -30.76, 0.02)),
        (LOUDNORM_STDERR.replace("-20.76", "-inf"), None),
        (LOUDNORM_STDERR.replace('"target_offset" : "0.02"', '"other" : "1"'), None),
        ("no report here", None),
        ("{ not json }", None),
        ("", None),
    ],
)
def test_parse_loudnorm_report(stderr: str, expected: LoudnormMeasurement | None) -> None:
    assert parse_loudnorm_report(stderr) == expected


PREPARE = (
    "aresample=out_sample_rate=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad=whole_dur=10,"
    "atrim=duration=10,asetpts=PTS-STARTPTS"
)


def test_audio_chain_plain() -> None:
    chains = build_audio_filters(source="0:a", duration_s=10.0, end_card_s=1.5, target_lufs=-14)
    assert chains == [
        f"[0:a]{PREPARE}[a_src]",
        "[a_src]anull[a_bleep]",
        f"[a_bleep]loudnorm=I=-14:TP=-1.5:LRA=11,{PREPARE}[a_main]",
        "anullsrc=sample_rate=48000:channel_layout=stereo:duration=1.5,aformat=sample_fmts=fltp:channel_layouts=stereo[a_end]",
        "[a_main][a_end]concat=n=2:v=0:a=1[aout]",
    ]


def test_audio_chain_with_everything() -> None:
    chains = build_audio_filters(
        source="0:a",
        duration_s=10.0,
        end_card_s=0.0,
        target_lufs=-14,
        bleeps=[Span(4.85, 5.45)],
        duck_spans=[Span(0.0, 3.0)],
        duck_db=-18,
        sfx=SfxPlan(input_index=1, times_s=(0.6, 2.4, 12.0)),
    )
    assert chains[0] == f"[0:a]{PREPARE},volume=volume=-18dB:enable=between(t\\,0\\,3)[a_src]"
    assert chains[1].startswith("[a_src]volume=volume=0:enable=between(t\\,4.85\\,5.45)")
    assert chains[4:8] == [
        "[1:a]aresample=out_sample_rate=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=volume=-12dB,"
        "asplit=outputs=2[a_sfx_p0_in][a_sfx_p1_in]",
        "[a_sfx_p0_in]adelay=delays=600:all=1[a_sfx_p0]",
        "[a_sfx_p1_in]adelay=delays=2400:all=1[a_sfx_p1]",
        "[a_bleep][a_sfx_p0][a_sfx_p1]amix=inputs=3:duration=first:normalize=0[a_sfx]",
    ]
    assert chains[-1] == f"[a_sfx]loudnorm=I=-14:TP=-1.5:LRA=11,{PREPARE}[aout]"


def test_audio_chain_for_a_source_without_audio() -> None:
    chains = build_audio_filters(source=None, duration_s=10.0, end_card_s=0.0, target_lufs=-14)
    assert chains[0] == (
        "anullsrc=sample_rate=48000:channel_layout=stereo:duration=10,aformat=sample_fmts=fltp:channel_layouts=stereo[a_src]"
    )

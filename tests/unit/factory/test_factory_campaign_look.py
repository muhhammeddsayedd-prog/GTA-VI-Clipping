"""Campaign looks (D118, PR 3): brief_match, render profiles applied to style rules, the disclosure line and
the campaign logo in the filter graph, and a real ffmpeg frame check of both."""

import shutil
import struct
import subprocess
import zlib
from pathlib import Path, PurePosixPath

import pytest

from sixstars.campaigns import brief, profiles
from sixstars.factory.layout import Box, Geometry, LayoutA
from sixstars.factory.overlays import disclosure_filter
from sixstars.factory.params import FactoryParams, FontFiles
from sixstars.factory.render import (
    RenderSpec,
    SourceInfo,
    build_video_filters,
    campaign_logo_filters,
    logo_position,
)
from sixstars.factory.style import StyleRules
from sixstars.factory.window import ClipWindow

ASSETS = Path(__file__).resolve().parents[3] / "assets" / "fonts"
FONTS = FontFiles(fonts_dir=PurePosixPath("/app/assets/fonts"), font_file=PurePosixPath("/app/f.ttf"))


# ── brief_match ──────────────────────────────────────────────


def test_a_clip_that_fits_the_brief_passes() -> None:
    result = brief.check(
        {"min_len": 10, "max_len": 60}, has_facecam=True, duration_s=20, creator_in_campaign=True
    )
    assert result.passed and result.failures == ()


def test_each_brief_rule_fails_on_its_own() -> None:
    assert not brief.check({}, has_facecam=False, duration_s=20, creator_in_campaign=True).passed
    assert brief.check(
        {"streamer_on_screen": False}, has_facecam=False, duration_s=20, creator_in_campaign=True
    ).passed
    short = brief.check({"min_len": 15}, has_facecam=True, duration_s=9.5, creator_in_campaign=True)
    assert short.failures == ("clip is 9.5 s, the brief wants at least 15 s",)
    long = brief.check({"max_len": 30}, has_facecam=True, duration_s=31, creator_in_campaign=True)
    assert "at most 30 s" in long.failures[0]
    assert not brief.check({}, has_facecam=True, duration_s=20, creator_in_campaign=False).passed
    assert not brief.check(
        {"overlay_required": True}, has_facecam=True, duration_s=20, creator_in_campaign=True
    ).passed


def test_brief_result_json() -> None:
    result = brief.check({}, has_facecam=False, duration_s=12.0, creator_in_campaign=True)
    data = result.as_json("dylan")
    assert data["campaign"] == "dylan" and data["passed"] is False
    assert data["facts"] == {"has_facecam": False, "duration_s": 12.0, "creator_in_campaign": True}


# ── profiles applied to style rules ──────────────────────────


def test_brand_changes_nothing() -> None:
    rules = StyleRules(follow_overlay_s=1.5, follow_overlay_text="Follow", frame="pov")
    assert profiles.apply(rules, profiles.BRAND_PROFILE) is rules


def test_a_campaign_profile_sets_its_look() -> None:
    rules = StyleRules(follow_overlay_s=1.5, follow_overlay_text="Follow", frame="pov")
    clean = profiles.RenderProfile(
        "clean-disclose", watermark=False, disclosure_text="Paid partnership", disclosure_s=1.5
    )
    out = profiles.apply(rules, clean)
    assert (out.watermark, out.disclosure_text, out.disclosure_s) == (False, "Paid partnership", 1.5)
    assert (
        out.follow_overlay_s == 0 and out.follow_overlay_text == ""
    )  # the follow line is a Normal-lane test
    assert out.frame == "auto"  # the POV crop would cut the streamer's face cam away
    assert out.campaign_logo == ""


def test_a_logo_profile_needs_its_file_and_position() -> None:
    logo = profiles.RenderProfile("partner-logo-x", watermark=False, logo_pos="bottom_right", logo_sha="abc")
    with_file = profiles.apply(StyleRules(), logo, Path("/data/logos/abc.png"))
    assert (with_file.campaign_logo, with_file.campaign_logo_pos) == (
        str(Path("/data/logos/abc.png")),
        "bottom_right",
    )
    assert profiles.apply(StyleRules(), logo, None).campaign_logo == ""


def test_brand_disclose_keeps_our_watermark() -> None:
    profile = profiles.RenderProfile(
        "brand-disclose", watermark=True, disclosure_text="Paid partnership", disclosure_s=1.5
    )
    assert profiles.apply(StyleRules(), profile).watermark
    assert not profiles.apply(StyleRules(watermark=False), profile).watermark  # the news card stays without


# ── filters ──────────────────────────────────────────────────


def test_the_disclosure_line_shows_only_at_the_start() -> None:
    geometry = Geometry.for_canvas(1080, 1920)
    line = disclosure_filter(
        "Paid partnership",
        geometry=geometry,
        font_name="X",
        fonts=FONTS,
        clip_s=20,
        show_s=1.5,
        safe_top_px=210,
    )
    assert line is not None
    assert "drawtext=text=Paid partnership" in line and "enable=lt(t\\,1.5)" in line
    assert (
        disclosure_filter(
            "", geometry=geometry, font_name="X", fonts=FONTS, clip_s=20, show_s=1.5, safe_top_px=210
        )
        is None
    )
    assert (
        disclosure_filter(
            "Paid", geometry=geometry, font_name="X", fonts=FONTS, clip_s=20, show_s=0, safe_top_px=210
        )
        is None
    )


@pytest.mark.parametrize(
    ("pos", "x", "y"),
    [("bottom_right", "W-w-48", "H-h-48"), ("top_left", "48", "48"), ("top_right", "W-w-48", "48")],
)
def test_logo_corners_stay_inside_the_safe_zone(pos: str, x: str, y: str) -> None:
    assert logo_position(pos, Geometry.for_canvas(1080, 1920), 0, 0) == (x, y)
    assert logo_position("bottom_left", Geometry.for_canvas(1080, 1920), 200, 300) == ("48", "H-h-348")


def spec(params: FactoryParams, **overrides: object) -> RenderSpec:
    values: dict[str, object] = {
        "source": SourceInfo(PurePosixPath("/data/vod/v.mp4"), 1920, 1080),
        "window": ClipWindow(0.0, 20.0),
        "layout": LayoutA(facecam_box=Box(1440, 720, 480, 360)),
        "ass_path": PurePosixPath("/data/render/c1/clip.ass"),
        "output_path": PurePosixPath("/data/ready/c1.mp4"),
        "series_no": 1,
        "params": params,
        "fonts": FONTS,
    }
    values.update(overrides)
    return RenderSpec(**values)  # type: ignore[arg-type]


def test_the_graph_has_the_campaign_look_only_when_asked(params: FactoryParams) -> None:
    plain = ";".join(build_video_filters(spec(params)))
    assert "Paid partnership" not in plain and "v_logo" not in plain
    styled = ";".join(
        build_video_filters(
            spec(
                params,
                watermark=False,
                disclosure_text="Paid partnership",
                disclosure_s=1.5,
                campaign_logo=PurePosixPath("/data/logos/abc.png"),
                campaign_logo_pos="bottom_right",
            )
        )
    )
    assert "Paid partnership" in styled
    assert "movie=filename=/data/logos/abc.png" in styled and "[v_logo]" in styled
    assert params.watermark not in styled


# ── real ffmpeg frame check ──────────────────────────────────


def _png(path: Path, width: int, height: int) -> None:
    """A solid red RGBA PNG (no image library needed)."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _frame(video: Path, at_s: float, width: int, height: int) -> bytes:
    out = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{at_s}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert len(out) == width * height * 3
    return out


def _pixel(frame: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    i = (y * width + x) * 3
    return frame[i], frame[i + 1], frame[i + 2]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_ffmpeg_draws_the_disclosure_then_removes_it_and_keeps_the_logo(
    params: FactoryParams, tmp_path: Path
) -> None:
    width, height = 540, 960
    geometry = Geometry.for_canvas(width, height)
    logo = tmp_path / "logo.png"
    _png(logo, 400, 200)
    font = ASSETS / "Montserrat-Black.ttf"
    fonts = FontFiles(fonts_dir=PurePosixPath(str(ASSETS)), font_file=PurePosixPath(str(font)))
    test_spec = spec(
        params, fonts=fonts, campaign_logo=PurePosixPath(str(logo)), campaign_logo_pos="bottom_right"
    )
    logo_chain = campaign_logo_filters(test_spec, geometry, source="0:v", out="v_logo")
    line = disclosure_filter(
        "Paid partnership",
        geometry=geometry,
        font_name="Montserrat",
        fonts=fonts,
        clip_s=3.0,
        show_s=1.5,
        safe_top_px=0,
    )
    assert line is not None
    graph = ";".join([*logo_chain, f"[v_logo]{line}[out]"])
    video = tmp_path / "out.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=gray:s={width}x{height}:d=3:r=10",
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    early, late = _frame(video, 0.5, width, height), _frame(video, 2.2, width, height)
    # The disclosure box sits at the top left in the first 1.5 s only.
    box_x, box_y = round(48 * height / 1920) + 4, round(24 * height / 1920) + 4
    assert _pixel(early, width, box_x, box_y) != _pixel(late, width, box_x, box_y)
    assert max(_pixel(late, width, box_x, box_y)) - min(_pixel(late, width, box_x, box_y)) < 12  # gray again
    # The red logo (100 px wide on this canvas) sits in the bottom right corner, above the bottom safe area,
    # for the whole clip.
    margin = round(48 * height / 1920)
    bottom = round(params.safe_bottom_px * height / 1920) + margin
    corner_x, corner_y = width - margin - 10, height - bottom - 10
    for frame in (early, late):
        red, green, blue = _pixel(frame, width, corner_x, corner_y)
        assert red > 150 and green < 90 and blue < 90

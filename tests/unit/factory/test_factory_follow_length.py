"""YouTube lane tuning C: the writer's hourly limits and the follow line (a style rule, no extra time)."""

import pytest

from sixstars.factory.config import FactoryConfig
from sixstars.factory.layout import REF_HEIGHT, Geometry
from sixstars.factory.overlays import follow_filter, follow_layout
from sixstars.factory.params import FontFiles
from sixstars.factory.style import StyleRules

OVERRIDE = {"youtube": {"min_len": 8, "max_len": 30}}


def test_the_writer_may_cut_short_hourly_clips_but_not_spike_clips() -> None:
    hourly = FactoryConfig.offline("vod", {"gate.hourly_len_by_platform": OVERRIDE})
    spike = FactoryConfig.offline("spike", {"gate.hourly_len_by_platform": OVERRIDE})
    plain = FactoryConfig.offline("vod")

    assert (hourly.min_len_s, hourly.max_len_s) == (8, plain.max_len_s)
    assert (spike.min_len_s, spike.max_len_s) == (
        FactoryConfig.offline("spike").min_len_s,
        FactoryConfig.offline("spike").max_len_s,
    )


# ── the follow line ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "Follow @6starsClips-m3x for Part 2",
        "Follow",
        "Follow @a_really_long_handle_name_for_testing for Part 2 👆",
    ],
)
@pytest.mark.parametrize(("top", "bottom"), [(220, 420), (0, 0), (400, 700)])
def test_the_follow_line_stays_inside_the_safe_zone(text: str, top: int, bottom: int) -> None:
    layout = follow_layout(text, top, bottom)
    assert layout.inside(top, bottom)
    assert layout.top + layout.size < REF_HEIGHT


def test_the_follow_filter_shows_the_last_seconds_only() -> None:
    geometry = Geometry.for_canvas(1080, 1920)
    drawn = follow_filter(
        "Follow @6starsClips-m3x for Part 2",
        geometry=geometry,
        font_name="Anton",
        fonts=FontFiles(),
        accent_color="#FF2BD6",
        clip_s=18.0,
        show_s=1.5,
        safe_top_px=220,
        safe_bottom_px=420,
    )
    assert drawn is not None
    assert drawn.startswith("drawtext=text=Follow @6starsClips-m3x for Part 2")
    assert drawn.endswith("enable=gte(t\\,16.5)")  # the last 1.5 s of an 18 s clip
    assert "box=1" in drawn


@pytest.mark.parametrize(("text", "show_s"), [("", 1.5), ("Follow", 0.0)])
def test_no_follow_line_when_off(text: str, show_s: float) -> None:
    drawn = follow_filter(
        text,
        geometry=Geometry.for_canvas(1080, 1920),
        font_name="Anton",
        fonts=FontFiles(),
        accent_color="#FF2BD6",
        clip_s=18.0,
        show_s=show_s,
        safe_top_px=220,
        safe_bottom_px=420,
    )
    assert drawn is None


def test_style_rules_read_the_follow_line() -> None:
    rules = StyleRules.from_setting(
        {"follow_overlay_s": 1.5, "follow_overlay_text": "Follow  @x  for Part 2"}
    )
    assert (rules.follow_overlay_s, rules.follow_overlay_text) == (1.5, "Follow @x for Part 2")
    assert StyleRules.from_setting({"follow_overlay_s": 9}).follow_overlay_s == 0.0  # out of range: classic
    assert StyleRules.classic().follow_overlay_s == 0.0

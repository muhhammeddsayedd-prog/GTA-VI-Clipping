"""Render v2 guard (D119, development-plan/M19 R0): no code path changes the scale or the crop of the frame
over time. Every style of the catalogue is drawn on every layout kind and the whole filter graph is checked:
no per-frame scale (`eval=frame`), no `zoompan`, no time-dependent `scale` or `crop` argument, and no
close-up laid over the frame for a span (`overlay` with `enable=between(...)`). The montage's B-roll inserts
are the only timed overlay left (they are gameplay, not a close-up; the montage is retired in R1). Pure."""

import re
from collections.abc import Callable
from pathlib import PurePath

import pytest

from sixstars.factory.layout import (
    Box,
    Layout,
    LayoutA,
    LayoutB,
    LayoutCard,
    LayoutG,
    LayoutG0,
    LayoutMontage,
    LayoutPov,
    LayoutV2Rules,
)
from sixstars.factory.params import FactoryParams
from sixstars.factory.pipeline import framed_layout
from sixstars.factory.render import RenderSpec, SourceInfo, build_filter_complex
from sixstars.factory.style import StyleRules
from sixstars.factory.styles import DEFAULT_STYLES
from sixstars.factory.window import ClipWindow

CAM = Box(1440, 720, 480, 360)
BASE_LAYOUTS: dict[str, Layout] = {
    "A": LayoutA(CAM),
    "C": LayoutA(CAM, cam_top=True),
    "B": LayoutB(),
    "H": LayoutCard(CAM),
    "P": LayoutPov(CAM),
    "M": LayoutMontage(CAM),
    # Render v2 (R1): drawn with factory.layout_v2 on, with a chat box the gameplay slides away from.
    "G": LayoutG(CAM, overlay_boxes=(Box(1500, 100, 400, 500),)),
    "G0": LayoutG0(overlay_boxes=(Box(0, 0, 300, 200),)),
}
V2_ON = LayoutV2Rules(enabled=True, scaler="lanczos")
# A filter is the text between unescaped commas or semicolons; its input labels come first.
_SPLIT = re.compile(r"(?<!\\)[,;]")
_LABELS = re.compile(r"^(?:\[[^\]]*\])+")
# A time or frame variable of an ffmpeg expression.
_TIME_VAR = re.compile(r"(?<![A-Za-z_])(t|n|pos|time)(?![A-Za-z_(])")


def _style_rules() -> dict[str, StyleRules]:
    rules: dict[str, StyleRules] = {}
    for style_id, entry in DEFAULT_STYLES.items():
        assert isinstance(entry, dict)
        rules[style_id] = StyleRules.from_setting(entry["rules"])
    return rules


def _spec(layout: Layout, style: StyleRules, params: FactoryParams) -> RenderSpec:
    montage = isinstance(layout, LayoutMontage)
    return RenderSpec(
        source=SourceInfo(PurePath("src.mp4"), 1920, 1080),
        window=ClipWindow(0.0, 12.0),
        layout=layout,
        ass_path=PurePath("clip.ass"),
        output_path=PurePath("out.mp4"),
        series_no=1,
        params=params,
        compress=style.compress,
        watermark=style.watermark,
        title_png=PurePath("title.png") if style.show_title else None,
        title_whole_clip=style.title_whole_clip,
        follow_text="Follow @6starsclips" if style.follow_overlay_s else "",
        follow_s=style.follow_overlay_s,
        broll=((40.0, 3.0, 3.0), (52.0, 9.0, 2.5)) if montage else (),
    )


def _filters(graph: str) -> list[tuple[str, str, str]]:
    """(input labels, filter name, arguments) of every filter in the graph."""
    found = []
    for part in _SPLIT.split(graph):
        part = part.strip()
        labels = _LABELS.match(part)
        head = labels.group(0) if labels else ""
        body = part[len(head) :]
        name, _, args = body.partition("=")
        found.append((head, name, args))
    return found


CASES = [
    pytest.param(style_id, kind, id=f"{style_id}-{kind}")
    for style_id in DEFAULT_STYLES
    for kind in BASE_LAYOUTS
]


@pytest.mark.parametrize(("style_id", "kind"), CASES)
def test_no_filter_changes_the_framing_over_time(
    style_id: str, kind: str, params_factory: Callable[..., FactoryParams]
) -> None:
    style = _style_rules()[style_id]
    layout = framed_layout(BASE_LAYOUTS[kind], style, V2_ON if kind in ("G", "G0") else None)
    assert kind not in ("G", "G0") or layout.kind == kind
    graph = build_filter_complex(_spec(layout, style, params_factory(end_card_s=0)))
    assert "eval=frame" not in graph
    assert "zoompan" not in graph
    for labels, name, args in _filters(graph):
        if name in ("scale", "crop", "zoompan"):
            # Drop the escaped separators, then look for a time variable in any value.
            values = [value.partition("=")[2] for value in args.replace("\\,", " ").split(":")]
            assert not any(_TIME_VAR.search(value) for value in values), f"{name}={args}"
        if name == "overlay" and "enable=between(" in args:
            assert layout.kind == "M" and "[m_broll_" in labels, f"timed overlay {labels}{name}={args}"


def test_every_layout_kind_is_covered() -> None:
    """A new layout kind must be added to this guard."""
    from typing import get_args

    from sixstars.factory.layout import LayoutKind

    assert set(BASE_LAYOUTS) == set(get_args(LayoutKind))


def test_the_guard_catches_a_zoom() -> None:
    """The checks above do find the zoom that R0 removed."""
    old_zoom = (
        "[v_game_in]crop=w=1014:h=1080:x=426:y=0,scale=w=2*trunc(1080*(1+0.08*clip((t-6)/4\\,0\\,1))/2)"
    )
    old_punch = "[v_layout_base][v_punch]overlay=x=0:y=0:enable=between(t\\,2\\,3.2)[v_layout]"
    scale = [f for f in _filters(old_zoom) if f[1] == "scale"][0]
    assert _TIME_VAR.search(scale[2].replace("\\,", " "))
    overlay = [f for f in _filters(old_punch) if f[1] == "overlay"][0]
    assert "enable=between(" in overlay[2] and "[m_broll_" not in overlay[0]

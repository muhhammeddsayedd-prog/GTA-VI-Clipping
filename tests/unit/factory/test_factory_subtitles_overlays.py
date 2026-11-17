"""ASS builder (captions, highlight, keyword pop, masking, safe zone) and the overlay builders."""

import os
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from sixstars.factory.bleep import ProfanityMatcher
from sixstars.factory.escape import WORD_JOINER
from sixstars.factory.layout import Geometry
from sixstars.factory.lines import Line, Word
from sixstars.factory.overlays import (
    HOOK_MAX_WIDTH,
    HOOK_SIZES,
    ass_filter,
    badge_events,
    credit_events,
    credit_text,
    end_card_filters,
    fit_hook,
    hook_events,
    progress_filters,
    rounded_rect_path,
    seam_filter,
    series_line,
    watermark_filter,
)
from sixstars.factory.params import FactoryParams, FontFiles
from sixstars.factory.subtitles import (
    KEYWORD_POP,
    build_ass,
    caption_bottom,
    caption_events,
    credit_bottom,
    keyword_flags,
    safe_zone,
)
from sixstars.factory.textfit import estimate_em_text_width, estimate_text_width

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "factory"
GEOMETRY = Geometry.for_canvas(1080, 1920)
ACCENT = "&H00D4FF&"
WHITE = "&HFFFFFF&"
POS = re.compile(r"\\pos\((\d+),(\d+)\)")
EVENT = re.compile(
    r"^Dialogue: (\d+),(\d):(\d\d):(\d\d)\.(\d\d),(\d):(\d\d):(\d\d)\.(\d\d),(\w+),,0,0,0,,(.*)$"
)


def assert_snapshot(name: str, actual: str) -> None:
    """Compares with a stored file; run with UPDATE_SNAPSHOTS=1 to (re)write it."""
    path = FIXTURES / name
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert path.is_file(), f"missing snapshot {path}; run with UPDATE_SNAPSHOTS=1"
    assert actual == path.read_text(encoding="utf-8").replace("\r\n", "\n")


def parse_events(ass: str) -> list[tuple[int, float, float, str, str]]:
    """(layer, start, end, style, text) for every Dialogue line."""
    events = []
    for line in ass.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        match = EVENT.match(line)
        assert match is not None, f"malformed event: {line!r}"
        g = match.groups()
        start = int(g[1]) * 3600 + int(g[2]) * 60 + int(g[3]) + int(g[4]) / 100
        end = int(g[5]) * 3600 + int(g[6]) * 60 + int(g[7]) + int(g[8]) / 100
        events.append((int(g[0]), start, end, g[9], g[10]))
    return events


def captions(ass: str) -> list[tuple[int, float, float, str, str]]:
    return [event for event in parse_events(ass) if event[3] == "Caption"]


def test_ass_header_and_styles(clip_lines_10s: list[Line], params: FactoryParams) -> None:
    ass = build_ass(clip_lines_10s, layout="A", params=params, duration_s=10.0)
    assert "PlayResX: 1080\nPlayResY: 1920\n" in ass
    assert (
        "Style: Caption,Montserrat Black,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,6,3,2,40,40,0,1" in ass
    )
    assert ass.endswith("\n") and "\r" not in ass
    known = (
        "[",
        "ScriptType",
        "PlayRes",
        "WrapStyle",
        "ScaledBorder",
        "YCbCr",
        "Format:",
        "Style:",
        "Dialogue:",
    )
    assert all(line.startswith(known) for line in ass.splitlines() if line)


def test_one_caption_event_per_word_state(clip_lines_10s: list[Line], params: FactoryParams) -> None:
    events = captions(build_ass(clip_lines_10s, layout="A", params=params, duration_s=10.0))
    assert len(events) == sum(len(line.words) for line in clip_lines_10s) == 21
    assert [(e[1], e[2]) for e in events[:5]] == [(0.2, 0.4), (0.4, 0.6), (0.6, 1.0), (1.0, 1.4), (1.4, 2.1)]
    for earlier, later in zip(events, events[1:], strict=False):
        assert earlier[2] <= later[1] + 1e-9, "caption events overlap"
        assert earlier[1] < earlier[2]
    assert events[-1][2] == pytest.approx(9.7)  # last word end + hold


def test_current_word_is_highlighted_in_the_accent_colour(
    clip_lines_10s: list[Line], params: FactoryParams
) -> None:
    events = captions(build_ass(clip_lines_10s, layout="A", params=params, duration_s=10.0))
    prefix = "{\\an2\\pos(540,1189)}"
    assert events[3][4] == f"{prefix}{{\\1c{ACCENT}}}expect{{\\1c{WHITE}}} that!"
    assert events[4][4] == f"{prefix}expect {{\\1c{ACCENT}}}that!{{\\1c{WHITE}}}"


def test_keyword_pops_when_spoken_then_stays_accent(
    clip_lines_10s: list[Line], params: FactoryParams, keywords: dict[int, list[str]]
) -> None:
    events = captions(
        build_ass(clip_lines_10s, layout="A", params=params, duration_s=10.0, keywords=keywords)
    )
    assert KEYWORD_POP == "\\t(0,80,\\fscx118\\fscy118)\\t(80,160,\\fscx100\\fscy100)"
    pop = f"{{\\1c{ACCENT}{KEYWORD_POP}}}"
    reset = f"{{\\1c{WHITE}\\fscx100\\fscy100}}"
    # line 0 "He did NOT": the keyword is white until spoken
    assert events[0][4].endswith(f"{{\\1c{ACCENT}}}He{{\\1c{WHITE}}} did NOT")
    assert events[2][4].endswith(f"He did {pop}NOT{reset}")
    # line 2 "No way," is a two-word keyword: "No" pops, then stays accent while "way," pops
    assert events[5][4].endswith(f"{pop}No{reset} way,")
    assert events[6][4].endswith(f"{{\\1c{ACCENT}}}No{{\\1c{WHITE}}} {pop}way,{reset}")
    # lines without keywords never pop
    assert KEYWORD_POP not in events[3][4]


@pytest.mark.parametrize(
    ("words", "keywords", "expected"),
    [
        (["No", "way,", "dude"], ["no way"], [True, True, False]),
        (["fifty", "thousand", "dollars"], ["THOUSAND", "dollars!"], [False, True, True]),
        (["no", "no", "way"], ["no way"], [False, True, True]),
        (["a", "b"], ["", "  ", "c"], [False, False]),
        (["way"], ["no way"], [False]),
    ],
)
def test_keyword_flags(words: list[str], keywords: list[str], expected: list[bool]) -> None:
    assert keyword_flags(words, keywords) == expected


def test_profanity_is_masked_only_when_bleep_is_on(
    clip_lines_10s: list[Line], params_factory: Callable[..., FactoryParams], profanity_terms: frozenset[str]
) -> None:
    matcher = ProfanityMatcher(profanity_terms)
    masked = build_ass(
        clip_lines_10s, layout="A", params=params_factory(), duration_s=10.0, profanity=matcher
    )
    assert "f******" in masked and "fucking" not in masked
    plain = build_ass(
        clip_lines_10s, layout="A", params=params_factory(bleep=False), duration_s=10.0, profanity=matcher
    )
    assert "fucking" in plain and "f******" not in plain


@pytest.mark.parametrize(
    ("layout", "overrides", "bottom"),
    [
        ("A", {}, 1189),
        ("B", {}, 1489),
        ("B", {"safe_bottom_px": 600}, 1311),
        ("A", {"safe_bottom_px": 800}, 1111),
        ("B", {"safe_bottom_px": 440}, 1471),
    ],
)
def test_caption_position_per_layout_and_safe_zone(
    params_factory: Callable[..., FactoryParams], layout: str, overrides: dict[str, object], bottom: int
) -> None:
    params = params_factory(**overrides)
    assert caption_bottom(layout, params) == bottom  # type: ignore[arg-type]
    events = caption_events([Line(0, (Word("hi", 0.0, 0.5),))], layout=layout, params=params, duration_s=5.0)  # type: ignore[arg-type]
    assert f"\\an2\\pos(540,{bottom})" in events[0]


def test_safe_zone_scales_with_the_output_height(params_factory: Callable[..., FactoryParams]) -> None:
    zone = safe_zone(params_factory(width=720, height=1280, safe_top_px=146, safe_bottom_px=280))
    assert zone.top == pytest.approx(219) and zone.bottom == pytest.approx(1500)


@pytest.mark.parametrize("layout", ["A", "B"])
@pytest.mark.parametrize("live_badge", [False, True])
def test_every_positioned_event_is_inside_the_safe_zone(
    clip_lines_10s: list[Line],
    params: FactoryParams,
    keywords: dict[int, list[str]],
    layout: str,
    live_badge: bool,
) -> None:
    ass = build_ass(
        clip_lines_10s,
        layout=layout,  # type: ignore[arg-type]
        params=params,
        duration_s=10.0,
        keywords=keywords,
        hook="He did NOT expect that 😭",
        creator="@some_streamer",
        live_badge=live_badge,
    )
    events = parse_events(ass)
    assert {e[3] for e in events} == {"Caption", "Hook", "Credit"} | ({"Badge"} if live_badge else set())
    for _layer, _start, end, style, text in events:
        positions = POS.findall(text)
        assert len(positions) == 1, text
        y = int(positions[0][1])
        assert 220 <= y <= 1500, f"{style} at y={y}"
        assert end <= 10.0
    hook_y = {int(POS.search(e[4]).group(2)) for e in events if e[3] == "Hook"}  # type: ignore[union-attr]
    assert hook_y == ({392} if live_badge else {340})  # box 312..472 under the badge, else 260..420
    assert caption_bottom(layout, params) + 6 + 3 <= 1500  # type: ignore[arg-type]
    assert credit_bottom(layout, params) == (1091 if layout == "A" else 1391)  # type: ignore[arg-type]


def test_long_lines_shrink_instead_of_wrapping(params: FactoryParams) -> None:
    wide = Line(0, (Word("MMMMMMMMMM", 0.0, 0.5), Word("WWWWWWWWWW", 0.5, 1.0)))
    event = caption_events([wide], layout="B", params=params, duration_s=5.0)[0]
    size = int(re.search(r"\\fs(\d+)", event).group(1))  # type: ignore[union-attr]
    assert 48 <= size < 78
    assert estimate_text_width("MMMMMMMMMM WWWWWWWWWW", size) <= 1000 or size == 48
    short = caption_events([Line(0, (Word("ok", 0.0, 0.5),))], layout="B", params=params, duration_s=5.0)[0]
    assert "\\fs" not in short


def test_events_are_clamped_to_the_clip(params: FactoryParams) -> None:
    line = Line(0, (Word("late", 9.9, 10.5), Word("gone", 10.0, 10.0)))
    events = caption_events([line], layout="A", params=params, duration_s=10.0)
    assert len(events) == 1 and ",0:00:09.90,0:00:10.00," in events[0]


HOSTILE_WORDS = ["{\\an8}top", "50%,", "a\\Nb", "it's:", 'say "hi";', "[x]", "new\nline", "😭"]


def test_hostile_text_cannot_inject_tags_or_lines(params: FactoryParams) -> None:
    words = tuple(Word(text, i * 0.5, i * 0.5 + 0.4) for i, text in enumerate(HOSTILE_WORDS))
    lines = [Line(0, words[:4]), Line(1, words[4:])]
    ass = build_ass(
        lines,
        layout="A",
        params=params,
        duration_s=10.0,
        hook="{\\pos(0,0)}Hook, with: 100% \\N {junk}\n"
        "Dialogue: 0,0:00:00.00,0:00:09.00,Caption,,0,0,0,,INJECTED",
        creator="@bad{\\fs200}guy\nDialogue: x",
        live_badge=True,
    )
    assert len([line for line in ass.splitlines() if line.startswith("Dialogue:")]) == len(parse_events(ass))
    assert not any(line.startswith("Dialogue: 0,0:00:00.00,0:00:09.00") for line in ass.splitlines())
    for _layer, _start, _end, _style, text in parse_events(ass):
        blocks = re.findall(r"(?<!\\)\{([^}]*)\}", text)
        for block in blocks:  # only tags written by the builder
            assert re.fullmatch(
                r"(\\(an\d|pos\(\d+,\d+\)|1c&H[0-9A-F]{6}&|4c&H[0-9A-F]{6}&|4a&H[0-9A-F]{2}&|fs\d+|fscx\d+|fscy\d+|t\(\d+,\d+,\\fscx\d+\\fscy\d+\)|fad\(\d+,\d+\)|p[01]|bord\d|shad\d|q2))+",
                block,
            ), block
    assert f"\\{{\\{WORD_JOINER}an8\\}}top" in ass
    assert f"a\\{WORD_JOINER}Nb" in ass
    assert "new line" in ass and "50%," in ass


def test_font_name_cannot_break_the_style_row(
    clip_lines_10s: list[Line], params_factory: Callable[..., FactoryParams]
) -> None:
    ass = build_ass(
        clip_lines_10s, layout="A", params=params_factory(font="Evil, Font\nStyle: x"), duration_s=10.0
    )
    styles = [line for line in ass.splitlines() if line.startswith("Style:")]
    assert len(styles) == 4 and all(len(style.split(",")) == 23 for style in styles)


def test_ass_snapshot(
    clip_lines_10s: list[Line],
    params: FactoryParams,
    keywords: dict[int, list[str]],
    profanity_terms: frozenset[str],
) -> None:
    ass = build_ass(
        clip_lines_10s,
        layout="A",
        params=params,
        duration_s=10.0,
        keywords=keywords,
        profanity=ProfanityMatcher(profanity_terms),
        hook="He did NOT expect that 😭",
        creator="@some_streamer",
        live_badge=True,
    )
    assert_snapshot("clip_layout_a.ass", ass)


# ---------------------------------------------------------------------------------------------------------
# overlays
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "lines", "max_size"),
    [
        ("NO WAY", 1, 76),
        ("He did NOT expect that 😭", 1, 76),
        ("Worst getaway driver in Leonida? Absolutely yes", 2, 64),
        ("  spaced \n out\thook  ", 1, 76),
    ],
)
def test_fit_hook(text: str, lines: int, max_size: int) -> None:
    fit = fit_hook(text)
    assert fit is not None
    assert len(fit.lines) == lines and fit.font_size <= max_size and fit.font_size in HOOK_SIZES
    assert " ".join(fit.lines) == " ".join(text.split())
    assert all(estimate_text_width(line, fit.font_size) <= fit.box_width - 2 * 40 for line in fit.lines)
    assert fit.box_width <= HOOK_MAX_WIDTH


def test_fit_hook_truncates_what_cannot_fit_in_two_lines() -> None:
    fit = fit_hook(" ".join(["unbelievable"] * 30))
    assert fit is not None and len(fit.lines) == 2 and fit.lines[-1].endswith("…")
    assert fit.font_size == HOOK_SIZES[-1]
    giant = fit_hook("W" * 400)
    assert giant is not None and len(giant.lines) == 1 and giant.lines[0].endswith("…")
    assert fit_hook("   \n ") is None


def test_hook_events_box_and_text() -> None:
    events = hook_events("NO WAY", end_s=3.0)
    assert len(events) == 2
    shape, label = events
    assert shape.startswith("Dialogue: 5,0:00:00.00,0:00:03.00,Hook,,0,0,0,,{\\an5\\pos(540,340)")
    assert "\\t(0,150,\\fscx100\\fscy100)" in shape and "\\p1" in shape and "\\1c&HFFFFFF&" in shape
    assert shape.endswith("{\\p0}")
    assert label.startswith("Dialogue: 6,0:00:00.00,0:00:03.00,Hook,,0,0,0,,{\\an5\\pos(540,340)")
    assert label.endswith("\\fs76}NO WAY")
    two = hook_events("Worst getaway driver in Leonida? Absolutely yes", end_s=3.0, top=312)
    assert "\\pos(540,392)" in two[1] and two[1].count("\\N") == 1
    assert hook_events("", end_s=3.0) == [] and hook_events("x", end_s=0.0) == []


def test_rounded_rect_path() -> None:
    assert rounded_rect_path(100, 40, 0) == "m 0 0 l 100 0 100 40 0 40"
    path = rounded_rect_path(200, 56, 28)
    assert path.startswith("m 28 0 l 172 0 b ") and path.count(" b ") == 4
    numbers = [int(n) for n in re.findall(r"-?\d+", path)]
    assert min(numbers) == 0 and max(numbers) == 200
    assert rounded_rect_path(40, 20, 500) == rounded_rect_path(40, 20, 10)  # radius is capped


def test_badge_and_credit_events() -> None:
    shape, label = badge_events(end_s=10.0, top=240)
    assert shape.startswith("Dialogue: 3,0:00:00.00,0:00:10.00,Badge,,0,0,0,,{\\an7\\pos(40,240)\\p1")
    assert "\\1c&H2B21E8&" in shape
    assert label.endswith("{\\an4\\pos(66,268)\\q2}● LIVE · JUST HAPPENED")
    assert credit_events("@some_streamer", end_s=10.0, bottom=1091) == [
        "Dialogue: 1,0:00:00.00,0:00:10.00,Credit,,0,0,0,,{\\an1\\pos(40,1091)\\q2}🎥 @some_streamer"
    ]
    assert credit_events("  ", end_s=10.0, bottom=1091) == []


@pytest.mark.parametrize(
    ("creator", "expected"),
    [
        ("some_streamer", "🎥 @some_streamer"),
        ("@some_streamer", "🎥 @some_streamer"),
        ("@@x\ny", "🎥 @x y"),
        ("", ""),
        ("@", ""),
    ],
)
def test_credit_text(creator: str, expected: str) -> None:
    assert credit_text(creator) == expected


def test_seam_and_progress_filters() -> None:
    assert seam_filter(GEOMETRY, "#FFD400") == "drawbox=x=0:y=1147:w=1080:h=6:color=0xFFD400:thickness=fill"
    assert progress_filters(
        geometry=GEOMETRY, accent_color="#FFD400", fps=30, duration_s=10.0, source="v_text", out="v_progress"
    ) == [
        "color=color=0xFFD400:size=1080x8:rate=30:duration=10[v_progress_bar]",
        "[v_text][v_progress_bar]overlay=x=-w+w*t/10:y=1500:eof_action=pass[v_progress]",
    ]


@pytest.mark.parametrize(
    ("ass_path", "fonts_dir", "expected"),
    [
        (PurePosixPath("/data/render/abc/clip.ass"), None, "ass=filename=/data/render/abc/clip.ass"),
        (
            PurePosixPath("/data/render/abc/clip.ass"),
            PurePosixPath("/app/assets/fonts"),
            "ass=filename=/data/render/abc/clip.ass:fontsdir=/app/assets/fonts",
        ),
        (
            PureWindowsPath("d:\\Personal Projects\\6Starts - GTA VI\\render\\clip a.ass"),
            PureWindowsPath("C:\\Windows\\Fonts"),
            "ass=filename=d\\\\:/Personal Projects/6Starts - GTA VI/render/clip a.ass"
            ":fontsdir=C\\\\:/Windows/Fonts",
        ),
        (
            PurePosixPath("/data/it's, a [dir]; 100%/clip.ass"),
            None,
            "ass=filename=/data/it\\\\\\'s\\, a \\[dir\\]\\; 100%/clip.ass",
        ),
    ],
)
def test_ass_filter_paths(
    ass_path: PurePosixPath | PureWindowsPath, fonts_dir: PurePosixPath | None, expected: str
) -> None:
    assert ass_filter(ass_path, fonts_dir) == expected


def test_watermark_filter() -> None:
    fonts = FontFiles(font_file=PureWindowsPath("C:\\Windows\\Fonts\\ariblk.ttf"))
    assert watermark_filter(
        "@6starsclips", geometry=GEOMETRY, font_name="Montserrat Black", fonts=fonts, start_s=3.0
    ) == (
        "drawtext=text=@6starsclips:expansion=none:fontfile=C\\\\:/Windows/Fonts/ariblk.ttf:fontsize=30:"
        "fontcolor=white@0.6:x=1040-text_w:y=240:shadowcolor=black@0.35:shadowx=2:shadowy=2:enable=gte(t\\,3)"
    )
    by_name = watermark_filter(
        "@x", geometry=GEOMETRY, font_name="Montserrat Black", fonts=FontFiles(), start_s=0.0
    )
    assert by_name is not None and ":font=Montserrat Black:" in by_name and "enable" not in by_name
    assert watermark_filter(" \n", geometry=GEOMETRY, font_name="x", fonts=FontFiles(), start_s=0.0) is None


def test_watermark_text_is_escaped() -> None:
    hostile = watermark_filter(
        "@me: it's 100%, [ok]; \\o/\n%{pts}", geometry=GEOMETRY, font_name="F", fonts=FontFiles(), start_s=0.0
    )
    assert hostile is not None
    assert hostile.startswith(
        "drawtext=text=@me\\\\: it\\\\\\'s 100%\\, \\[ok\\]\\; \\\\\\\\o/ %{pts}:expansion=none:"
    )


def test_series_line() -> None:
    assert series_line("6 Stars Moments", 128) == "6 Stars Moments #128"
    assert series_line("  ", 7) == "#7"
    assert series_line("Line\nbreak", 1) == "Line break #1"


def test_end_card_filters() -> None:
    chains = end_card_filters(
        geometry=GEOMETRY,
        fps=30,
        end_card_s=1.5,
        series_label="6 Stars Moments",
        series_no=128,
        accent_color="#FFD400",
        font_name="Montserrat Black",
        fonts=FontFiles(font_file=PurePosixPath("/app/assets/fonts/Montserrat-Black.ttf")),
        out="v_end",
    )
    font = "expansion=none:fontfile=/app/assets/fonts/Montserrat-Black.ttf"
    assert chains == [
        "color=color=0x0D0D12:size=1080x1920:rate=30:duration=1.5,"
        f"drawtext=text=6 STARS CLIPS:{font}:fontsize=92:fontcolor=white:x=(w-text_w)/2:y=800,"
        f"drawtext=text=6 Stars Moments #128:{font}:fontsize=58:fontcolor=0xFFD400:x=(w-text_w)/2:y=932,"
        f"drawtext=text=Follow for more:{font}:fontsize=42:fontcolor=white@0.85:x=(w-text_w)/2:y=1034,"
        "setsar=1,format=yuv420p[v_end]"
    ]


def test_end_card_shrinks_a_long_series_label() -> None:
    label = "An Extremely Long Series Label That Cannot Fit"
    chain = end_card_filters(
        geometry=GEOMETRY,
        fps=30,
        end_card_s=1.5,
        series_label=label,
        series_no=1,
        accent_color="#FFD400",
        font_name="F",
        fonts=FontFiles(),
        out="v_end",
    )[0]
    size = int(re.search(r"#1:expansion=none:font=F:fontsize=(\d+)", chain).group(1))  # type: ignore[union-attr]
    assert size < 58 and estimate_em_text_width(f"{label} #1", size) <= 980

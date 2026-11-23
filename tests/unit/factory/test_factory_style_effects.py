"""The docs/27 editing style: the `factory.style` setting, caption chunks, sound effects and the
filter-graph pieces they add. Pure: no ffmpeg, no network."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePath

import pytest

from sixstars.ai.schemas import ClipWriting
from sixstars.factory.assets import load_assets
from sixstars.factory.audio import SfxPlan, build_audio_filters
from sixstars.factory.bleep import ProfanityMatcher, Span
from sixstars.factory.effects import SfxCue, parse_momentary, plan_sfx
from sixstars.factory.layout import Geometry, LayoutB
from sixstars.factory.lines import Line, Word, chunk_line, collapse_repeats
from sixstars.factory.params import FactoryParams
from sixstars.factory.render import RenderSpec, SourceInfo, build_render_command
from sixstars.factory.style import RETIRED_KEYS, VIRAL_STYLE, StyleRules
from sixstars.factory.subtitles import SubtitleStyle, build_ass
from sixstars.factory.window import ClipWindow

REPO = Path(__file__).resolve().parents[3]


def _line(index: int, *words: tuple[str, float, float]) -> Line:
    return Line(index, tuple(Word(text, start, end) for text, start, end in words))


def _viral() -> StyleRules:
    return StyleRules.from_setting(VIRAL_STYLE)


# ── the setting ──────────────────────────────────────────────


def test_missing_or_bad_setting_gives_the_classic_look() -> None:
    classic = StyleRules.classic()
    assert StyleRules.from_setting(None) == classic
    assert StyleRules.from_setting("nope") == classic
    assert StyleRules.from_setting({"caption_size": 5000}) == classic  # fails validation as a whole
    assert not classic.chunked and not classic.payoff_fx and not classic.compress


def test_viral_setting_is_read_and_bad_fields_fall_back() -> None:
    rules = _viral()
    assert rules.chunked and rules.caption_words == 2 and rules.caption_caps
    assert rules.caption_font == "Anton" and rules.hook_mode == "title_bar"
    assert rules.payoff_fx and rules.compress
    odd = StyleRules.from_setting({**VIRAL_STYLE, "hook_mode": "banner", "caption_words": 1.5, "compress": 1})
    assert odd.hook_mode == "box" and odd.caption_words == 0 and odd.compress is False


# ── caption chunks ───────────────────────────────────────────


def test_chunk_line_splits_at_words_chars_pauses_and_punctuation() -> None:
    line = _line(
        3,
        ("He", 0.0, 0.2),
        ("did", 0.2, 0.4),
        ("NOT", 0.4, 0.7),
        ("expect", 0.7, 1.0),
        ("that!", 1.0, 1.3),
        ("Helicopter", 1.35, 2.0),
        ("now", 2.6, 2.8),
    )
    chunks = chunk_line(line, max_words=2, max_chars=14)
    assert [chunk.words for chunk in chunks] == [(0, 1), (2, 3), (4,), (5,), (6,)]
    assert all(chunk.line_index == 3 for chunk in chunks)
    assert chunks[0].start_s == 0.0 and chunks[0].end_s == 0.4
    with pytest.raises(ValueError):
        chunk_line(line, max_words=0, max_chars=14)


def test_collapse_repeats_merges_a_chunk_said_again() -> None:
    line = _line(0, ("come", 0, 0.2), ("here", 0.2, 0.4), ("Come", 0.4, 0.6), ("here!", 0.6, 0.8))
    other = _line(1, ("come", 1.0, 1.2), ("here", 1.2, 1.4), ("bro", 1.5, 1.7))
    chunks = chunk_line(line, 2, 14) + chunk_line(other, 2, 14)
    merged = collapse_repeats([line, other], chunks)
    assert [chunk.repeat for chunk in merged] == [3, 1]
    assert merged[0].start_s == 0 and merged[0].end_s == 1.4


def test_chunked_captions_are_caps_with_accent_repeat_and_mask(
    params_factory: Callable[..., FactoryParams],
) -> None:
    params = params_factory(accent_color="#FF2BD6")
    lines = [
        _line(0, ("wait", 0.0, 0.3), ("wait", 0.35, 0.6), ("no", 0.7, 0.9), ("way", 0.9, 1.2)),
        _line(1, ("this", 1.5, 1.7), ("fucking", 1.7, 2.1), ("cop", 2.1, 2.4)),
    ]
    style = SubtitleStyle.from_rules(_viral())
    ass = build_ass(
        lines,
        layout="A",
        params=params,
        duration_s=5.0,
        keywords={0: ["no way"]},
        profanity=ProfanityMatcher(frozenset({"fucking"})),
        style=style,
        hook="He did NOT expect that 😭",
        hook_mode="title_bar",
        payoff_s=2.2,
        payoff_emoji="💀",
    )
    captions = [row for row in ass.splitlines() if row.startswith("Dialogue: 2,")]
    assert len(captions) == 4  # WAIT X2 | NO WAY | THIS F****** | COP
    assert "WAIT X2" in captions[0] and "\\fscx80" in captions[0]
    assert "&HD62BFF&" in captions[1] and "NO" in captions[1] and "\\fscx125" in captions[1]
    assert "F******" in captions[2] and "fucking" not in ass.lower()
    assert "Style: Caption,Anton,150," in ass
    hook_rows = [row for row in ass.splitlines() if row.startswith("Dialogue: 5,")]
    assert hook_rows and all(",0:00:03.00,Hook," in row for row in hook_rows)  # first hook_seconds only
    payoff = [row for row in ass.splitlines() if row.startswith("Dialogue: 8,")]
    assert len(payoff) == 1 and "0:00:02.20,0:00:03.70" in payoff[0] and "Noto Emoji" in payoff[0]


def test_classic_style_is_unchanged_by_default(params: FactoryParams, clip_lines_10s: list[Line]) -> None:
    assert build_ass(clip_lines_10s, layout="A", params=params, duration_s=10.0) == build_ass(
        clip_lines_10s,
        layout="A",
        params=params,
        duration_s=10.0,
        style=SubtitleStyle.from_rules(StyleRules.classic()),
    )


def test_retired_zoom_keys_are_ignored_not_fatal() -> None:
    """Render v2 (D119): a setting that still carries the zoom and punch-in rules reads exactly like one
    without them, and is not rejected (which would fall back to the classic look)."""
    old = {
        **VIRAL_STYLE,
        "punchin_max": 4,
        "punchin_zoom": 1.25,
        "punchin_len_s": 1.2,
        "punchin_min_gap_s": 2.5,
        "punchin_db": 5,
        "zoomcut_every_s": 2.5,
        "zoomcut_scale": 1.08,
        "cut_sfx": True,
    }
    assert set(RETIRED_KEYS) <= set(old)
    assert StyleRules.from_setting(old) == _viral() != StyleRules.classic()
    assert not set(RETIRED_KEYS) & set(VIRAL_STYLE)
    # Out-of-range values of the old rules no longer matter.
    assert StyleRules.from_setting({**VIRAL_STYLE, "punchin_zoom": 99, "zoomcut_scale": -1}) == _viral()


# ── sound effects ────────────────────────────────────────────


def test_parse_momentary_reads_ebur128_lines() -> None:
    stderr = (
        "[Parsed_ebur128_0 @ 0x1] t: 0.5      TARGET:-23 LUFS    M: -20.1 S:-120.7     I: -20.1 LUFS\n"
        "[Parsed_ebur128_0 @ 0x1] t: 0.6      TARGET:-23 LUFS    M:-120.7 S:-120.7     I: -70.0 LUFS\n"
        "[Parsed_ebur128_0 @ 0x1] t: 0.7      TARGET:-23 LUFS    M:  -inf S:-120.7\n"
    )
    assert parse_momentary(stderr) == [(pytest.approx(0.3), -20.1), (pytest.approx(0.4), -120.7)]


def test_plan_sfx_orders_cues_and_clears_pops() -> None:
    cues = plan_sfx(
        duration_s=10.0,
        keyword_times=(1.0, 3.05, 5.0, 9.9, 12.0),
        cuts=(Span(3.0, 4.2),),
        payoff_s=7.0,
        rules=_viral(),
    )
    assert cues == (
        SfxCue("pop", 1.0),
        SfxCue("whoosh", 2.7),
        SfxCue("impact", 3.0),
        SfxCue("pop", 5.0),
        SfxCue("boom", 7.0),
        SfxCue("pop", 9.9),
    )
    classic = plan_sfx(
        duration_s=10.0,
        keyword_times=(1.0, 3.05),
        payoff_s=7.0,
        rules=StyleRules.classic(),
    )
    assert classic == (SfxCue("pop", 1.0), SfxCue("pop", 3.05))


# ── audio graph ──────────────────────────────────────────────


def test_audio_graph_mixes_extra_effects_and_compresses() -> None:
    chains = build_audio_filters(
        source="0:a",
        duration_s=10.0,
        end_card_s=0.0,
        target_lufs=-12,
        sfx=SfxPlan(1, (1.0,), gain_db=-10),
        extra_sfx=[SfxPlan(2, (2.7,), gain_db=-10), SfxPlan(3, (), gain_db=-10)],
        compress=True,
    )
    graph = ";".join(chains)
    assert "[2:a]" in graph and "[3:a]" not in graph and "volume=-10dB" in graph
    assert graph.index("acompressor") < graph.index("loudnorm")
    assert "acompressor" not in ";".join(
        build_audio_filters(source="0:a", duration_s=10.0, end_card_s=0.0, target_lufs=-14)
    )


def test_render_command_adds_effect_inputs(params_factory: Callable[..., FactoryParams]) -> None:
    from sixstars.factory.audio import SfxTrack

    spec = RenderSpec(
        source=SourceInfo(PurePath("src.mp4"), 1920, 1080),
        window=ClipWindow(5.0, 15.0),
        layout=LayoutB(),
        ass_path=PurePath("clip.ass"),
        output_path=PurePath("out.mp4"),
        series_no=1,
        params=params_factory(end_card_s=0),
        sfx_path=PurePath("pop.wav"),
        sfx_times_s=(1.0,),
        sfx_tracks=(SfxTrack(PurePath("boom.wav"), (7.0,)), SfxTrack(PurePath("whoosh.wav"), ())),
        compress=True,
        watermark_y=450,
    )
    command = build_render_command(spec)
    inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
    assert inputs == ["src.mp4", "pop.wav", "boom.wav"]
    graph = command[command.index("-filter_complex") + 1]
    assert "between(" not in graph and "y=450" in graph and "concat" not in graph


# ── title bar ────────────────────────────────────────────────


def _mono_width(text: str, size: int) -> float:
    return len(text) * size * 0.5


def test_title_splits_runs_and_wraps_fill_first() -> None:
    from sixstars.factory.titlebar import TITLE_MAX_WIDTH, TITLE_PAD_X, fit_title, split_runs

    assert split_runs("as a 😂 fake 🙏") == [("as a ", False), ("😂", True), (" fake ", False), ("🙏", True)]
    fit = fit_title("Trolling Players as a 😂 fake NPC 🙏", _mono_width)
    assert fit is not None and fit.size == 64 and len(fit.lines) == 2
    assert fit.lines[0].startswith("Trolling Players") and len(fit.lines[0]) > len(fit.lines[1])
    assert fit.box_w <= TITLE_MAX_WIDTH and fit.box_w > 2 * TITLE_PAD_X
    long = fit_title(" ".join(["word"] * 40), _mono_width)
    assert long is not None and len(long.lines) <= 2 and long.lines[-1].endswith("…")
    assert fit_title("   ", _mono_width) is None


def test_title_bar_png_is_drawn_and_overlaid(
    tmp_path: Path, params_factory: Callable[..., FactoryParams]
) -> None:
    from PIL import Image

    from sixstars.factory.render import title_filters
    from sixstars.factory.titlebar import draw_title_bar

    assets = load_assets("Montserrat Black")
    assert assets.title_font_file is not None and assets.emoji_font_file is not None
    bar = draw_title_bar(
        "This boat heist went SO wrong 😭",
        tmp_path / "title.png",
        font_file=assets.title_font_file,
        emoji_font=assets.emoji_font_file,
        emoji_colour=assets.emoji_colour,
    )
    assert bar is not None
    image = Image.open(bar.path)
    assert image.size == (bar.width, bar.height) and image.mode == "RGBA"
    assert image.getpixel((bar.width // 2, 2)) == (255, 255, 255, 255)  # white box
    spec = RenderSpec(
        source=SourceInfo(PurePath("src.mp4"), 1920, 1080),
        window=ClipWindow(0.0, 10.0),
        layout=LayoutB(),
        ass_path=PurePath("clip.ass"),
        output_path=PurePath("out.mp4"),
        series_no=1,
        params=params_factory(width=720, height=1280, end_card_s=0),
        title_png=PurePath("title.png"),
        title_top=220,
    )
    chains = title_filters(spec, Geometry.for_canvas(720, 1280), source="v_in", out="v_titled")
    assert chains[0].startswith("movie=filename=title.png") and "scale=" in chains[0]
    assert chains[1] == "[v_in][v_titled_img]overlay=x=(W-w)/2:y=147:enable=lt(t\\,3)[v_titled]"
    graph = ";".join(build_render_command(spec))
    assert "[v_progress][v_titled_img]overlay" in graph and "[v_titled]ass=" in graph
    # A hook longer than the clip is shown for the whole clip, and none at all when switched off.
    longer = replace(spec, params=params_factory(width=720, height=1280, end_card_s=0, hook_seconds=30))
    canvas = Geometry.for_canvas(720, 1280)
    assert "enable=lt(t\\,10)" in title_filters(longer, canvas, source="a", out="b")[1]
    off = replace(spec, params=params_factory(width=720, height=1280, end_card_s=0, hook_seconds=0))
    assert "v_titled" not in ";".join(build_render_command(off))


# ── writer payoff and assets ─────────────────────────────────


def _writing(**overrides: object) -> ClipWriting:
    base: dict[str, object] = {
        "start_s": 2.0,
        "end_s": 22.0,
        "hook": "He did NOT expect that",
        "title": "t",
        "caption": "c",
        "hashtags": ["a", "b", "c"],
        "topic": "car-crash",
        "spoiler": False,
        "safety": {
            "politics": False,
            "gambling": False,
            "sponsored": False,
            "real_violence": False,
            "sexual": False,
            "personal_info": False,
            "hate": False,
            "copyrighted_media": False,
        },
        "score_parts": {"hook": 7, "clarity": 7, "payoff": 7, "rewatch": 7},
        "score": 7,
        "reject": False,
    }
    base.update(overrides)
    return ClipWriting.model_validate(base)


def test_payoff_fields_are_optional_decorations() -> None:
    assert _writing().payoff_s is None and _writing().payoff_emoji is None
    good = _writing(payoff_s=18.5, payoff_emoji=" 💀 ")
    assert good.payoff_s == 18.5 and good.payoff_emoji == "💀"
    assert _writing(payoff_s=30.0).payoff_s is None  # outside the cut: dropped, not an error
    assert _writing(payoff_emoji="LOL").payoff_emoji is None


def test_style_assets_are_bundled() -> None:
    assets = load_assets("Montserrat Black", caption_font="Anton")
    assert assets.caption_font_file == REPO / "assets" / "fonts" / "Anton-Regular.ttf"
    assert set(assets.sfx) == {"whoosh", "impact", "boom"}
    assert (REPO / "assets" / "fonts" / "OFL-Anton.txt").is_file()
    assert load_assets("Montserrat Black").caption_font_file == assets.font_file

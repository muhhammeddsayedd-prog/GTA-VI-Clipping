"""Design styles, Phase B (development-plan/M18): the clip writer's moment type and texts, the styles that
need them, the director's affinity, layouts H (news card) and P (POV) and the drawn texts. No network."""

from pathlib import Path

import pytest
from PIL import Image

from sixstars.ai.schemas import ClipWriting
from sixstars.factory import cards
from sixstars.factory.assets import load_assets
from sixstars.factory.director import (
    DEFAULT_DIRECTOR,
    AccountSlot,
    DirectorRules,
    clip_facts,
    plan_styles,
)
from sixstars.factory.layout import (
    C_BAND_TOP,
    C_FACECAM_H,
    C_QUESTION_GAP,
    H_HEADER_TOP,
    H_VIDEO_H,
    H_VIDEO_Y,
    LOW_CREDIT_BOTTOM,
    P_TEXT_CENTER_Y,
    Box,
    Geometry,
    LayoutA,
    LayoutB,
    LayoutCard,
    LayoutPov,
    card_crop,
    gameplay_band,
    layout_filters,
)
from sixstars.factory.overlays import CREDIT_SIZE
from sixstars.factory.params import FactoryParams
from sixstars.factory.pipeline import chosen_title, framed_layout
from sixstars.factory.style import HEADLINE_CARD, POV_FULLFRAME, StyleRules
from sixstars.factory.styles import DEFAULT_STYLES, StyleCatalog
from sixstars.factory.subtitles import SubtitleStyle, credit_bottom
from sixstars.factory.variants import recipe_json

# The five styles of phases A and B (phases C and E have their own tests).
CATALOG = StyleCatalog.from_setting(
    {k: v for k, v in DEFAULT_STYLES.items() if k not in ("cam_top_wordcap", "hype_montage")}
)
ACCOUNTS = [
    AccountSlot("instagram_main", "instagram", ("hourly", "spike")),
    AccountSlot("tiktok_main", "tiktok", ("hourly", "spike")),
    AccountSlot("youtube_main", "youtube", ("hourly", "spike")),
]
CAM = Box(0, 300, 260, 166)
BASE_REPLY: dict[str, object] = {
    "start_s": 0,
    "end_s": 10,
    "hook": "Hook",
    "title": "Title",
    "caption": "Caption",
    "hashtags": ["a", "b", "c"],
    "topic": "x",
    "spoiler": False,
    "safety": {},
    "score_parts": {"hook": 5, "clarity": 5, "payoff": 5, "rewatch": 5},
    "score": 5,
    "reject": False,
}


# ── the clip writer's texts ──────────────────────────────────


def test_the_writer_texts_are_kept_when_they_follow_their_rules() -> None:
    writing = ClipWriting.model_validate(
        {
            **BASE_REPLY,
            "moment_type": "NPC funny",
            "title_line": "NPC has a pet alligator 😭",
            "headline": "Speed learns you can't steal every car in GTA 6 👀",
            "question": "Can you punch a bird in GTA 6? 🐦",
            "pov_line": "POV this doesn't feel real at all 😭🔥",
        }
    )
    assert writing.moment_type == "npc_funny"
    assert set(writing.style_texts()) == {"title_line", "headline", "question", "pov_line"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("question", "Not a question"),
        ("pov_line", "this is not a pov line"),
        ("headline", "x" * 91),
        ("title_line", "y" * 49),
        ("title_line", "follow @6starsclips"),
        ("headline", "Big news #gta6"),
    ],
)
def test_a_text_that_breaks_its_rule_is_dropped_not_an_error(field: str, value: str) -> None:
    writing = ClipWriting.model_validate({**BASE_REPLY, field: value, "moment_type": "nonsense"})
    assert writing.style_texts() == {} and writing.moment_type is None


# ── catalogue and director ───────────────────────────────────


def test_the_phase_b_styles_need_the_writers_text_they_show() -> None:
    assert CATALOG.ids == (
        "cam_top_question",
        "cam_top_title",
        "cam_top_viral",
        "headline_card",
        "pov_fullframe",
    )
    assert CATALOG.get("headline_card").requires == ("headline",)  # type: ignore[union-attr]
    assert CATALOG.get("pov_fullframe").requires == ("pov_line",)  # type: ignore[union-attr]
    assert CATALOG.get("cam_top_question").requires == ("question",)  # type: ignore[union-attr]
    assert CATALOG.get("cam_top_viral").requires == ()  # type: ignore[union-attr]
    assert StyleCatalog.from_setting({"x": {"requires": ["unicorn"]}, "y": {}}).ids == ("y",)


def test_a_clip_without_the_texts_only_gets_the_styles_that_need_none() -> None:
    plan = plan_styles("c1", "hourly", ACCOUNTS, CATALOG, DirectorRules(), facts=frozenset({"facecam"}))
    assert set(plan.by_account.values()) <= {"cam_top_viral", "cam_top_title"}
    assert set(plan.choices["tiktok_main"].unmet) == {"headline_card", "pov_fullframe", "cam_top_question"}


def test_with_every_text_the_three_accounts_get_three_different_styles() -> None:
    facts = clip_facts(
        {"headline": "h", "pov_line": "POV x", "question": "q?", "title_line": "t"}, has_facecam=True
    )
    assert facts == {"facecam", "headline", "pov_line", "question", "title_line"}
    for n in range(10):
        plan = plan_styles(f"c{n}", "hourly", ACCOUNTS, CATALOG, DirectorRules(), facts=facts)
        assert len(set(plan.by_account.values())) == 3
        assert plan.as_json()["facts"] == sorted(facts)


def test_the_moment_type_steers_the_pick() -> None:
    rules = DirectorRules.from_setting(
        {**DEFAULT_DIRECTOR, "affinity": {"discovery": {"headline_card": 1000}}}
    )
    facts = clip_facts({"headline": "h", "question": "q?"}, has_facecam=True)
    picks = [
        plan_styles(f"c{n}", "hourly", ACCOUNTS[:1], CATALOG, rules, facts=facts, moment_type="discovery")
        for n in range(20)
    ]
    assert all(plan.by_account["instagram_main"] == "headline_card" for plan in picks)
    assert DirectorRules.from_setting(DEFAULT_DIRECTOR).multiplier("reaction", "cam_top_viral") == 3.0
    assert DirectorRules.from_setting(DEFAULT_DIRECTOR).multiplier(None, "cam_top_viral") == 1.0
    bad = DirectorRules.from_setting({"affinity": {"x": {"a": -1, "b": True, "c": 2}, "y": "no"}})
    assert bad.affinity == {"x": {"c": 2.0}}


# ── layouts H and P ──────────────────────────────────────────


def test_the_frame_of_the_style_wins_over_the_facecam() -> None:
    found = LayoutA(facecam_box=CAM)
    card = framed_layout(found, StyleRules.from_setting(HEADLINE_CARD))
    assert card == LayoutCard(facecam_box=CAM) and card.kind == "H"
    pov = framed_layout(LayoutB(), StyleRules.from_setting(POV_FULLFRAME))
    assert pov == LayoutPov() and pov.kind == "P"
    assert framed_layout(card, StyleRules(facecam_top=True)) == LayoutA(facecam_box=CAM, cam_top=True)
    assert framed_layout(LayoutPov(), StyleRules()) == LayoutB()


def test_the_news_card_crop_keeps_the_facecam() -> None:
    centred = card_crop(None, 1920, 1080)
    assert (centred.w, centred.h, centred.x) == (1440, 1080, 240)
    left = card_crop(Box(0, 600, 300, 200), 1920, 1080)
    assert left.x == 0
    right = card_crop(Box(1700, 0, 220, 200), 1920, 1080)
    assert right.x + right.w == 1920
    assert card_crop(Box(0, 0, 1800, 200), 1920, 1080) == centred, "a facecam wider than the crop is ignored"


def test_layouts_h_and_p_pad_one_crop_into_the_frame() -> None:
    g = Geometry.for_canvas(1080, 1920)
    chains = layout_filters(
        LayoutCard(facecam_box=CAM), source_w=1920, source_h=1080, geometry=g, fps=30, duration_s=10.0
    )
    assert len(chains) == 1 and f"pad=w=1080:h=1920:x=0:y={H_VIDEO_Y}:color=black" in chains[0]
    assert chains[0].endswith("[v_layout]")
    pov = layout_filters(LayoutPov(), source_w=1920, source_h=1080, geometry=g, fps=30, duration_s=10.0)
    assert "pad=w=1080:h=1920:x=0:y=240:color=black" in pov[0] and "h=1440" in pov[0]


def test_the_gate_bands_avoid_every_text(params: FactoryParams) -> None:
    h_top, h_bottom = (round(x * 1920) for x in gameplay_band("H"))
    assert h_top > H_VIDEO_Y and h_bottom < H_VIDEO_Y + H_VIDEO_H < LOW_CREDIT_BOTTOM - CREDIT_SIZE
    p_top, p_bottom = (round(x * 1920) for x in gameplay_band("P"))
    pov_height = 2 * cards.POV_SIZES[0] * cards.LINE_PITCH + 2 * (cards.POV_STROKE + 8)
    assert p_bottom <= P_TEXT_CENTER_Y - pov_height / 2
    c_top, c_bottom = (round(x * 1920) for x in gameplay_band("C"))
    question_height = 2 * cards.QUESTION_SIZES[0] * cards.LINE_PITCH + 2 * (cards.QUESTION_STROKE + 8)
    assert C_FACECAM_H + C_QUESTION_GAP + question_height <= C_BAND_TOP == c_top
    viral = SubtitleStyle.from_rules(CATALOG.rules_for("cam_top_viral"))
    assert c_bottom <= credit_bottom("C", params, viral) - CREDIT_SIZE
    assert credit_bottom("H", params) == credit_bottom("P", params) == LOW_CREDIT_BOTTOM


# ── the shown text ───────────────────────────────────────────


def test_each_style_shows_its_own_text_or_the_hook() -> None:
    texts = {"title_line": "Speed vs the truck", "headline": "Speed learns X", "question": "Can you X?"}
    title = CATALOG.rules_for("cam_top_title")
    assert chosen_title("Hook", texts, "😭", title) == "Speed vs the truck 😭"
    assert chosen_title("Hook", texts, "😭", CATALOG.rules_for("headline_card")) == "Speed learns X"
    assert chosen_title("Hook", texts, "😭", CATALOG.rules_for("cam_top_question")) == "Can you X?"
    assert chosen_title("Hook", {}, "😭", CATALOG.rules_for("pov_fullframe")) == "Hook 😭"
    assert chosen_title("Hook", texts, "😭", CATALOG.rules_for("cam_top_viral")) == "Hook"


def test_fit_lines_wraps_shrinks_and_cuts() -> None:
    def width(text: str, size: int) -> float:
        return len(text) * size * 0.5

    fit = cards.fit_lines("one two three four", (60, 40), 300, 2, width)
    assert fit is not None and fit.size == 60 and fit.lines == ("one two", "three four")
    short = cards.fit_lines("one two three four", (60, 40), 300, 2, width, max_height=100)
    assert short is not None and short.size == 40, "two lines of 60 are taller than 100"
    long = cards.fit_lines("word " * 40, (60, 40), 300, 2, width)
    assert long is not None and long.size == 40 and long.lines[-1].endswith("…") and len(long.lines) == 2
    assert cards.fit_lines("   ", (60,), 300, 2, width) is None


def test_the_texts_are_drawn_with_the_bundled_fonts(tmp_path: Path) -> None:
    assets = load_assets("Montserrat Black")
    assert assets.title_font_file is not None and assets.question_font_file is not None
    assert assets.avatar is not None
    card = cards.draw_headline_card(
        "6 Stars Clips",
        "Speed learns you can't steal every car in GTA 6",
        tmp_path / "card.png",
        font_file=assets.title_font_file,
        avatar=assets.avatar,
        emoji_font=None,
        emoji_colour=False,
    )
    assert card is not None and card.width == cards.CARD_WIDTH
    assert card.height > cards.CARD_AVATAR
    long_card = cards.draw_headline_card(
        "6 Stars Clips",
        "Speed learns the hard way that you cannot steal every single parked car in GTA 6 at all",
        tmp_path / "long.png",
        font_file=assets.title_font_file,
        avatar=assets.avatar,
        emoji_font=None,
        emoji_colour=False,
    )
    assert long_card is not None
    for drawn in (card, long_card):
        assert H_HEADER_TOP + drawn.height <= H_VIDEO_Y, "the card ends above the stream"
    question = cards.draw_outline_text(
        "Can you steal any car in GTA 6?",
        tmp_path / "q.png",
        font_file=assets.question_font_file,
        weight=None,
        sizes=cards.QUESTION_SIZES,
        max_width=cards.QUESTION_MAX_WIDTH,
        fill=cards.QUESTION_RED,
        stroke=cards.QUESTION_STROKE,
        emoji_font=None,
        emoji_colour=False,
        caps=True,
    )
    assert question is not None and question.width <= cards.QUESTION_MAX_WIDTH
    with Image.open(question.path) as image:
        assert image.mode == "RGBA" and image.size == (question.width, question.height)


def test_the_recipe_carries_the_texts() -> None:
    recipe = recipe_json(
        lines=(),
        layout=LayoutB(),
        duration_s=5.0,
        series_no=1,
        mode="normal",
        lane="hourly",
        creator="c",
        hook="h",
        keywords={},
        duck=(),
        payoff_s=None,
        payoff_emoji=None,
        texts={"headline": "Speed learns X"},
    )
    assert recipe["texts"] == {"headline": "Speed learns X"}

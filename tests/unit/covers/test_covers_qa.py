"""Cover engine C6 (docs/29a stage 5): the QA gate's rule checks. No network, no models."""

from pathlib import Path

import pytest

from sixstars.covers import qa
from sixstars.covers.qa import CoverFacts, QaResult, rule_problems


def facts(**overrides: object) -> CoverFacts:
    values: dict[str, object] = {
        "layout": "split",
        "line": "BIKE LANE RAGE",
        "line_style": "quote",
        "cover_path": Path("c.jpg"),
        "preview_path": Path("p.jpg"),
        "parts_dir": None,
        "text_rect": (120, 880, 840, 300),
        "face_h_px": 280.0,
        "face_rect": (400, 500, 240, 280),
        "small_text": "BIKE LANE RAGE",
        "full_text": "BIKE LANE RAGE @kaicenat",
        "luma": 120.0,
        "clipped": 0.005,
        "phash": 0x0F0F0F0F0F0F0F0F,
    }
    values.update(overrides)
    return CoverFacts(**values)  # type: ignore[arg-type]


def test_a_good_cover_passes_the_rules() -> None:
    assert rule_problems(facts()) == []


@pytest.mark.parametrize(
    ("override", "problem"),
    [
        ({"small_text": "BIKE LAN"}, "ocr"),
        ({"text_rect": (40, 1800, 900, 200)}, "text_outside_safe_band"),
        ({"full_text": "WHAT IS HAPPENING МЕРТВА"}, "foreign_text"),
        ({"face_h_px": None, "face_rect": None}, "no_face"),
        ({"face_h_px": 120.0}, "face_size=120"),
        ({"face_rect": (400, 200, 240, 280)}, "face_outside_safe_band"),
        ({"luma": 30.0}, "exposure:luma=30"),
        ({"clipped": 0.05}, "exposure:clipped=0.050"),
        ({"line": "THE GTA 6 ENDING", "small_text": "THE GTA 6 ENDING"}, "story_spoiler"),
        (
            {"line": "HE REALLY DID NOT SEE THAT", "small_text": "HE REALLY DID NOT SEE THAT"},
            "too_many_words",
        ),
    ],
)
def test_each_rule(override: dict[str, object], problem: str) -> None:
    assert problem in rule_problems(facts(**override))


def test_duplicates() -> None:
    same = facts().phash
    assert same is not None
    assert "duplicate:phash=0" in rule_problems(facts(), recent_hashes=[same])
    far = same ^ 0xFFFF_FFFF  # 32 bits apart
    assert rule_problems(facts(), recent_hashes=[far]) == []
    arms = [("split", "quote"), ("split", "quote")]
    assert "duplicate:arm" in rule_problems(facts(), recent_arms=arms)
    assert "duplicate:arm" not in rule_problems(
        facts(), recent_arms=[("split", "quote"), ("face_led", "quote")]
    )


def test_a_plain_cover_skips_the_text_rules() -> None:
    assert rule_problems(facts(line="", small_text="", text_rect=None)) == []


def test_helpers() -> None:
    assert qa.similarity("BIKE LANE RAGE!", "bike lane rage") == 1.0
    assert qa.has_foreign_script("МЕРТВА") and not qa.has_foreign_script("Café GTA 6")
    assert qa.inside((60, 400, 900, 1120)) and not qa.inside((59, 400, 10, 10))
    assert qa.hamming(0b1011, 0b0001) == 2
    assert qa.three_in_a_row([("a", "b"), ("a", "b")], ("a", "b"))
    assert not qa.three_in_a_row([("a", "b")], ("a", "b"))


def test_qa_result() -> None:
    vision = {"scores": {}, "total": 8, "hard_fail": False, "reason": "ok"}
    assert QaResult((), vision, 7).passed
    assert not QaResult((), {**vision, "total": 6}, 7).passed
    assert not QaResult((), {**vision, "hard_fail": True}, 7).passed
    assert not QaResult(("ocr",), vision, 7).passed
    assert not QaResult((), None, 7).passed

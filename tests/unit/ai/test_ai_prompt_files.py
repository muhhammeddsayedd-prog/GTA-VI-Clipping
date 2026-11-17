"""Prompt files: every prompt loads, has both sections and renders with the values the calls pass."""

import pytest

from sixstars.ai import prompt_files
from sixstars.ai.prompt_files import PromptFileError

SYSTEM_VALUES = {"game": "Grand Theft Auto V", "watermark": "@6starsclipping"}


@pytest.mark.parametrize("prompt_id", prompt_files.PROMPT_IDS)
def test_every_prompt_loads_and_renders_its_system(prompt_id: str) -> None:
    prompt = prompt_files.load(prompt_id)
    system = prompt.render_system(SYSTEM_VALUES)
    assert system.startswith("You work for 6 Stars Clips (@6starsclipping)")
    assert "Grand Theft Auto V" in system
    assert "You answer only with JSON" in system
    assert "{" not in system and "}" not in system
    assert "{" in prompt.user_template


def test_prompt_ids_match_the_files() -> None:
    files = {path.stem for path in prompt_files.PROMPTS_DIR.glob("p*.md")}
    assert files == set(prompt_files.PROMPT_IDS)


def test_system_prompt_is_identical_between_calls() -> None:
    """The system block is cached by the API: nothing per-request may leak into it."""
    prompt = prompt_files.load("p2_clip_writer")
    assert prompt.render_system(SYSTEM_VALUES) == prompt.render_system(dict(SYSTEM_VALUES))


def test_clip_writer_prompt_carries_the_brand_rules() -> None:
    system = prompt_files.load("p2_clip_writer").render_system(SYSTEM_VALUES)
    for needle in (
        "at most 8 words",
        "at most 70 characters",
        "at most 120 characters",
        "3 to 5",
        "hype",
        "gambling",
        "sponsored",
        "politics",
        "spoiler",
        "English",
    ):
        assert needle in system


def test_parse_errors() -> None:
    with pytest.raises(PromptFileError, match="sections"):
        prompt_files.parse("x", "## System\nhello")
    with pytest.raises(PromptFileError, match="before"):
        prompt_files.parse("x", "## User template\nu\n## System\ns")
    with pytest.raises(PromptFileError, match="empty"):
        prompt_files.parse("x", "## System\n\n## User template\nu")
    with pytest.raises(PromptFileError, match="not found"):
        prompt_files.load("p9_missing")


def test_missing_placeholder_value_is_reported() -> None:
    prompt = prompt_files.parse("x", "## System\n{preamble} s\n## User template\nHello {name}")
    with pytest.raises(PromptFileError, match="name"):
        prompt.render_user({})
    assert prompt.render_user({"name": '{"json": 1}'}) == 'Hello {"json": 1}'

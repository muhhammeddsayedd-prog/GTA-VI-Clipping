"""Title rules (YouTube lane tuning): a title never states the outcome; one retry, then the old behaviour."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from ai_samples import clip_context, writing_reply
from structlog.testing import capture_logs

from sixstars.ai import calls
from sixstars.ai.titles import correction_note, spoils_ending
from sixstars.clients.anthropic_client import AnthropicClient

Scripted = Callable[..., tuple[AnthropicClient, Any]]
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "title",
    [
        "DarkViperAU's getaway ends in a crash",
        "Speedy's boat ride ends in the ocean",
        "His heist ENDS WITH the cops",
        "xQc crashes the jet into the tower",
        "Kai crashed the bus",
        "Nick dies to one NPC",
        "The yacht sinks with everyone on it",
        "Speedy fails at the easiest jump",
    ],
)
def test_a_title_that_states_the_outcome_is_caught(title: str) -> None:
    assert spoils_ending(title) is not None


@pytest.mark.parametrize(
    "title",
    [
        "Elajjaz's aim goes horribly wrong in GTA 6 😭",
        "ohnePixel's shirt has a very questionable slogan 😭",
        "DarkViperAU had ONE job",
        "Chat lost it when Speedy saw the cops",
        "Kai's plan goes wrong",
        "The crash test dummy challenge",  # "crash" as a noun is fine
        "Endless chase through Vice City",
        None,
        "",
    ],
)
def test_a_title_that_sets_up_the_tension_passes(title: str | None) -> None:
    assert spoils_ending(title) is None


def test_the_correction_note_names_the_title_and_the_rule() -> None:
    note = correction_note("X's car ends in the sea", 'the title states the outcome ("ends in")')
    assert "X's car ends in the sea" in note and "whole JSON" in note and "had ONE job" in note


def test_a_spoiling_title_is_rewritten_once(scripted: Scripted) -> None:
    spoiler = json.dumps(writing_reply(title="Speedy's jump ends in a crash"))
    fixed = json.dumps(writing_reply(title="Speedy's jump goes horribly wrong"))
    client, sdk = scripted(spoiler, fixed)

    writing = calls.write_clip(clip_context(now=NOW), client=client)

    assert writing.title == "Speedy's jump goes horribly wrong"
    assert len(sdk.requests) == 2
    retry_user = sdk.requests[1]["messages"][0]["content"][-1]["text"]
    assert "Speedy's jump ends in a crash" in retry_user and "never" in retry_user


def test_a_title_still_spoiling_after_the_retry_keeps_the_first_reply(scripted: Scripted) -> None:
    first = json.dumps(writing_reply(title="Speedy's jump ends in a crash", hook="First hook 😭"))
    second = json.dumps(writing_reply(title="Speedy crashes the bike", hook="Second hook 😭"))
    client, sdk = scripted(first, second)

    with capture_logs() as logs:
        writing = calls.write_clip(clip_context(now=NOW), client=client)

    assert (writing.title, writing.hook) == ("Speedy's jump ends in a crash", "First hook 😭")
    assert len(sdk.requests) == 2
    assert any(log["event"] == "title_spoiler_kept" for log in logs)


def test_a_good_title_makes_one_call(scripted: Scripted) -> None:
    client, sdk = scripted(json.dumps(writing_reply(title="Speedy had ONE job")))

    assert calls.write_clip(clip_context(now=NOW), client=client).title == "Speedy had ONE job"
    assert len(sdk.requests) == 1


def test_a_failed_retry_keeps_the_first_reply(scripted: Scripted) -> None:
    spoiler = json.dumps(writing_reply(title="Speedy's jump ends in a crash"))
    client, _ = scripted(spoiler, "not json", "still not json", "never json")

    writing = calls.write_clip(clip_context(now=NOW), client=client)

    assert writing.title == "Speedy's jump ends in a crash"

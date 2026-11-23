"""Title writer v2 for one clip with the database and Claude stubbed: retry, fallback, apply, gate hook."""

from collections.abc import Mapping
from typing import Any

import pytest

from sixstars import events, settings, usage
from sixstars.clients.fakes import FakeAnthropicClient
from sixstars.scoring import config as scoring_config
from sixstars.scoring import log as scoring_log
from sixstars.titles import service, store

CLIP = "11111111-1111-1111-1111-111111111111"
ROW: dict[str, Any] = {
    "id": CLIP,
    "status": "approved",
    "title": "Kai Cenat's bike beef gets out of hand",
    "caption": "old caption",
    "hashtags": ["bike"],
    "hook": "He did NOT expect that 😭",
    "topic": "streamer-argues-with-cyclist",
    "spoiler": False,
    "transcript": {
        "words": [
            {"w": "bro", "s": 1.0, "e": 1.2},
            {"w": "you", "s": 1.3, "e": 1.4},
            {"w": "hit", "s": 1.5, "e": 1.7},
            {"w": "my", "s": 1.8, "e": 1.9},
            {"w": "car", "s": 2.0, "e": 2.3},
        ]
    },
    "start_s": 20000.0,
    "duration_s": 30.0,
    "score_parts": {"visual": {"seen": "A cyclist rams Kai's car and Kai chases him down the street."}},
    "style_texts": None,
    "gate_profile": "hourly",
    "source_type": "vod",
    "source_platform": "twitch",
    "spike_id": None,
    "creator_id": "22222222-2222-2222-2222-222222222222",
    "handle": "kaicenat",
    "display_name": "KaiCenat",
    "title_name": "Kai Cenat",
    "title_aliases": ["kai cenat", "kaicenat"],
    "follower_count": 21_998_769,
    "spike_mode": None,
    "spike_offset_s": None,
    "chat_sample": [{"t_rel": 0, "text": "KEKW cyclist"}, {"t_rel": 1, "text": "cyclist KEKW"}],
    "live_offset_s": None,
}
GOOD = {
    "candidates": [
        {"pattern": "fail", "title": "Kai Cenat's Cyclist Chase Falls Apart in GTA 6 💀",
         "facts_used": ["event"]},
        {"pattern": "stakes", "title": "Kai Cenat Chases Down a Cyclist Who Hit His Car in GTA 6 😭",
         "facts_used": ["event"]},
    ],
    "caption_short": "kai chased a cyclist down the whole street 😭",
}  # fmt: skip
BAD = {
    "candidates": [{"pattern": "fail", "title": "Chat loses it at Kai", "facts_used": []}],
    "caption_short": "chat lost it",
}
FLAG = {"enabled": True, "explore_share": 0.0, "s_tier_names": ["Kai Cenat"], "tag_tail": ["streamerclips"]}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"runs": [], "applied": [], "log": [], "events": [], "status": "approved"}
    values = {
        "ai.model_writer": "claude-sonnet-5-5",
        "ai.model_fast": "claude-haiku-5-5",
        "ai.daily_budget_usd": 25,
        "env.target_game": "Grand Theft Auto VI",
        "factory.watermark": "@6starsclips",
    }
    monkeypatch.setattr(settings, "_all", lambda: values)
    monkeypatch.setattr(usage, "spent_today", lambda provider: 0.0)
    monkeypatch.setattr(usage, "record", lambda *a, **k: False)
    scoring_config.set_cache({"titles.v2": FLAG})
    monkeypatch.setattr(store, "clip_row", lambda clip_id: dict(ROW) if clip_id == CLIP else None)
    monkeypatch.setattr(store, "book_rows", lambda: [])
    monkeypatch.setattr(store, "already_titled", lambda clip_id: False)

    def insert_run(clip_id: str, **kwargs: Any) -> int:
        seen["runs"].append(kwargs)
        return len(seen["runs"])

    def apply(run_id: int, clip_id: str, title: str, caption: str, tags: list[str]) -> bool:
        if seen["status"] == "posted":
            return False
        seen["applied"].append((run_id, title, caption, list(tags)))
        return True

    monkeypatch.setattr(store, "insert_run", insert_run)
    monkeypatch.setattr(store, "apply", apply)
    monkeypatch.setattr(scoring_log, "record", lambda stage, **k: seen["log"].append((stage, k)) or 0)
    monkeypatch.setattr(events, "emit", lambda type_, **k: seen["events"].append((type_, k)) or 1)
    return seen


def test_applies_the_best_surviving_candidate(world: dict[str, Any]) -> None:
    client = FakeAnthropicClient({"p11_title_writer": GOOD})
    outcome = service.retitle(CLIP, apply=True, client=client)
    assert outcome.title == "Kai Cenat Chases Down a Cyclist Who Hit His Car in GTA 6 😭"
    assert outcome.pattern == "stakes" and outcome.source == "v2" and outcome.applied
    assert world["applied"] == [
        (1, outcome.title, "kai chased a cyclist down the whole street 😭", ["kaicenat", "streamerclips"])
    ]
    run = world["runs"][0]
    assert run["prior_title"] == ROW["title"]
    assert run["facts"]["numbers"] == {}  # hours into the stream are not a fact
    assert run["facts"]["event"].startswith("A cyclist rams")
    assert run["features"]["title_pattern"] == "stakes"
    assert world["log"][0][0] == "title"
    assert world["events"][0][0] == "clip.retitled"
    call = client.calls_to("complete_json")[0]
    assert call["prompt_id"] == "p11_title_writer" and call["model"] == "claude-haiku-5-5"


def test_retries_once_then_uses_the_retry(world: dict[str, Any]) -> None:
    client = FakeAnthropicClient({"p11_title_writer": [BAD, GOOD]})
    outcome = service.retitle(CLIP, apply=True, client=client)
    assert len(client.calls) == 2
    assert "None of your candidates passed" in client.calls_to("complete_json")[1]["user_content"]
    assert outcome.source == "v2" and outcome.pattern == "stakes"
    assert len(outcome.candidates) == 3  # both rounds are stored


def test_falls_back_after_two_failed_rounds(world: dict[str, Any]) -> None:
    client = FakeAnthropicClient({"p11_title_writer": [BAD, BAD]})
    outcome = service.retitle(CLIP, apply=True, client=client)
    assert outcome.source == "fallback"
    assert outcome.title == "Kai Cenat Argues With Cyclist in GTA 6 😭"
    assert outcome.caption == "Kai Cenat Argues With Cyclist in GTA 6"  # the bad caption is replaced
    assert world["events"][0][0] == "clip.title_fallback"


def test_dry_run_changes_no_clip(world: dict[str, Any]) -> None:
    outcome = service.retitle(CLIP, apply=False, client=FakeAnthropicClient({"p11_title_writer": GOOD}))
    assert not outcome.applied and world["applied"] == [] and world["events"] == []
    assert len(world["runs"]) == 1


def test_a_posted_clip_is_not_changed(world: dict[str, Any]) -> None:
    world["status"] = "posted"
    outcome = service.retitle(CLIP, apply=True, client=FakeAnthropicClient({"p11_title_writer": GOOD}))
    assert not outcome.applied and world["events"] == []


def test_gate_hook_does_nothing_with_the_flag_off(world: dict[str, Any]) -> None:
    scoring_config.set_cache({"titles.v2": {**FLAG, "enabled": False}})
    client = FakeAnthropicClient({"p11_title_writer": GOOD})
    assert service.after_approval(CLIP, client=client) is None
    assert client.calls == []


def test_gate_hook_never_raises(world: dict[str, Any]) -> None:
    client = FakeAnthropicClient({"p11_title_writer": RuntimeError("API down")})
    assert service.after_approval(CLIP, client=client) is None
    assert world["applied"] == []


def test_gate_hook_applies_with_the_flag_on(world: dict[str, Any]) -> None:
    outcome = service.after_approval(CLIP, client=FakeAnthropicClient({"p11_title_writer": GOOD}))
    assert outcome is not None and outcome.applied


def test_peak_in_clip() -> None:
    assert service.peak_in_clip({"spike_mode": "replay", "spike_offset_s": 110, "start_s": 100}) == 10


def test_options_from_flag() -> None:
    options = service.Options.from_flag(
        {"model": "writer", "explore_share": 0.3, "extra_names": {"N3on": ["N3ON"], "bad": "x"}}
    )
    assert options.model == "writer" and options.explore_share == 0.3
    assert options.extra_names == {"N3on": ["n3on"]}
    assert options.tag_tail == ("streamerclips",)


def _row(**changes: Any) -> Mapping[str, Any]:
    return {**ROW, **changes}


def test_event_falls_back_to_the_headline_then_the_caption() -> None:
    assert service._event(_row(score_parts={})) == "old caption"
    assert service._event(_row(score_parts=None, style_texts={"headline": "Kai fights a cyclist"})) == (
        "Kai fights a cyclist"
    )

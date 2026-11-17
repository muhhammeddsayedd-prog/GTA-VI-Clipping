"""Canned model replies and inputs shared by the AI layer tests."""

from dataclasses import dataclass
from typing import Any

from sixstars.ai.calls import ChatLine, ClipContext, SpikeContext


@dataclass(frozen=True)
class FakeLine:
    index: int
    start_s: float
    end_s: float
    text: str


LINES = [
    FakeLine(0, 2.0, 3.1, "okay watch this"),
    FakeLine(1, 3.4, 5.0, "I'm gonna jump it"),
    FakeLine(2, 9.5, 11.0, "no way no way"),
    FakeLine(3, 20.0, 22.5, "he landed on the cop"),
    FakeLine(4, 30.0, 31.0, "that's five grand gone"),
]


def spike(spike_id: str = "sp-1") -> SpikeContext:
    return SpikeContext(
        spike_id=spike_id,
        creator="speedy",
        platform="twitch",
        stream_title="GTA RP | cops and robbers",
        score_z=4.6,
        msgs=82,
        uniq=61,
        top_tokens=("LMAO", "NO WAY", "clip"),
        chat=(ChatLine(-2.0, "LMAOOO"), ChatLine(0.5, "NO WAY he landed that", "viewer1")),
        transcript="okay watch this I'm gonna jump it no way",
    )


def clip_context(**overrides: Any) -> ClipContext:
    values: dict[str, Any] = {
        "creator": "speedy",
        "platform": "twitch",
        "stream_title": "GTA RP | cops and robbers",
        "lines": LINES,
        "window_s": 60.0,
        "proposed_start_s": 5.0,
        "proposed_end_s": 35.0,
        "spike_floor_s": 9.0,
        "min_len_s": 20.0,
        "max_len_s": 60.0,
        "chat": (ChatLine(10.0, "LMAO"),),
        "clip_id": "clip-1",
    }
    values.update(overrides)
    return ClipContext(**values)


def verdict_reply(**overrides: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "is_moment": True,
        "confidence": 0.9,
        "moment_type": "funny",
        "one_line": "Streamer jumps a bike onto a police car",
        "noise_reason": None,
    }
    reply.update(overrides)
    return reply


def writing_reply(**overrides: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "start_s": 2.0,
        "end_s": 32.0,
        "hook": "He did NOT stick this landing 😭",
        "title": "speedy tries the worst bike jump in Los Santos",
        "caption": "The cop never saw it coming",
        "hashtags": ["bikejump", "gtarp", "copchase"],
        "commentary": None,
        "topic": "bike-jump-lands-on-cop",
        "keywords": [{"line": 2, "words": ["no way"]}, {"line": 4, "words": ["five", "grand"]}],
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
        "score": 8,
        "score_parts": {"hook": 8, "clarity": 7, "payoff": 9, "rewatch": 8},
        "reject": False,
        "reject_reason": None,
    }
    reply.update(overrides)
    return reply

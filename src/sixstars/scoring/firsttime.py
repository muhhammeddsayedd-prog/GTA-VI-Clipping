"""The "first time" boost (`scoring_config.boost.first_time`, docs/taste-profile.md section 3).

A clip earns the boost when its transcript or chat says "first time" / "first ever" / "finally" (the
configured phrases), or when its transcript or topic shows an event type (car theft, wanted level, death,
police chase, first gun) that this creator has no approved clip of yet.

`find_phrases` and `find_events` are pure; `first_events` reads the creator's earlier approved clips from
`scoring_log` (stage `gate`, `sub_scores.event_types`).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sixstars import db
from sixstars.logging import get_logger
from sixstars.scoring import config

_log = get_logger("scoring")


def _normal(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", text.lower())) + " "


def _contains(normal_text: str, phrase: str) -> bool:
    words = _normal(phrase)
    return words.strip() != "" and words in normal_text


def find_phrases(texts: Iterable[str], phrases: Sequence[str]) -> list[str]:
    """The phrases that appear as whole words in any of `texts`. Pure."""
    joined = _normal(" ".join(texts))
    return [phrase for phrase in phrases if _contains(joined, phrase)]


def find_events(texts: Iterable[str], event_types: Mapping[str, Sequence[str]]) -> list[str]:
    """The event types with at least one of their words in `texts`, in config order. Pure."""
    joined = _normal(" ".join(texts))
    return [name for name, words in event_types.items() if any(_contains(joined, word) for word in words)]


@dataclass(frozen=True, slots=True)
class Boost:
    """What the boost found and how many points it gives."""

    phrases: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()
    first_events: tuple[str, ...] = ()
    score_points: float = 0.0
    rank_points: float = 0.0
    details: Mapping[str, object] = field(default_factory=dict)

    @property
    def applies(self) -> bool:
        return bool(self.phrases or self.first_events)

    def as_dict(self) -> dict[str, object]:
        return {
            "phrases": list(self.phrases),
            "event_types": list(self.event_types),
            "first_events": list(self.first_events),
            "score_points": self.score_points,
            "rank_points": self.rank_points,
        }


def event_types_setting(flag: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    raw = flag.get("event_types")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name): tuple(str(word) for word in words)
        for name, words in raw.items()
        if isinstance(words, list)
    }


def decide(
    flag: Mapping[str, object],
    *,
    transcript_text: str,
    chat_texts: Sequence[str],
    topic: str,
    seen_events: Iterable[str],
) -> Boost:
    """The boost of one clip. `seen_events` are the event types the creator already has approved clips of.
    Pure."""
    phrases = tuple(find_phrases([transcript_text, *chat_texts], config.strings(flag, "phrases")))
    events = tuple(find_events([transcript_text, topic.replace("-", " ")], event_types_setting(flag)))
    seen = set(seen_events)
    first = tuple(name for name in events if name not in seen)
    applies = bool(phrases or first)
    return Boost(
        phrases=phrases,
        event_types=events,
        first_events=first,
        score_points=config.number(flag, "score_points", 1.0) if applies else 0.0,
        rank_points=config.number(flag, "rank_points", 1.0) if applies else 0.0,
    )


def seen_events(creator_id: str, clip_id: str) -> set[str]:
    """Event types of the creator's approved or posted clips, as logged by the gate."""
    try:
        rows = db.fetch_all(
            """
            select distinct jsonb_array_elements_text(l.sub_scores -> 'first_time' -> 'event_types') as e
            from scoring_log l join clips c on c.id = l.clip_id
            where l.stage = 'gate' and l.creator_id = %s and l.clip_id <> %s
              and c.status in ('approved', 'posted', 'posting')
            """,
            (creator_id, clip_id),
        )
    except Exception as exc:  # noqa: BLE001 - no history: every event counts as seen (no boost)
        _log.warning("first_time_history_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return {"*"}
    return {str(row["e"]) for row in rows}


def for_clip(
    flag: Mapping[str, object],
    *,
    creator_id: str,
    clip_id: str,
    transcript_text: str,
    chat_texts: Sequence[str],
    topic: str,
) -> Boost:
    """The boost of a stored clip (reads the creator's history)."""
    seen = seen_events(creator_id, clip_id)
    if "*" in seen:
        events = event_types_setting(flag)
        seen = set(events)
    return decide(flag, transcript_text=transcript_text, chat_texts=chat_texts, topic=topic, seen_events=seen)

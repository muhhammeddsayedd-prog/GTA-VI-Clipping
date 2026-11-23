"""The facts the title model gets about one clip, and the name book that maps streamer names to the way fans
write them. Pure functions; `titles.store` reads the rows they are built from.

The model may use only these facts (docs/30-titles-v2.md Part 1), so everything a good title needs has to be
here: who is in the clip, what happens (the visual judge's `seen` sentence), the strongest spoken line, and
any number (hours into the stream, hours played, percent, money).
"""

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

TIERS = ("S", "A", "B", "C")
CHAT_WORDS_MAX = 6
# A spoken line this close to the chat peak is what chat reacted to.
PEAK_BEFORE_S = 6.0
PEAK_AFTER_S = 3.0
MIN_HOURS_INTO_STREAM = 1.0
_CHAT_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "to",
        "of",
        "in",
        "on",
        "at",
        "it",
        "is",
        "was",
        "this",
        "that",
        "he",
        "she",
        "they",
        "you",
        "i",
        "me",
        "my",
        "we",
        "be",
        "so",
        "for",
        "with",
        "what",
        "why",
        "how",
        "just",
        "like",
        "not",
        "no",
        "yes",
        "yeah",
        "lol",
        "lmao",
        "bro",
        "its",
        "it's",
        "im",
        "i'm",
        "dont",
        "don't",
        "do",
        "did",
        "are",
        "have",
        "has",
        "had",
        "get",
        "got",
        "can",
        "his",
        "her",
        "him",
        "them",
        "their",
        "there",
        "here",
        "then",
        "than",
        "too",
        "very",
        "all",
        "one",
        "out",
        "up",
        "go",
        "now",
        "oh",
        "omg",
    }
)
_WORD = re.compile(r"[a-z][a-z0-9']{2,}")
_HOURS = re.compile(r"\b(\d{1,3}(?:\.\d)?)\s*(?:hours?|hrs?)\b", re.IGNORECASE)
_PERCENT = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)", re.IGNORECASE)
_MONEY = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m|million|thousand|grand)?\b"
    r"|\b(\d[\d,]*(?:\.\d+)?)\s*(k|m|million|thousand|grand)?\s*(?:dollars|bucks)\b",
    re.IGNORECASE,
)
_SCALE = {"k": 1e3, "thousand": 1e3, "grand": 1e3, "m": 1e6, "million": 1e6}


@dataclass(frozen=True, slots=True)
class Person:
    """One streamer in the name book."""

    name: str
    aliases: tuple[str, ...]
    tier: str = "C"


@dataclass(frozen=True, slots=True)
class NameBook:
    """Streamer names as fans write them, with the lowercase aliases that mean each one."""

    people: tuple[Person, ...] = ()

    def aliases_of(self, name: str) -> tuple[str, ...]:
        for person in self.people:
            if person.name == name:
                return person.aliases or (name.lower(),)
        return (name.lower(),)

    def tier_of(self, name: str) -> str:
        return next((person.tier for person in self.people if person.name == name), "C")

    def names_in(self, text: str) -> list[str]:
        """The people named in `text` (whole words, any case), in book order."""
        plain = " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())
        found: list[str] = []
        for person in self.people:
            for alias in person.aliases or (person.name.lower(),):
                if alias and re.search(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", plain):
                    found.append(person.name)
                    break
        return found


def clean_name(display_name: str | None, handle: str) -> str:
    """The name to use when no `title_name` is set: the platform's display name, never the lowercase login.
    Underscores become spaces; a name in all lowercase gets a capital; a non-Latin name falls back to the
    handle with a capital."""
    for raw in (display_name or "", handle):
        name = " ".join(raw.replace("_", " ").split())
        if not name or not name.isascii() or not any(ch.isalpha() for ch in name):
            continue
        if name.islower():
            name = name[0].upper() + name[1:]
        return name
    return handle


def aliases_for(name: str, handle: str, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Lowercase aliases: the given ones, then the name and the handle (at least 3 characters each)."""
    found: list[str] = []
    for alias in (*extra, name, handle.strip("_"), handle):
        alias = " ".join(alias.lower().replace("_", " ").split())
        if len(alias) >= 3 and alias not in found:
            found.append(alias)
    return tuple(found)


def tier_for(name: str, followers: int | None, s_names: Iterable[str], thresholds: Mapping[str, Any]) -> str:
    """S for the names the research found pull views by themselves; otherwise A or B by followers; C."""
    if name.lower() in {item.lower() for item in s_names}:
        return "S"
    for tier in ("A", "B"):
        limit = thresholds.get(tier)
        if followers is not None and isinstance(limit, int | float) and followers >= limit:
            return tier
    return "C"


def book_from_rows(
    rows: Iterable[Mapping[str, Any]],
    extra_names: Mapping[str, Sequence[str]],
    s_names: Iterable[str],
    thresholds: Mapping[str, Any],
) -> NameBook:
    """The name book from `creators` rows (`handle`, `display_name`, `title_name`, `title_aliases`,
    `follower_count`) plus names that are not creators of ours (`extra_names`: name → aliases)."""
    people: dict[str, Person] = {}
    s_list = list(s_names)
    for row in rows:
        handle = str(row.get("handle") or "")
        name = str(row.get("title_name") or "") or clean_name(row.get("display_name"), handle)
        aliases = aliases_for(name, handle, row.get("title_aliases") or ())
        follower_count = row.get("follower_count")
        followers = int(follower_count) if isinstance(follower_count, int | float) else None
        people.setdefault(name, Person(name, aliases, tier_for(name, followers, s_list, thresholds)))
    for name, known in extra_names.items():
        if name not in people:
            people[name] = Person(
                name, aliases_for(name, "", known), tier_for(name, None, s_list, thresholds)
            )
    return NameBook(tuple(people.values()))


@dataclass(frozen=True, slots=True)
class TitleFacts:
    """Everything the title model may use. `numbers` holds only the keys that have a value."""

    streamer: str
    streamer_tier: str
    event: str
    gta_label: str = "GTA 6"
    other_streamers: tuple[str, ...] = ()
    other_tiers: tuple[str, ...] = ()
    peak_line: str | None = None
    numbers: Mapping[str, float] = field(default_factory=dict)
    chat_words: tuple[str, ...] = ()
    campaign: Mapping[str, Any] | None = None
    spoiler: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "streamer": self.streamer,
            "streamer_tier": self.streamer_tier,
            "other_streamers": [
                {"name": name, "tier": tier}
                for name, tier in zip(self.other_streamers, self.other_tiers, strict=False)
            ],
            "event": self.event,
            "peak_line": self.peak_line,
            "numbers": dict(self.numbers),
            "chat_words": list(self.chat_words),
            "campaign": dict(self.campaign) if self.campaign else None,
            "gta_label": self.gta_label,
            "story_spoiler": self.spoiler,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=1)


@dataclass(frozen=True, slots=True)
class TimedLine:
    """A spoken line in clip time."""

    start_s: float
    end_s: float
    text: str


def peak_line(lines: Sequence[TimedLine], peak_s: float | None) -> str | None:
    """The line chat reacted to: of the lines just before and at the chat peak, the one with the most words.
    Without a peak, the line with the most exclamation marks, then the most words. The audio level is not
    stored per line, so "loudest" is read from the words."""
    spoken = [line for line in lines if line.text.strip()]
    if not spoken:
        return None
    if peak_s is not None:
        near = [
            line
            for line in spoken
            if line.end_s >= peak_s - PEAK_BEFORE_S and line.start_s <= peak_s + PEAK_AFTER_S
        ]
        if near:
            return max(near, key=lambda line: len(line.text.split())).text.strip()
    return max(spoken, key=lambda line: (line.text.count("!"), len(line.text.split()))).text.strip()


def _amount(number: str, unit: str | None) -> float | None:
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    return value * _SCALE.get((unit or "").lower(), 1.0)


def spoken_numbers(text: str) -> dict[str, float]:
    """Hours played, percent complete and money said in the transcript (the first of each)."""
    found: dict[str, float] = {}
    if match := _HOURS.search(text):
        found["hours_played"] = float(match.group(1))
    if match := _PERCENT.search(text):
        value = float(match.group(1))
        if 0 < value <= 100:
            found["percent_complete"] = value
    if match := _MONEY.search(text):
        amount = _amount(match.group(1) or match.group(3), match.group(2) or match.group(4))
        if amount:
            found["money"] = amount
    return found


def hours_into_stream(offset_s: float | None) -> float | None:
    """Whole hours since the stream started, when it is at least an hour (a smaller number is no stake).
    Whole hours because the model copied "8.9 Hours" into titles despite the rule to round."""
    if offset_s is None or offset_s < MIN_HOURS_INTO_STREAM * 3600:
        return None
    return float(round(offset_s / 3600.0))


def chat_words(messages: Iterable[str], limit: int = CHAT_WORDS_MAX) -> tuple[str, ...]:
    """The words most chat messages used (each message counts a word once), common words left out."""
    counts: Counter[str] = Counter()
    for message in messages:
        counts.update(set(_WORD.findall(message.lower())) - _CHAT_STOP)
    return tuple(word for word, count in counts.most_common(limit) if count >= 2)


def build(
    *,
    streamer: str,
    book: NameBook,
    event: str,
    lines: Sequence[TimedLine],
    peak_s: float | None,
    offset_s: float | None,
    chat: Iterable[str],
    gta_label: str,
    spoiler: bool,
    campaign: Mapping[str, Any] | None = None,
) -> TitleFacts:
    """The facts for one clip. Other streamers are the people of the name book named in what is said or
    seen, besides the clip's own streamer."""
    spoken = " ".join(line.text for line in lines)
    others = [name for name in book.names_in(f"{spoken} {event}") if name != streamer]
    numbers: dict[str, float] = {}
    if (hours := hours_into_stream(offset_s)) is not None:
        numbers["hours_into_stream"] = hours
    numbers.update(spoken_numbers(spoken))
    return TitleFacts(
        streamer=streamer,
        streamer_tier=book.tier_of(streamer),
        event=" ".join(event.split()),
        gta_label=gta_label,
        other_streamers=tuple(others),
        other_tiers=tuple(book.tier_of(name) for name in others),
        peak_line=peak_line(lines, peak_s),
        numbers=numbers,
        chat_words=chat_words(chat),
        campaign=campaign,
        spoiler=spoiler,
    )


def hashtags(facts: TitleFacts, tail: Sequence[str]) -> list[str]:
    """The clip's own tags for TikTok and Instagram: the streamer, then `tail` (e.g. streamerclips). The
    channel's base tags (#GTA6 ...) are added by posting."""
    own = re.sub(r"[^a-z0-9]", "", facts.streamer.lower())
    tags = [own] if own else []
    tags.extend(tag.lower().lstrip("#") for tag in tail if tag)
    return list(dict.fromkeys(tags))

"""Transcript words and their grouping into subtitle lines (docs/09 section 4): 2 to 4 words, about 22
characters, broken at pauses and punctuation. Lines are numbered before the clip writer runs so its
per-line keywords still match after the clip is trimmed.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sixstars.factory.window import ClipWindow

_BREAK_PUNCTUATION = (".", "!", "?", ",", ";", ":", "…")
_CLOSERS = "\"')]}”’"


@dataclass(frozen=True, slots=True)
class Word:
    """One transcript word; times in seconds."""

    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class Line:
    """One subtitle line. `index` is the line number given to the clip writer."""

    index: int
    words: tuple[Word, ...]

    @property
    def start_s(self) -> float:
        return self.words[0].start_s

    @property
    def end_s(self) -> float:
        return self.words[-1].end_s

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)


@dataclass(frozen=True, slots=True)
class LineRules:
    """The grouping rules of docs/09 section 4 (layout spec, not settings)."""

    min_words: int = 2
    max_words: int = 4
    max_chars: int = 22
    pause_s: float = 0.35


def words_from_transcript(transcript: Mapping[str, object]) -> list[Word]:
    """Parses the stored transcript format `{"language": "en", "words": [{"w": ..., "s": ..., "e": ...}]}`.

    Empty words are dropped, words are sorted by start time and an end before its start is clamped.
    """
    raw = transcript.get("words")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ValueError("transcript has no words list")
    words: list[Word] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("transcript word is not an object")
        text, start, end = item.get("w"), item.get("s"), item.get("e")
        if not isinstance(text, str) or not _is_number(start) or not _is_number(end):
            raise ValueError(f"malformed transcript word: {item!r}")
        assert isinstance(start, int | float) and isinstance(end, int | float)
        clean = " ".join(text.split())
        if not clean:
            continue
        start_s = max(0.0, float(start))
        words.append(Word(clean, start_s, max(start_s, float(end))))
    words.sort(key=lambda word: word.start_s)
    return words


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _ends_phrase(text: str) -> bool:
    return text.rstrip(_CLOSERS).endswith(_BREAK_PUNCTUATION)


def group_lines(words: Sequence[Word], rules: LineRules | None = None) -> list[Line]:
    """Groups words into numbered subtitle lines.

    A line ends before a word that would exceed `max_words` or `max_chars`, before a word that follows a
    pause longer than `pause_s`, and after a word ending in punctuation once the line has `min_words`. A
    single word left over by a length break is rebalanced with the previous line when that keeps both lines
    within the rules.
    """
    rules = rules or LineRules()
    groups: list[list[Word]] = []
    forced: list[bool] = []  # True when the group started after a pause or punctuation
    current: list[Word] = []
    current_forced = True
    close_after_punctuation = False
    for word in words:
        if current:
            chars = sum(len(w.text) for w in current) + len(current) + len(word.text)
            pause = word.start_s - current[-1].end_s > rules.pause_s
            too_long = len(current) >= rules.max_words or chars > rules.max_chars
            if pause or close_after_punctuation or too_long:
                groups.append(current)
                forced.append(current_forced)
                current = []
                current_forced = pause or close_after_punctuation
        current.append(word)
        close_after_punctuation = _ends_phrase(word.text) and len(current) >= rules.min_words
    if current:
        groups.append(current)
        forced.append(current_forced)
    _rebalance(groups, forced, rules)
    return [Line(index, tuple(group)) for index, group in enumerate(groups)]


def _rebalance(groups: list[list[Word]], forced: list[bool], rules: LineRules) -> None:
    for i in range(1, len(groups)):
        previous, group = groups[i - 1], groups[i]
        if len(group) != 1 or forced[i] or len(previous) <= max(rules.min_words, 1):
            continue
        moved = previous[-1]
        if _ends_phrase(previous[-2].text) or len(moved.text) + 1 + len(group[0].text) > rules.max_chars:
            continue
        groups[i] = [previous.pop(), *group]


def clip_lines(lines: Sequence[Line], window: ClipWindow) -> list[Line]:
    """Keeps the words that start inside `window` and shifts them so the window start is time zero.

    Line numbers are preserved; lines left without words are dropped; word ends are clamped to the window.
    """
    duration = window.duration_s
    out: list[Line] = []
    for line in lines:
        kept = tuple(
            Word(
                word.text,
                word.start_s - window.start_s,
                min(max(word.end_s, word.start_s) - window.start_s, duration),
            )
            for word in line.words
            if window.start_s <= word.start_s < window.end_s
        )
        if kept:
            out.append(Line(line.index, kept))
    return out

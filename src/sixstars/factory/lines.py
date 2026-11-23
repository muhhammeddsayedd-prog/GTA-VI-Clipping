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


@dataclass(frozen=True, slots=True)
class Chunk:
    """A caption chunk: one or two words shown together (docs/27). `words` are positions in the line's
    word list; `repeat` is how many times the same chunk was said in a row (shown as `X2`, `X3`).
    `phrase` marks a cover-ready phrase (several chunks of a line shown whole and still, `coverready`)."""

    line_index: int
    words: tuple[int, ...]
    start_s: float
    end_s: float
    repeat: int = 1
    phrase: bool = False


# A chunk ends before a word that follows a pause this long, even when it has room for the word.
CHUNK_PAUSE_S = 0.25


def chunk_line(line: Line, max_words: int, max_chars: int, pause_s: float = CHUNK_PAUSE_S) -> list[Chunk]:
    """Splits a subtitle line into caption chunks of at most `max_words` words and `max_chars` characters
    (a single longer word is a chunk on its own). A chunk also ends after punctuation, before a pause and
    before a word that repeats the one before it (so `collapse_repeats` can show it as `X2`)."""
    if max_words < 1:
        raise ValueError("max_words must be at least 1")
    chunks: list[list[int]] = []
    current: list[int] = []
    for i, word in enumerate(line.words):
        if current:
            last = line.words[current[-1]]
            chars = sum(len(line.words[j].text) for j in current) + len(current) + len(word.text)
            if (
                len(current) >= max_words
                or chars > max_chars
                or word.start_s - last.end_s > pause_s
                or _ends_phrase(last.text)
                or _word_key(word.text) == _word_key(last.text)
                or (current[-1] > 0 and _word_key(last.text) == _word_key(line.words[current[-1] - 1].text))
            ):
                chunks.append(current)
                current = []
        current.append(i)
    if current:
        chunks.append(current)
    return [
        Chunk(line.index, tuple(group), line.words[group[0]].start_s, line.words[group[-1]].end_s)
        for group in chunks
    ]


def _word_key(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum() or ch == "'")


def _chunk_key(line: Line, chunk: Chunk) -> str:
    return " ".join(_word_key(line.words[i].text) for i in chunk.words)


def collapse_repeats(lines: Sequence[Line], chunks: Sequence[Chunk]) -> list[Chunk]:
    """Merges a chunk said again straight after itself into one chunk with a repeat count, so "come here
    come here" shows as `COME HERE X2`. `chunks` must be in time order; each refers to a line in `lines`."""
    by_index = {line.index: line for line in lines}
    out: list[Chunk] = []
    previous_key = ""
    for chunk in chunks:
        key = _chunk_key(by_index[chunk.line_index], chunk)
        if out and key and key == previous_key:
            last = out[-1]
            out[-1] = Chunk(last.line_index, last.words, last.start_s, chunk.end_s, last.repeat + 1)
            continue
        out.append(chunk)
        previous_key = key
    return out


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

"""Snap the cut to a pause: pure rules that move a clip's in and out points to where a phrase begins and
ends, so a clip does not open or close in the middle of a sentence.

Whisper often returns no punctuation, so a sentence boundary cannot come from the text alone. Two things
mark one here: a sentence-ending mark on the previous word (when the transcript has punctuation) and a
gap between two words of at least `SnapRules.pause_s`. Whisper also stretches a word across the silence
next to it, which hides those gaps; `tighten_words` pulls such a word back onto the speech using the
silent stretches found by voice activity detection, so the gaps show up again. And where people talk
without pausing there is no gap to find, so a transcript that came back without punctuation gets its
sentence ends back from the fast model (prompt P10) and `mark_sentence_ends` writes them into the words.

Every time is in seconds on one clock (the factory uses window time). Every threshold comes from the
setting `factory.cut_snap`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from sixstars.factory.lines import Word
from sixstars.factory.window import ClipWindow

BreakKind = Literal["sentence", "pause"]

_SENTENCE_END = (".", "!", "?", "…")
_CLOSERS = "\"')]}”’"
# Float slack when a gap or a time is compared with a limit (transcript times are rounded to 1 ms).
_EPSILON_S = 1e-6


@dataclass(frozen=True, slots=True)
class SnapRules:
    """The setting `factory.cut_snap`. A `pause_s` of 0 switches the rule off; a range of 0 switches
    off the move in that direction."""

    pause_s: float = 0.0
    start_back_s: float = 0.0
    start_forward_s: float = 0.0
    end_forward_s: float = 0.0
    end_back_s: float = 0.0
    vad: bool = False
    punctuate_below: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.pause_s > 0

    @classmethod
    def from_setting(cls, raw: object) -> "SnapRules":
        """Build the rules from the JSON object of the setting. A missing or malformed value gives the
        switched-off rules, so a database without the setting behaves as before."""
        if not isinstance(raw, Mapping):
            return cls()

        def number(key: str) -> float:
            value = raw.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
                return float(value)
            return 0.0

        return cls(
            pause_s=number("pause_s"),
            start_back_s=number("start_back_s"),
            start_forward_s=number("start_forward_s"),
            end_forward_s=number("end_forward_s"),
            end_back_s=number("end_back_s"),
            vad=raw.get("vad") is True,
            punctuate_below=number("punctuate_below"),
        )


@dataclass(frozen=True, slots=True)
class Silence:
    """A stretch with no speech."""

    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class Break:
    """A phrase boundary just before `words[index]` (`index == len(words)` is after the last word).

    `end_s` is where the phrase before it ends and `start_s` where the phrase after it begins.
    """

    index: int
    kind: BreakKind
    end_s: float
    start_s: float


def tighten_words(words: Sequence[Word], silences: Sequence[Silence], min_word_s: float = 0.0) -> list[Word]:
    """Pulls words that were stretched across a silence back onto the speech.

    A silence over the start of a word moves the start to the end of the silence; one over the end
    moves the end to the start of the silence; one inside a word leaves the word on the longer of its
    two spoken sides (the later side when they are equal). A word that lies wholly inside a silence, or
    that would become shorter than `min_word_s`, is left as it is.
    """
    ordered = sorted(silences, key=lambda item: item.start_s)
    tight: list[Word] = []
    for word in words:
        start, end = word.start_s, word.end_s
        for silence in ordered:
            if silence.end_s <= start or silence.start_s >= end:
                continue
            covers_start = silence.start_s <= start
            covers_end = silence.end_s >= end
            if covers_start and covers_end:
                continue
            new_start, new_end = start, end
            if covers_start:
                new_start = silence.end_s
            elif covers_end:
                new_end = silence.start_s
            elif end - silence.end_s >= silence.start_s - start:
                new_start = silence.end_s
            else:
                new_end = silence.start_s
            if new_end - new_start >= max(min_word_s, _EPSILON_S):
                start, end = new_start, new_end
        tight.append(Word(word.text, start, end))
    return tight


def _ends_sentence(text: str) -> bool:
    return text.rstrip(_CLOSERS).endswith(_SENTENCE_END)


def sentence_end_share(words: Sequence[Word]) -> float:
    """The share of words that end a sentence (0 for no words). Punctuated speech is near 0.1; a
    transcript Whisper left without punctuation is at 0."""
    if not words:
        return 0.0
    return sum(1 for word in words if _ends_sentence(word.text)) / len(words)


def mark_sentence_ends(words: Sequence[Word], ends: Sequence[int]) -> list[Word]:
    """Writes a full stop after the words at the indexes `ends`, as Whisper does when it punctuates.

    A word that already ends in punctuation is left alone and an index outside the transcript is
    ignored. Times are not changed.
    """
    wanted = set(ends)
    marked: list[Word] = []
    for index, word in enumerate(words):
        bare = word.text.rstrip(_CLOSERS)
        if index in wanted and bare and bare[-1].isalnum():
            marked.append(Word(word.text + ".", word.start_s, word.end_s))
        else:
            marked.append(word)
    return marked


def find_breaks(words: Sequence[Word], pause_s: float, limit_s: float | None = None) -> list[Break]:
    """The phrase boundaries of a transcript, in order.

    There is one before a word that follows a sentence-ending mark (`sentence`) or a gap of at least
    `pause_s` (`pause`); before the first word when it is at least `pause_s` from time zero; and after
    the last word when it ends a sentence or `limit_s` (the end of the file) is at least `pause_s` away.
    """
    if not words or pause_s <= 0:
        return []
    found: list[Break] = []
    first = words[0]
    if first.start_s >= pause_s - _EPSILON_S:
        found.append(Break(0, "pause", 0.0, first.start_s))
    for index in range(1, len(words)):
        previous, word = words[index - 1], words[index]
        if _ends_sentence(previous.text):
            found.append(Break(index, "sentence", previous.end_s, word.start_s))
        elif word.start_s - previous.end_s >= pause_s - _EPSILON_S:
            found.append(Break(index, "pause", previous.end_s, word.start_s))
    last = words[-1]
    if _ends_sentence(last.text):
        found.append(Break(len(words), "sentence", last.end_s, last.end_s))
    elif limit_s is not None and limit_s - last.end_s >= pause_s - _EPSILON_S:
        found.append(Break(len(words), "pause", last.end_s, limit_s))
    return found


def _pick(candidates: Sequence[tuple[float, Break]]) -> Break | None:
    """The nearest sentence boundary if there is one, else the nearest pause. `candidates` pairs each
    boundary with its distance from the current cut."""
    for kind in ("sentence", "pause"):
        of_kind = [item for item in candidates if item[1].kind == kind]
        if of_kind:
            return min(of_kind, key=lambda item: item[0])[1]
    return None


def snap_start(
    window: ClipWindow,
    words: Sequence[Word],
    breaks: Sequence[Break],
    rules: SnapRules,
    *,
    floor_s: float,
    min_len_s: float,
    max_len_s: float,
) -> float:
    """The clip start moved to the beginning of a phrase.

    Unchanged when the first word of the clip already begins a phrase. Otherwise it moves back to the
    nearest phrase start within `start_back_s` that keeps the clip within `max_len_s`; failing that,
    forward to the nearest one within `start_forward_s` that is not later than `floor_s` and keeps the
    clip at least `min_len_s` long. A sentence start is preferred to a bare pause in either direction.
    """
    first = next((i for i, word in enumerate(words) if word.start_s >= window.start_s - _EPSILON_S), None)
    if first is None or words[first].start_s >= window.end_s:
        return window.start_s
    usable = [item for item in breaks if item.index < len(words)]
    if any(item.index == first for item in usable):
        return window.start_s
    before = [
        (window.start_s - item.start_s, item)
        for item in usable
        if item.index < first
        and window.start_s - item.start_s <= rules.start_back_s + _EPSILON_S
        and window.end_s - item.start_s <= max_len_s + _EPSILON_S
    ]
    chosen = _pick(before)
    if chosen is None:
        after = [
            (item.start_s - window.start_s, item)
            for item in usable
            if item.index > first
            and item.start_s - window.start_s <= rules.start_forward_s + _EPSILON_S
            and item.start_s <= floor_s + _EPSILON_S
            and window.end_s - item.start_s >= min_len_s - _EPSILON_S
        ]
        chosen = _pick(after)
    return window.start_s if chosen is None else chosen.start_s


def snap_end(
    window: ClipWindow,
    words: Sequence[Word],
    breaks: Sequence[Break],
    rules: SnapRules,
    *,
    limit_s: float,
    min_len_s: float,
    max_len_s: float,
) -> float:
    """The clip end moved to the end of a phrase.

    Unchanged when the last word of the clip already ends a phrase. Otherwise it moves forward to the
    nearest phrase end within `end_forward_s` that is inside the file (`limit_s`) and keeps the clip
    within `max_len_s`; failing that, back to the nearest one within `end_back_s` that keeps the clip
    at least `min_len_s` long. A sentence end is preferred to a bare pause in either direction.
    """
    inside = [i for i, word in enumerate(words) if window.start_s - _EPSILON_S <= word.start_s < window.end_s]
    if not inside:
        return window.end_s
    last = inside[-1]
    usable = [item for item in breaks if item.index > 0]
    if any(item.index == last + 1 for item in usable):
        return window.end_s
    reference = max(window.end_s, words[last].end_s)
    after = [
        (item.end_s - reference, item)
        for item in usable
        if item.index > last + 1
        and item.end_s - window.end_s <= rules.end_forward_s + _EPSILON_S
        and item.end_s <= limit_s + _EPSILON_S
        and item.end_s - window.start_s <= max_len_s + _EPSILON_S
    ]
    chosen = _pick(after)
    if chosen is None:
        before = [
            (window.end_s - item.end_s, item)
            for item in usable
            if item.index <= last
            and item.index > inside[0]
            and window.end_s - item.end_s <= rules.end_back_s + _EPSILON_S
            and item.end_s - window.start_s >= min_len_s - _EPSILON_S
        ]
        chosen = _pick(before)
    return window.end_s if chosen is None else chosen.end_s


def snap_to_pauses(
    window: ClipWindow,
    words: Sequence[Word],
    rules: SnapRules,
    *,
    floor_s: float,
    min_len_s: float,
    max_len_s: float,
    limit_s: float,
) -> ClipWindow:
    """The window with its start and end moved to phrase boundaries (see `snap_start`, `snap_end`).

    The start is never later than `floor_s`, the end never after `limit_s`, and the length stays within
    `[min_len_s, max_len_s]` whenever the given window was. With no boundary in range, or with the rule
    switched off, the window comes back unchanged.
    """
    if not rules.enabled or not words:
        return window
    breaks = find_breaks(words, rules.pause_s, limit_s)
    if not breaks:
        return window
    start = snap_start(
        window, words, breaks, rules, floor_s=floor_s, min_len_s=min_len_s, max_len_s=max_len_s
    )
    moved = ClipWindow(start, window.end_s) if start < window.end_s else window
    end = snap_end(moved, words, breaks, rules, limit_s=limit_s, min_len_s=min_len_s, max_len_s=max_len_s)
    return ClipWindow(moved.start_s, end) if end > moved.start_s else moved

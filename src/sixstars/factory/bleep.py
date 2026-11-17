"""Finds profanity in the transcript using the profanity list and produces audio mute and bleep ranges plus
masked subtitle text. Implemented in M06.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sixstars.factory.lines import Word

# docs/09 section 6: mute `[start - 0.05, end + 0.05]` and mix a 1 kHz sine at -20 dB over the same span.
BLEEP_PAD_S = 0.05
BLEEP_FREQUENCY_HZ = 1000
BLEEP_GAIN_DB = -20.0

_SUFFIXES = ("", "s", "es", "ed", "ing", "in", "er", "ers")
_DOUBLING_SUFFIXES = ("ed", "ing", "in", "er", "ers")
_EDGE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)
_CORE = re.compile(r"^(\W*)(.*?)(\W*)$", re.UNICODE | re.DOTALL)


@dataclass(frozen=True, slots=True)
class Span:
    """A time span in seconds relative to the clip start."""

    start_s: float
    end_s: float


def normalize_word(text: str) -> str:
    """Lowercases a transcript word and strips punctuation from both ends (`"Sh*t!"` keeps its middle)."""
    core = _EDGE.sub("", text.strip().lower().replace("’", "'"))
    return core.removesuffix("'s")


def parse_profanity_list(lines: Iterable[str]) -> frozenset[str]:
    """Reads the `assets/profanity.txt` format: one lowercase word or phrase per line, `#` comments."""
    terms: set[str] = set()
    for line in lines:
        entry = line.split("#", 1)[0]
        term = " ".join(normalize_word(part) for part in entry.split())
        if term.strip():
            terms.add(term)
    return frozenset(terms)


def load_profanity_list(path: Path) -> frozenset[str]:
    """Loads a profanity list file (UTF-8)."""
    return parse_profanity_list(path.read_text(encoding="utf-8").splitlines())


@dataclass(frozen=True, slots=True)
class ProfanityMatcher:
    """Whole-word matcher with simple plural, -ed and -ing forms; phrases match consecutive words."""

    terms: frozenset[str]

    def is_profane(self, text: str) -> bool:
        """True when a single transcript word is on the list, directly or through a simple suffix."""
        word = normalize_word(text)
        if not word:
            return False
        for suffix in _SUFFIXES:
            if suffix and not word.endswith(suffix):
                continue
            stem = word[: len(word) - len(suffix)] if suffix else word
            if stem in self.terms:
                return True
            if (
                suffix in _DOUBLING_SUFFIXES
                and len(stem) >= 3
                and stem[-1] == stem[-2]
                and stem[:-1] in self.terms
            ):
                return True
        return False

    def find(self, words: Sequence[str]) -> list[int]:
        """Indices of the words to bleep and mask, including every word of a matched phrase."""
        normal = [normalize_word(word) for word in words]
        hits = {i for i, word in enumerate(words) if self.is_profane(word)}
        for phrase in (term.split(" ") for term in self.terms if " " in term):
            size = len(phrase)
            for i in range(len(normal) - size + 1):
                if normal[i : i + size] == phrase:
                    hits.update(range(i, i + size))
        return sorted(hits)


def mask_word(text: str) -> str:
    """Masks a word for subtitles: first letter kept, the rest as asterisks, outer punctuation kept."""
    match = _CORE.match(text)
    if match is None or not match.group(2):
        return text
    lead, core, tail = match.groups()
    return f"{lead}{core[0]}{'*' * (len(core) - 1)}{tail}"


def bleep_spans(
    words: Sequence[Word],
    profane_indices: Iterable[int],
    clip_duration_s: float,
    pad_s: float = BLEEP_PAD_S,
) -> list[Span]:
    """Padded spans for the given words, clamped to the clip and merged where they touch or overlap."""
    raw = sorted(
        (max(0.0, words[i].start_s - pad_s), min(clip_duration_s, words[i].end_s + pad_s))
        for i in profane_indices
    )
    merged: list[Span] = []
    for start, end in raw:
        if end <= start:
            continue
        if merged and start <= merged[-1].end_s:
            merged[-1] = Span(merged[-1].start_s, max(merged[-1].end_s, end))
        else:
            merged.append(Span(start, end))
    return merged

"""Pure text helpers of the gate: term search in the written fields and word overlap for duplicates."""

import re
from collections.abc import Iterable, Sequence

_WORD = re.compile(r"[a-z0-9]+")
# Words that say nothing about what a topic is.
TOPIC_STOPWORDS = frozenset({"a", "an", "and", "at", "by", "for", "in", "of", "on", "the", "to", "with"})
SHINGLE_WORDS = 3
# With fewer shingles than this a transcript is too short to compare.
MIN_SHINGLES = 3


def words(text: str | None) -> list[str]:
    """Lowercase words of `text`: letters and digits, everything else separates (apostrophes are dropped)."""
    if not text:
        return []
    return _WORD.findall(text.casefold().replace("'", "").replace("’", ""))


def contains_term(text: str | None, term: str) -> bool:
    """True when the words of `term` appear in `text` as whole words, in order and next to each other."""
    needle = words(term)
    hay = words(text)
    if not needle or len(needle) > len(hay):
        return False
    size = len(needle)
    return any(hay[i : i + size] == needle for i in range(len(hay) - size + 1))


def hashtag_has_term(hashtag: str, term: str) -> bool:
    """A hashtag has no spaces. It matches a term as whole words, as the term written without spaces, or,
    for a term of several words, when it contains the term written without spaces ("gtafinalmission")."""
    if contains_term(hashtag, term):
        return True
    parts = words(term)
    squashed = "".join(parts)
    tag = "".join(words(hashtag))
    if not squashed:
        return False
    return squashed in tag if len(parts) > 1 else tag == squashed


def find_terms(
    fields: Sequence[tuple[str, str | None]], hashtags: Iterable[str], terms: Iterable[str]
) -> list[str]:
    """Every hit as "<field>: <term>", fields first in the given order, then hashtags."""
    listed = [term for term in terms if words(term)]
    hits = [f"{name}: {term}" for name, text in fields for term in listed if contains_term(text, term)]
    tags = list(hashtags)
    hits += [f"hashtags: {term}" for term in listed if any(hashtag_has_term(tag, term) for tag in tags)]
    return hits


def stem(word: str) -> str:
    """A crude English stem, enough to match "robs" with "robbing" and "jumps" with "jumping"."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    if len(word) >= 4 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]
    return word


def topic_tokens(topic: str | None) -> frozenset[str]:
    """The stemmed content words of a topic slug."""
    return frozenset(stem(word) for word in words(topic) if word not in TOPIC_STOPWORDS)


def same_topic(a: str | None, b: str | None) -> bool:
    """Equal topics, ignoring case and separators. An empty topic equals nothing."""
    left = words(a)
    return bool(left) and left == words(b)


def overlap_coefficient(a: frozenset[str], b: frozenset[str]) -> float:
    """Shared tokens divided by the size of the smaller set; 0 unless at least two tokens are shared (or
    the smaller set has only one)."""
    if not a or not b:
        return 0.0
    shared = len(a & b)
    smaller = min(len(a), len(b))
    if shared < min(2, smaller):
        return 0.0
    return shared / smaller


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def shingles(text: str | None) -> frozenset[tuple[str, ...]]:
    """The set of three-word sequences of a transcript."""
    listed = words(text)
    return frozenset(tuple(listed[i : i + SHINGLE_WORDS]) for i in range(len(listed) - SHINGLE_WORDS + 1))


def transcript_overlap(a: str | None, b: str | None) -> float:
    """Share of the shorter transcript's three-word sequences that also occur in the other (0 to 1).
    Unrelated speech shares almost none; the same moment cut twice shares most."""
    left, right = shingles(a), shingles(b)
    if len(left) < MIN_SHINGLES or len(right) < MIN_SHINGLES:
        return 0.0
    return len(left & right) / min(len(left), len(right))

"""Pure logic that groups chat messages into fixed time buckets and counts messages, unique chatters,
reaction tokens and clip requests per bucket. Implemented in M04.

Token rules (`docs/05-chat-spike-engine.md` section 3): the text is uppercased and split on whitespace and
punctuation; a one-word token must equal a whole word; a multi-word token ("NO WAY") must appear as a run of
whole words; a token without letters or digits (emoji) is matched as characters. Two additions, both needed
on real chat: runs of a repeated letter are squeezed on both sides, so "LMAOOOO" counts as "LMAO" and "WWWW"
as "W"; and a token written with a trailing `*` in the setting ("HAHA*") matches any word starting with it.
"""

import re
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from sixstars.chat.models import ChatMessage

_WORD_RE: Final = re.compile(r"[^\W_]+")
_REPEAT_RE: Final = re.compile(r"(.)\1+")
_CUSTOM_EMOJI_RE: Final = re.compile(r":[A-Za-z0-9_][A-Za-z0-9_\-]*:")
PREFIX_MARK: Final = "*"


def squeeze(word: str) -> str:
    """Collapse every run of one repeated character to a single character ("LMAOOO" -> "LMAO")."""
    return _REPEAT_RE.sub(r"\1", word)


def tokenise(text: str) -> list[str]:
    """Uppercase words of a chat message, split on whitespace and punctuation, repeated letters squeezed."""
    return [squeeze(word) for word in _WORD_RE.findall(text.upper())]


def is_burst(tokens: Sequence[str], burst_min: int) -> bool:
    """True when one word makes up `burst_min` or more of the message's words ("KEKW KEKW KEKW")."""
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
        if counts[token] >= burst_min:
            return True
    return False


@dataclass(frozen=True, slots=True)
class TokenMatcher:
    """A compiled token list (`spikes.reaction_tokens` or `spikes.clip_tokens`)."""

    words: frozenset[str]
    prefixes: tuple[str, ...]
    phrases: tuple[str, ...]
    symbols: tuple[str, ...]
    burst_min: int = 0  # a message repeating one word this many times matches (0 = off)
    custom_emoji: bool = False  # a YouTube custom emoji (":name:") matches

    @classmethod
    def from_tokens(
        cls, tokens: Iterable[str], *, burst_min: int = 0, custom_emoji: bool = False
    ) -> "TokenMatcher":
        """Sort the tokens of a setting into whole words, word prefixes, phrases and symbols. `burst_min`
        and `custom_emoji` are the `reactions.extended` rules (scoring_config)."""
        words: set[str] = set()
        prefixes: set[str] = set()
        phrases: set[str] = set()
        symbols: set[str] = set()
        for token in tokens:
            stripped = token.strip()
            parts = tokenise(stripped)
            if not stripped:
                continue
            if not parts:
                symbols.add(stripped)
            elif len(parts) > 1:
                phrases.add(" " + " ".join(parts) + " ")
            elif stripped.endswith(PREFIX_MARK):
                prefixes.add(parts[0])
            else:
                words.add(parts[0])
        return cls(
            frozenset(words),
            tuple(sorted(prefixes)),
            tuple(sorted(phrases)),
            tuple(sorted(symbols)),
            burst_min=max(0, burst_min),
            custom_emoji=custom_emoji,
        )

    def matches(self, text: str, words: Sequence[str] | None = None) -> bool:
        """True when the message contains at least one token. `words` is `tokenise(text)` if already made."""
        if any(symbol in text for symbol in self.symbols):
            return True
        if self.custom_emoji and _CUSTOM_EMOJI_RE.search(text):
            return True
        tokens = tokenise(text) if words is None else words
        if not tokens:
            return False
        if self.burst_min >= 2 and len(tokens) >= self.burst_min and is_burst(tokens, self.burst_min):
            return True
        if any(token in self.words for token in tokens):
            return True
        if self.prefixes and any(token.startswith(self.prefixes) for token in tokens):
            return True
        if self.phrases:
            joined = " " + " ".join(tokens) + " "
            return any(phrase in joined for phrase in self.phrases)
        return False


@dataclass(frozen=True, slots=True)
class Counted:
    """One message that passed the filters, with its token flags worked out once."""

    t: float
    user_id: str
    react: bool
    clip: bool
    msg: ChatMessage


def count_message(msg: ChatMessage, reactions: TokenMatcher, clips: TokenMatcher) -> Counted:
    """Work out the reaction and clip-request flags of one message."""
    words = tokenise(msg.text)
    return Counted(
        msg.t, msg.user_id, reactions.matches(msg.text, words), clips.matches(msg.text, words), msg
    )


class LoudnessTrack:
    """Loudness readings in dB over time, supplied by whoever has the audio (the live buffer in M14, a
    downloaded window in M08). Without readings every audio jump is 0 and the score works without it."""

    def __init__(self, readings: Iterable[tuple[float, float]] = ()) -> None:
        ordered = sorted(readings)
        self._times: list[float] = [t for t, _ in ordered]
        self._levels: list[float] = [level for _, level in ordered]

    def __len__(self) -> int:
        return len(self._times)

    def add(self, t: float, level_db: float) -> None:
        """Add one reading (readings may arrive slightly out of order)."""
        index = bisect_left(self._times, t)
        self._times.insert(index, t)
        self._levels.insert(index, level_db)

    def trim(self, before: float) -> None:
        """Forget readings older than `before`."""
        index = bisect_left(self._times, before)
        del self._times[:index]
        del self._levels[:index]

    def _mean(self, start: float, end: float) -> float | None:
        lo, hi = bisect_left(self._times, start), bisect_left(self._times, end)
        return sum(self._levels[lo:hi]) / (hi - lo) if hi > lo else None

    def jump(self, start: float, bucket_s: float, ref_s: float) -> float:
        """Mean loudness in `[start, start + bucket_s)` minus the mean of the `ref_s` seconds before it,
        in dB. 0 when either side has no reading."""
        now, before = self._mean(start, start + bucket_s), self._mean(start - ref_s, start)
        return now - before if now is not None and before is not None else 0.0


@dataclass(frozen=True, slots=True)
class Bucket:
    """Counts for the chat in `[t, t + bucket_s)`."""

    t: float
    msgs: int = 0
    uniq: int = 0
    reaction_count: int = 0
    clipreq: int = 0
    audio: float = 0.0

    @property
    def react(self) -> float:
        """Share of the bucket's messages that contain a reaction token (0 for an empty bucket)."""
        return self.reaction_count / self.msgs if self.msgs else 0.0

    def value(self, feature: str) -> float:
        """The bucket's value for one of `config.FEATURES`."""
        return float(getattr(self, feature))


def make_buckets(
    items: Sequence[Counted],
    origin: float,
    bucket_s: float,
    count: int,
    *,
    max_per_user: int,
    loudness: LoudnessTrack | None = None,
    audio_ref_s: float = 0.0,
) -> list[Bucket]:
    """Return `count` back-to-back buckets starting at `origin` from messages sorted by time.

    Each user counts at most `max_per_user` messages per bucket (their first ones). Messages outside
    `[origin, origin + count * bucket_s)` are ignored. Replay mode calls this once for the whole file; live
    mode calls it on every step with `origin` chosen so that the last bucket is the newest `bucket_s`.
    """
    msgs = [0] * count
    reacts = [0] * count
    clips = [0] * count
    users: list[dict[str, int]] = [{} for _ in range(count)]
    end = origin + count * bucket_s
    for index in range(bisect_left(items, origin, key=lambda item: item.t), len(items)):
        item = items[index]
        if item.t >= end:
            break
        slot = min(int((item.t - origin) // bucket_s), count - 1)
        seen = users[slot].get(item.user_id, 0)
        if seen >= max_per_user:
            continue
        users[slot][item.user_id] = seen + 1
        msgs[slot] += 1
        reacts[slot] += item.react
        clips[slot] += item.clip
    return [
        Bucket(
            t=origin + slot * bucket_s,
            msgs=msgs[slot],
            uniq=len(users[slot]),
            reaction_count=reacts[slot],
            clipreq=clips[slot],
            audio=loudness.jump(origin + slot * bucket_s, bucket_s, audio_ref_s) if loudness else 0.0,
        )
        for slot in range(count)
    ]

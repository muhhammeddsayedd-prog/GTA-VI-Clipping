"""Find a clip inside its re-downloaded footage and choose the full moment's cut (M20 L3). Pure logic.

The fetched file covers the clip plus padding. Its fresh local transcript is matched against the words stored
on the clip (`clips.transcript`, clip time) to find where the clip starts in the file; this fixes the drift of
live clips (their offsets are estimated from the stream start) and any edit-list offset of the download.
The cut then widens the clip toward the moment's estimated length, more before (the build-up) than after,
and snaps both ends to pauses in speech so no sentence is cut.
"""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from sixstars.factory.lines import Word


@dataclass(frozen=True)
class CutRules:
    """Setting `longform.cut`."""

    pause_s: float = 0.4
    snap_before_s: float = 3.0
    snap_after_s: float = 3.0
    lead_share: float = 0.6
    match_tolerance_s: float = 0.6
    min_matches: int = 3
    max_shift_s: float = 12.0


@dataclass(frozen=True)
class Cut:
    """The cut in file time, and how the clip was found."""

    start_s: float
    end_s: float
    clip_start_s: float
    matched: int

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


_TOKEN = re.compile(r"[a-z0-9']+")


def norm(text: str) -> str:
    """Lower-case word without punctuation ('' when nothing is left)."""
    found = _TOKEN.findall(text.lower())
    return "".join(found)


def locate_clip(
    file_words: Sequence[Word], clip_words: Sequence[Word], nominal_s: float, rules: CutRules
) -> tuple[float, int]:
    """Where the clip starts in the file: the offset most word pairs agree on (within the tolerance),
    near `nominal_s`. Falls back to `nominal_s` with fewer than `rules.min_matches` agreeing words."""
    votes: Counter[float] = Counter()
    by_token: dict[str, list[float]] = {}
    for w in file_words:
        token = norm(w.text)
        if len(token) >= 2:
            by_token.setdefault(token, []).append(w.start_s)
    step = rules.match_tolerance_s
    for w in clip_words:
        for start in by_token.get(norm(w.text), ()):
            offset = start - w.start_s
            if abs(offset - nominal_s) <= rules.max_shift_s:
                votes[round(offset / step) * step] += 1
    if not votes:
        return nominal_s, 0
    bucket, count = max(votes.items(), key=lambda kv: (kv[1], -abs(kv[0] - nominal_s)))
    if count < rules.min_matches:
        return nominal_s, count
    # refine: mean of the offsets inside the winning bucket
    exact = [
        start - w.start_s
        for w in clip_words
        for start in by_token.get(norm(w.text), ())
        if abs((start - w.start_s) - bucket) <= step
    ]
    return (sum(exact) / len(exact) if exact else bucket), count


def pauses(words: Sequence[Word], pause_s: float) -> list[float]:
    """Times in the middle of every silence of at least `pause_s` between two words (plus the edges)."""
    points: list[float] = []
    ordered = sorted(words, key=lambda w: w.start_s)
    for a, b in zip(ordered, ordered[1:], strict=False):
        if b.start_s - a.end_s >= pause_s:
            points.append((a.end_s + b.start_s) / 2)
    return points


def choose_cut(
    file_words: Sequence[Word],
    clip_start_s: float,
    clip_len_s: float,
    target_s: float,
    file_len_s: float,
    rules: CutRules,
    min_s: float,
    max_s: float,
) -> tuple[float, float]:
    """Widen [clip_start, clip_start + clip_len] toward `target_s`, snap to pauses, clamp to the file."""
    extra = max(target_s - clip_len_s, 0.0)
    start = clip_start_s - extra * rules.lead_share
    end = clip_start_s + clip_len_s + extra * (1 - rules.lead_share)
    gaps = pauses(file_words, rules.pause_s)
    before = [p for p in gaps if start - rules.snap_before_s <= p <= start + 0.5]
    if before:
        start = max(before)  # the closest pause at or before the wanted start
    after = [p for p in gaps if end - 0.5 <= p <= end + rules.snap_after_s]
    if after:
        end = min(after)  # the first pause at or after the wanted end
    start, end = max(start, 0.0), min(end, file_len_s)
    if end - start > max_s:
        end = start + max_s
    if end - start < min_s:
        missing = min_s - (end - start)
        start = max(0.0, start - missing * rules.lead_share)
        end = min(file_len_s, start + min_s)
    return round(start, 3), round(end, 3)


def align(
    file_words: Sequence[Word],
    clip_words: Sequence[Word],
    *,
    nominal_s: float,
    clip_len_s: float,
    target_s: float,
    file_len_s: float,
    rules: CutRules,
    min_s: float,
    max_s: float,
) -> Cut:
    """Locate the clip in the file and choose the moment's cut."""
    clip_start, matched = locate_clip(file_words, clip_words, nominal_s, rules)
    start, end = choose_cut(file_words, clip_start, clip_len_s, target_s, file_len_s, rules, min_s, max_s)
    return Cut(start, end, round(clip_start, 3), matched)


def words_in(words: Sequence[Word], start_s: float, end_s: float) -> list[Word]:
    """The words inside [start, end], shifted to segment time (0 = start)."""
    return [
        Word(w.text, round(max(w.start_s - start_s, 0.0), 3), round(min(w.end_s, end_s) - start_s, 3))
        for w in words
        if w.start_s >= start_s - 0.05 and w.end_s <= end_s + 0.05 and w.end_s > w.start_s
    ]


def stored_words(transcript: object) -> list[Word]:
    """`clips.transcript` (`{"words": [{"w", "s", "e"}]}`, clip time) as Words; [] when malformed."""
    if not isinstance(transcript, dict):
        return []
    out: list[Word] = []
    for item in transcript.get("words") or []:
        try:
            out.append(Word(str(item["w"]), float(item["s"]), float(item["e"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out

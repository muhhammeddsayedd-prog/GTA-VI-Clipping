"""The code checks on a candidate title, the choice between candidates, the title's traits for the Learning
Loop and the fallback title. Pure functions, no network (docs/30-titles-v2.md Part 2).

A candidate is rejected when any check fails. The first survivor in pattern order is used; on a share of
clips (`explore_share`, decided by the clip id so a rerun gives the same answer) the second one is, so the
Learning Loop can compare patterns.
"""

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sixstars.titles.facts import NameBook, TitleFacts

PATTERNS = ("stakes", "emotion", "hot_take", "streamer_on_streamer", "discovery", "fail")

MIN_WORDS = 6
MAX_WORDS = 12
MAX_CHARS = 70
MAX_EMOJI = 2
MAX_CAPS_WORDS = 1
CAPTION_MAX_CHARS = 80
# Fixed by the research (rule 5 of the prompt), not tuning numbers.
BANNED_PHRASES = (
    "chat",
    "loses it",
    "losing it",
    "lost it",
    "goes sideways",
    "off the rails",
    "takes a turn",
    "this moment",
    "you won't believe",
    "wait for it",
    "insane",
    "craziest",
)
CAPS_ALLOWED = frozenset({"GTA", "VI", "GTAVI", "NPC", "NPCS", "FPS", "POV", "RP", "IRL", "AI", "USA", "UK"})
# A claim the title may only make when the facts say so ("first", "beat", "finish" and their forms).
CLAIM_STEMS = ("first", "only", "record", "beat", "finish")
# A story clip's title and caption must not hint at the story (the spoiler rule of P2 still holds).
SPOILER_WORDS = (
    "ending",
    "ends",
    "ended",
    "finale",
    "final mission",
    "dies",
    "died",
    "death",
    "twist",
    "betray",
    "betrays",
    "betrayed",
    "betrayal",
    "killed off",
)
STAKE_WORDS = re.compile(
    r"\$\d|\b(?:hours?|days?|progress|lost|loses|lose|money|bank|broke|banned|ban|swatted|record)\b",
    re.IGNORECASE,
)

_EMOJI = re.compile(
    "[\U0001f300-\U0001f3fa\U0001f400-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u2300-\u23ff\U0001f1e6-\U0001f1ff]"
)
_EMOJI_JOINERS = re.compile("[\ufe0f\u200d\U0001f3fb-\U0001f3ff]")
_NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*[kKmM]?%?")
_GTA_NUMBER = re.compile(r"\bGTA\s*(?:6|VI)\b", re.IGNORECASE)
_QUOTES = ("\"'“”‘’")  # fmt: skip


def _phrase_re(phrase: str) -> re.Pattern[str]:
    words = [re.escape(word) for word in phrase.split()]
    return re.compile(r"(?<![\w'])" + r"\s+".join(words) + r"(?![\w])", re.IGNORECASE)


_BANNED_RES = tuple((phrase, _phrase_re(phrase)) for phrase in BANNED_PHRASES)
_SPOILER_RES = tuple((word, _phrase_re(word)) for word in SPOILER_WORDS)


def emoji_count(text: str) -> int:
    """Emoji in `text` (skin tones, joiners and variation selectors are not counted)."""
    return len(_EMOJI.findall(_EMOJI_JOINERS.sub("", text)))


def strip_emoji(text: str) -> str:
    return " ".join(_EMOJI_JOINERS.sub("", _EMOJI.sub(" ", text)).split())


def words(title: str) -> list[str]:
    """The words of a title: whitespace-separated tokens that hold a letter or a digit (emoji, "&" and
    lone punctuation are not words). "GTA 6" and "GTA VI" are one word, the game's name."""
    text = _GTA_NUMBER.sub(lambda match: match.group().replace(" ", ""), strip_emoji(title))
    return [token for token in text.split() if any(ch.isalnum() for ch in token)]


def emoji_only_at_end(title: str) -> bool:
    """True when no emoji is followed by a word."""
    text = _EMOJI_JOINERS.sub("", title).rstrip()
    while text and (_EMOJI.match(text[-1]) or text[-1].isspace()):
        text = text[:-1]
    return emoji_count(text) == 0


def caps_words(title: str, names: Iterable[str] = ()) -> list[str]:
    """Words written in ALL CAPS (two letters or more), not counting the allowed acronyms, words with a
    digit ("10K", "N3ON") or a streamer name written the way it is spelled (`names`)."""
    allowed = CAPS_ALLOWED | {name.upper() for name in names if name.isupper()}
    found: list[str] = []
    for token in words(title):
        core = re.sub(r"(?:'s|’s)$", "", token.strip(".,!:;\"'“”‘’()"))
        letters = [ch for ch in core if ch.isalpha()]
        if len(letters) < 2 or any(ch.isdigit() for ch in core):
            continue
        if core.isupper() and core.upper() not in allowed:
            found.append(core)
    return found


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())


def _contains_name(text: str, alias: str) -> bool:
    return bool(re.search(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", _norm(text)))


def starts_with_name(title: str, aliases: Iterable[str]) -> bool:
    """The title opens with one of `aliases` (lowercase), alone or as "<name>'s"."""
    head = _norm(strip_emoji(title))
    for alias in aliases:
        if alias and (head == alias or head.startswith(alias + " ") or head.startswith(alias + "'s")):
            return True
    return False


def _value(token: str) -> float | None:
    """`"$10K"` → 10000.0, `"27%"` → 27.0, `"1,500"` → 1500.0."""
    text = token.strip().lstrip("$").rstrip("%").replace(",", "").strip()
    scale = 1.0
    if text[-1:].lower() == "k":
        scale, text = 1_000.0, text[:-1].strip()
    elif text[-1:].lower() == "m":
        scale, text = 1_000_000.0, text[:-1].strip()
    try:
        return float(text) * scale
    except ValueError:
        return None


def title_numbers(title: str) -> list[float]:
    """Every number in the title except the 6 of "GTA 6"."""
    text = _GTA_NUMBER.sub(" ", title)
    found = (_value(match.group()) for match in _NUMBER.finditer(text))
    return [value for value in found if value is not None]


def evidence_numbers(facts: TitleFacts) -> set[float]:
    """Numbers the title may use: those in `facts.numbers` and any written in the event or the peak line,
    each also rounded and cut to a whole number (5.6 hours may be "5 Hours" or "6 Hours")."""
    values: set[float] = set()
    raw: list[float] = [float(v) for v in facts.numbers.values()]
    for text in (facts.event, facts.peak_line or ""):
        raw.extend(
            value
            for match in _NUMBER.finditer(_GTA_NUMBER.sub(" ", text))
            if (value := _value(match.group())) is not None
        )
    for value in raw:
        values.update({value, float(round(value)), float(int(value))})
    return values


def _evidence_text(facts: TitleFacts) -> str:
    return _norm(" ".join([facts.event, facts.peak_line or "", " ".join(facts.chat_words)]))


def unsupported_claims(title: str, facts: TitleFacts) -> list[str]:
    """Numbers and claim words ("first", "only", "record", "beat", "finish") in the title that the facts do
    not support."""
    problems: list[str] = []
    allowed = evidence_numbers(facts)
    for value in title_numbers(title):
        if value not in allowed:
            problems.append(f"number {value:g} is not in the facts")
    evidence = _evidence_text(facts)
    title_words = _norm(title).split()
    for stem in CLAIM_STEMS:
        if any(word.startswith(stem) for word in title_words) and not any(
            word.startswith(stem) for word in evidence.split()
        ):
            problems.append(f'"{stem}" is not in the facts')
    return problems


def unknown_names(title: str, facts: TitleFacts, book: NameBook) -> list[str]:
    """Streamers named in the title that are neither the clip's streamer nor one of `other_streamers`."""
    allowed = {facts.streamer, *facts.other_streamers}
    return [name for name in book.names_in(title) if name not in allowed]


@dataclass(frozen=True, slots=True)
class Checked:
    """One candidate after the checks. `failed` is empty when it passed."""

    pattern: str
    title: str
    facts_used: tuple[str, ...]
    failed: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "title": self.title,
            "facts_used": list(self.facts_used),
            "failed": list(self.failed),
        }


def check_title(title: str, facts: TitleFacts, book: NameBook) -> list[str]:
    """Every rule the title breaks (empty: it passes). Part 2 of docs/30-titles-v2.md."""
    failed: list[str] = []
    text = title.strip()
    lead = [*book.aliases_of(facts.streamer)]
    for other in facts.other_streamers:
        lead.extend(book.aliases_of(other))
    if not starts_with_name(text, lead):
        failed.append("name_first: does not start with the streamer's name")
    if facts.gta_label.lower() not in text.lower():
        failed.append(f'gta: no "{facts.gta_label}"')
    count = len(words(text))
    if not MIN_WORDS <= count <= MAX_WORDS:
        failed.append(f"length: {count} words (wanted {MIN_WORDS} to {MAX_WORDS})")
    if len(text) > MAX_CHARS:
        failed.append(f"length: {len(text)} characters (max {MAX_CHARS})")
    banned = [phrase for phrase, pattern in _BANNED_RES if pattern.search(text)]
    if banned:
        failed.append("banned: " + ", ".join(banned))
    emoji = emoji_count(text)
    if emoji > MAX_EMOJI:
        failed.append(f"emoji: {emoji} (max {MAX_EMOJI})")
    elif emoji and not emoji_only_at_end(text):
        failed.append("emoji: not at the end")
    caps = caps_words(text, [facts.streamer, *facts.other_streamers])
    if len(caps) > MAX_CAPS_WORDS:
        failed.append("caps: " + ", ".join(caps))
    if "#" in text or "?" in text:
        failed.append("format: has # or ?")
    plain = strip_emoji(text)
    if len(plain) >= 2 and plain[0] in _QUOTES and plain[-1] in _QUOTES:
        failed.append("format: only a quote")
    failed.extend("facts: " + problem for problem in unsupported_claims(text, facts))
    failed.extend(f"names: {name} is not in the clip" for name in unknown_names(text, facts, book))
    if facts.spoiler:
        hints = [word for word, pattern in _SPOILER_RES if pattern.search(text)]
        if hints:
            failed.append("spoiler: " + ", ".join(hints))
    return failed


def check_caption(caption: str | None, facts: TitleFacts) -> list[str]:
    """The TikTok and Instagram line: short, no hashtags, no banned phrase, no story hint."""
    if not caption or not caption.strip():
        return ["caption: empty"]
    failed: list[str] = []
    if len(caption) > CAPTION_MAX_CHARS:
        failed.append(f"caption: {len(caption)} characters (max {CAPTION_MAX_CHARS})")
    if "#" in caption:
        failed.append("caption: has #")
    banned = [phrase for phrase, pattern in _BANNED_RES if pattern.search(caption)]
    if banned:
        failed.append("caption banned: " + ", ".join(banned))
    if facts.spoiler and any(pattern.search(caption) for _, pattern in _SPOILER_RES):
        failed.append("caption: spoiler")
    return failed


def check_all(candidates: Sequence[Mapping[str, Any]], facts: TitleFacts, book: NameBook) -> list[Checked]:
    """Check every candidate (`{"pattern", "title", "facts_used"}`). An unknown pattern fails too."""
    checked: list[Checked] = []
    for item in candidates:
        title = str(item.get("title") or "").strip()
        pattern = str(item.get("pattern") or "")
        failed = check_title(title, facts, book) if title else ["empty"]
        if pattern not in PATTERNS:
            failed = [*failed, f"pattern: unknown {pattern!r}"]
        used = item.get("facts_used")
        facts_used = tuple(str(x) for x in used) if isinstance(used, list) else ()
        checked.append(Checked(pattern, title, facts_used, tuple(failed)))
    return checked


def explore(clip_id: str, share: float) -> bool:
    """True for about `share` of clips, the same answer for the same clip every time."""
    if share <= 0:
        return False
    digest = hashlib.sha256(clip_id.encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32 < share


@dataclass(frozen=True, slots=True)
class Choice:
    """The candidate to use: its index in the candidate list, and whether the runner-up was taken."""

    index: int
    explored: bool


def choose(checked: Sequence[Checked], *, exploring: bool) -> Choice | None:
    """The first survivor in pattern order (the model's own order breaks ties); with `exploring` and two
    or more survivors, the second. None when nothing passed."""
    survivors = sorted(
        (i for i, item in enumerate(checked) if item.passed),
        key=lambda i: (PATTERNS.index(checked[i].pattern), i),
    )
    if not survivors:
        return None
    if exploring and len(survivors) > 1:
        return Choice(survivors[1], True)
    return Choice(survivors[0], False)


def fallback_title(facts: TitleFacts, topic: str | None) -> str:
    """When no candidate passes twice: "<Streamer>'s <Topic Words> in GTA 6 😭", or "<Streamer> in GTA 6 😭"
    without a topic. Kept within the length limits."""
    noun = " ".join(word.capitalize() for word in (topic or "").replace("_", "-").split("-") if word)
    head = f"{facts.streamer}'s {noun}" if noun else facts.streamer
    title = f"{head} in {facts.gta_label} 😭"
    while (len(words(title)) > MAX_WORDS or len(title) > MAX_CHARS) and " " in head:
        head = head.rsplit(" ", 1)[0]
        title = f"{head} in {facts.gta_label} 😭"
    return title


@dataclass(frozen=True, slots=True)
class Features:
    """The traits of the title used, stored for the Learning Loop (docs/28 Star Score)."""

    pattern: str
    word_count: int
    char_count: int
    has_number: bool
    has_stake: bool
    emoji_count: int
    caps_words: int
    name_tier: str
    names: int
    explored: bool
    source: str
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title_pattern": self.pattern,
            "word_count": self.word_count,
            "char_count": self.char_count,
            "has_number": self.has_number,
            "has_stake": self.has_stake,
            "emoji_count": self.emoji_count,
            "caps_words": self.caps_words,
            "name_tier": self.name_tier,
            "names": self.names,
            "explored": self.explored,
            "source": self.source,
            **self.extra,
        }


def features(
    title: str, pattern: str, facts: TitleFacts, book: NameBook, *, explored: bool, source: str
) -> Features:
    return Features(
        pattern=pattern,
        word_count=len(words(title)),
        char_count=len(title),
        has_number=bool(title_numbers(title)),
        has_stake=pattern == "stakes" or bool(STAKE_WORDS.search(title)),
        emoji_count=emoji_count(title),
        caps_words=len(caps_words(title, [facts.streamer, *facts.other_streamers])),
        name_tier=facts.streamer_tier,
        names=len(
            set(book.names_in(title))
            | ({facts.streamer} if starts_with_name(title, book.aliases_of(facts.streamer)) else set())
        ),
        explored=explored,
        source=source,
    )


def retry_note(checked: Sequence[Checked]) -> str:
    """The extra instruction for the one retry when no candidate passed."""
    lines = [f'- "{item.title}": ' + "; ".join(item.failed) for item in checked]
    return (
        "None of your candidates passed the checks:\n"
        + "\n".join(lines)
        + "\nReply again with the whole JSON object: five new candidates that follow every rule. Start each "
        "title with the streamer's name exactly as given, include the gta_label, 6 to 12 words, and use only "
        "numbers and claims that are in the facts."
    )

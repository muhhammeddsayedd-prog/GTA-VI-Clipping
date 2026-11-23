"""Stage 3 of the cover engine (docs/29a section 7): the cover line.

One call to P12 (`ai.calls.write_cover_lines`, the fast model unless `ai.model.p12_cover_hookline` says
otherwise) returns three candidates; every rule of docs/29a section 6.3 is then checked here in code and a
failing candidate is dropped. When none survives the cover has no line (the plain best frame).

Checks (`validate`, pure): 1-5 words in capitals that wrap into at most 2 lines of 14 characters; no
emoji, at most one "?!" and no other repeated punctuation; none of the banned words (INSANE, CRAZY, YOU
WON'T BELIEVE, SPOILER) and none of the clip titles' outcome words (`ai.titles.spoils_ending`, the same
list the title writer obeys); at most 2 content words shared with the post title; the `source` found in
the transcript (fuzzy match >= 0.8 over a window of the same length); not a near-repeat of a recent line.
Profanity in the line is masked F*CK-style before the checks.
"""

import difflib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sixstars.ai import titles

MAX_WORDS = 5
MAX_LINES = 2
MAX_LINE_CHARS = 14
SOURCE_MATCH = 0.8
REPEAT_MATCH = 0.9
MAX_SHARED_CONTENT_WORDS = 2
TRANSCRIPT_PAD_S = 5.0
RECENT_LINES = 20
BANNED_PHRASES = ("INSANE", "CRAZY", "YOU WON'T BELIEVE", "YOU WONT BELIEVE", "SPOILER")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "by",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "his",
        "our",
        "their",
        "gta",
        "6",
        "vi",
        "gta6",
        "in-game",
    ]
)

LineStyle = Literal["quote", "label"]

_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿←-⇿⬀-⯿️]")
_REPEATED_PUNCT = re.compile(r"[!?]{3,}|!!|\?\?")
_WORD = re.compile(r"[^A-Z0-9*']+")
_VOWELS = re.compile(r"[AEIOU]")


@dataclass(frozen=True, slots=True)
class Candidate:
    """A cover line candidate after masking, with its checks."""

    text: str
    style: LineStyle
    key_word: str
    source: str
    lines: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.problems

    def as_json(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "style": self.style,
            "key_word": self.key_word,
            "source": self.source,
            "lines": list(self.lines),
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class LineContext:
    """What a cover line is checked against."""

    title: str
    transcript_words: Sequence[str]
    recent_lines: Sequence[str] = ()
    profanity: frozenset[str] = field(default_factory=frozenset)


# ── Pure checks ───────────────────────────────────────────────


def words_of(text: str) -> list[str]:
    """Upper-case word tokens (letters, digits, apostrophes, the mask star). Pure."""
    return [w for w in _WORD.split(text.upper()) if w.strip("'")]


def wrap(text: str, max_lines: int = MAX_LINES, width: int = MAX_LINE_CHARS) -> tuple[str, ...] | None:
    """Greedy wrap into at most `max_lines` lines of `width` characters; None when it does not fit (a
    word longer than a line never fits). Pure."""
    lines: list[str] = []
    for word in text.split():
        if len(word) > width:
            return None
        if lines and len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    if not lines or len(lines) > max_lines:
        return None
    return tuple(lines)


def mask_profanity(text: str, profanity: frozenset[str]) -> str:
    """Mask each profane word F*CK-style (vowels after the first letter become stars). Pure."""

    def mask(match: re.Match[str]) -> str:
        word = match.group(0)
        if word.lower().strip("'") not in profanity:
            return word
        return word[0] + _VOWELS.sub("*", word[1:])

    return re.sub(r"[A-Za-z']+", mask, text)


def content_words(text: str) -> set[str]:
    """The words that carry meaning (stopwords and the game's name left out). Pure."""
    return {w.strip("'").lower() for w in words_of(text) if w.strip("'").lower() not in STOPWORDS}


def source_found(source: str, transcript_words: Sequence[str]) -> bool:
    """Is `source` in the transcript: a window of the same number of words with a fuzzy match of at least
    0.8 (case and punctuation ignored)? Pure."""
    wanted = [w.strip("'").lower() for w in words_of(source)]
    said = [w.strip("'").lower() for w in words_of(" ".join(transcript_words))]
    if not wanted or not said:
        return False
    target = " ".join(wanted)
    size = len(wanted)
    for start in range(0, max(1, len(said) - size + 1)):
        window = " ".join(said[start : start + size])
        if difflib.SequenceMatcher(None, target, window).ratio() >= SOURCE_MATCH:
            return True
    return False


def validate(raw: Mapping[str, Any], context: LineContext) -> Candidate:
    """Check one model candidate against docs/29a section 6.3. Pure."""
    style: LineStyle = "label" if raw.get("style") == "label" else "quote"
    text = " ".join(str(raw.get("text") or "").split())
    text = mask_profanity(text, context.profanity)
    source = str(raw.get("source") or "")
    problems: list[str] = []
    words = words_of(text)
    if not 1 <= len(words) <= MAX_WORDS:
        problems.append(f"words={len(words)}")
    if text != text.upper():
        problems.append("not_capitals")
    lines = wrap(text)
    if lines is None:
        problems.append("too_long")
    if _EMOJI.search(text):
        problems.append("emoji")
    if text.count("?!") > 1 or _REPEATED_PUNCT.search(text.replace("?!", "")):
        problems.append("punctuation")
    upper = text.upper().replace("’", "'")
    for phrase in BANNED_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", upper):
            problems.append(f"banned:{phrase.lower()}")
    outcome = titles.spoils_ending(text)
    if outcome:
        problems.append("spoils_outcome")
    shared = content_words(text) & content_words(context.title)
    if len(shared) > MAX_SHARED_CONTENT_WORDS:
        problems.append("repeats_title")
    if not source_found(source, context.transcript_words):
        problems.append("source_not_in_transcript")
    for recent in context.recent_lines:
        if difflib.SequenceMatcher(None, text.upper(), recent.upper()).ratio() >= REPEAT_MATCH:
            problems.append("repeats_recent")
            break
    key_word = str(raw.get("key_word") or "").upper().strip()
    if key_word not in words:
        key_word = max(words, key=len) if words else ""
    return Candidate(text, style, key_word, source, lines or (), tuple(problems))


def transcript_window(
    words: Sequence[tuple[str, float, float]], peak_s: float | None, pad_s: float = TRANSCRIPT_PAD_S
) -> list[str]:
    """The words said within `pad_s` of the peak (the whole transcript when there is no peak). Pure."""
    if peak_s is None:
        return [w for w, _s, _e in words]
    return [w for w, start, end in words if end >= peak_s - pad_s and start <= peak_s + pad_s]


# ── The call ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LineInput:
    """What P12 gets for one clip."""

    clip_id: str
    title: str
    streamer: str
    tags: Sequence[str]
    transcript: Sequence[tuple[str, float, float]]  # (word, start, end), clip time
    peak_s: float | None
    recent_lines: Sequence[str]


@dataclass(frozen=True, slots=True)
class LineResult:
    """Every candidate with its checks; `valid` keeps the order the model gave."""

    candidates: tuple[Candidate, ...]

    @property
    def valid(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.valid)


def prompt_values(item: LineInput) -> dict[str, object]:
    """The P12 user template values. Pure."""
    window = transcript_window(item.transcript, item.peak_s)
    return {
        "title": item.title or "(none)",
        "streamer": item.streamer or "(unknown)",
        "tags": ", ".join(item.tags) if item.tags else "(none)",
        "recent_lines": json.dumps(list(item.recent_lines[:RECENT_LINES]), ensure_ascii=False),
        "transcript": " ".join(window) or "(no speech)",
    }


def write_line(item: LineInput, *, profanity: frozenset[str] = frozenset(), client: Any = None) -> LineResult:
    """Ask P12 for candidates and check each one. Raises what the AI client raises (budget, refusal)."""
    from sixstars.ai import calls

    reply = calls.write_cover_lines(prompt_values(item), client=client, clip_id=item.clip_id)
    context = LineContext(
        title=item.title,
        transcript_words=[w for w, _s, _e in item.transcript],
        recent_lines=item.recent_lines,
        profanity=profanity,
    )
    return LineResult(tuple(validate(c.model_dump(), context) for c in reply.candidates))


# ── Inputs from the database, and the C4 eval ────────────────


_CLIP_SQL = """
select c.id::text as id, c.title, c.hook, c.topic, c.moment_type, c.transcript,
       (to_jsonb(c) ->> 'peak_s')::numeric as peak_s,  -- null before migration 0044
       coalesce(cr.display_name, cr.handle, '') as streamer
from clips c left join creators cr on cr.id = c.creator_id
where c.id = %s
"""


def event_tags(texts: Sequence[str], moment_type: str | None, topic: str | None) -> list[str]:
    """Event tags for the prompt: the first-time phrases and event types of `boost.first_time` (the same
    lists the scoring overhaul uses), plus the writer's moment type and topic. Pure apart from the config
    read."""
    from sixstars.scoring import config as scoring_config
    from sixstars.scoring import firsttime

    flag = scoring_config.section("boost.first_time")
    phrases = [str(p) for p in flag.get("phrases") or []] if isinstance(flag.get("phrases"), list) else []
    tags = [f"first time ({p})" for p in firsttime.find_phrases(texts, phrases)[:1]]
    tags += [
        name.replace("_", " ") for name in firsttime.find_events(texts, firsttime.event_types_setting(flag))
    ]
    if moment_type:
        tags.append(f"moment: {moment_type}")
    if topic:
        tags.append(f"topic: {topic.replace('-', ' ')}")
    return tags


def load_input(clip_id: str, account_ids: Sequence[str] = ()) -> LineInput | None:
    """The P12 input of a clip: its title, streamer, tags, transcript and peak, and the recent cover lines
    of the accounts it goes to. None when the clip does not exist."""
    from sixstars import db
    from sixstars.covers import store
    from sixstars.gate.store import transcript_words

    row = db.fetch_one(_CLIP_SQL, (clip_id,))
    if row is None:
        return None
    words = transcript_words(row["transcript"])
    recent: list[str] = []
    for account in account_ids:
        recent += [line for line in store.recent_lines(account, RECENT_LINES) if line not in recent]
    peak = float(row["peak_s"]) if row["peak_s"] is not None else None
    return LineInput(
        clip_id=clip_id,
        title=str(row["title"] or row["hook"] or ""),
        streamer=str(row["streamer"] or ""),
        tags=event_tags([w for w, _s, _e in words], row["moment_type"], row["topic"]),
        transcript=words,
        peak_s=peak,
        recent_lines=recent[:RECENT_LINES],
    )


def profanity_terms() -> frozenset[str]:
    """The pipeline's profanity list (`assets/profanity.txt`), empty when missing."""
    from sixstars.factory.assets import PROFANITY_LIST, assets_dir
    from sixstars.factory.bleep import load_profanity_list

    path = assets_dir() / PROFANITY_LIST
    return load_profanity_list(path) if path.is_file() else frozenset()


_EVAL_SQL = """
select c.id::text as id from clips c
where c.transcript is not null and jsonb_array_length(coalesce(c.transcript->'words', '[]'::jsonb)) >= 8
  and c.title is not null
order by c.created_at desc
limit %s
"""


def evaluate(limit: int = 50) -> dict[str, Any]:
    """C4 eval (docs/29a section 9): P12 on the newest clips with a transcript and a title. Done when at
    least 45 of 50 have a valid candidate and no kept candidate invents content (every kept line's source
    is in the transcript, which `validate` enforces)."""
    from datetime import UTC, datetime

    from sixstars import db
    from sixstars.ai import costs

    started = datetime.now(UTC)
    profanity = profanity_terms()
    rows: list[dict[str, Any]] = []
    for row in db.fetch_all(_EVAL_SQL, (limit,)):
        item = load_input(str(row["id"]))
        if item is None:
            continue
        try:
            result = write_line(item, profanity=profanity)
        except Exception as exc:  # noqa: BLE001 - one failed call is a failed clip, not a failed eval
            rows.append({"clip_id": item.clip_id, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        rows.append(
            {
                "clip_id": item.clip_id,
                "title": item.title,
                "valid": [c.text for c in result.valid],
                "candidates": [c.as_json() for c in result.candidates],
            }
        )
    spend = [s for s in costs.spend_by_operation(since=started) if s.operation == "p12_cover_hookline"]
    cost = sum(s.cost_usd for s in spend)
    with_line = sum(1 for r in rows if r.get("valid"))
    problems: dict[str, int] = {}
    for r in rows:
        for c in r.get("candidates", []):
            for p in c["problems"]:
                key = p.split(":")[0] if not p.startswith("banned") else p
                problems[key] = problems.get(key, 0) + 1
    return {
        "clips": len(rows),
        "with_valid_line": with_line,
        "errors": sum(1 for r in rows if "error" in r),
        "problems": problems,
        "cost_usd": round(cost, 4),
        "cost_per_100_clips_usd": round(cost * 100 / max(len(rows), 1), 4),
        "models": sorted({s.model for s in spend}),
        "rows": rows,
    }

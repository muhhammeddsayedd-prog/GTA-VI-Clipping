"""Title rules for P2 (YouTube lane tuning, 2026-11-21): a title sets up the tension and never states the
outcome. Titles that gave the ending away ("... ends in a crash", "... ends in the ocean") got about a tenth
of the views of the Shorts posted the same day; "X's plan goes wrong" and "chat lost it when ..." did best.

Pure functions, no network.
"""

import re

# The outcome stated outright. Whole words, any case.
_BANNED = (
    (re.compile(r"\bends\s+(?:in|with|up)\b", re.IGNORECASE), "ends in"),
    (re.compile(r"\bend(?:ed)?\s+in\s+(?:a|an|the)\b", re.IGNORECASE), "ends in"),
    (re.compile(r"\bcrash(?:es|ed)\b", re.IGNORECASE), "crashes"),
    (re.compile(r"\b(?:dies|died)\b", re.IGNORECASE), "dies"),
    (re.compile(r"\b(?:sinks|sank|sunk)\b", re.IGNORECASE), "sinks"),
    (re.compile(r"\bfail(?:s|ed)\s+at\b", re.IGNORECASE), "fails at"),
)


def spoils_ending(title: str | None) -> str | None:
    """Why `title` gives the ending away (the banned phrase it uses), or None when it does not."""
    if not title:
        return None
    for pattern, name in _BANNED:
        if pattern.search(title):
            return f'the title states the outcome ("{name}")'
    return None


def correction_note(title: str, problem: str) -> str:
    """The extra instruction for the one retry after a title that `spoils_ending`."""
    return (
        f"Your previous title was {title!r}: {problem}. Reply again with the whole JSON object. Keep "
        "everything else, but write a new `title` that names the streamer and sets up the tension without "
        'saying how it ends: never "ends in", "crashes", "dies", "sinks" or "fails at". Patterns that work: '
        '"X\'s plan goes wrong", "X had ONE job", "chat lost it when ...".'
    )

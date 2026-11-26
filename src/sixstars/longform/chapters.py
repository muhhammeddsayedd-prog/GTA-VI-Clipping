"""YouTube chapters, title and description of an episode (M20 L4). Pure logic.

YouTube rules (docs/14 §3, checked 2026-11-25): the first timestamp is 00:00, at least three timestamps in
ascending order, every chapter at least 10 seconds. The cold open is short, so it is part of the first
chapter; the end card is part of the last.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

MIN_CHAPTERS = 3
MIN_CHAPTER_S = 10.0
TITLE_MAX = 100
DESCRIPTION_MAX = 5000


@dataclass(frozen=True)
class Moment:
    """One played segment: countdown number, creator, its stored title, credit link and real length."""

    position: int
    creator: str
    title: str
    credit: str
    duration_s: float


@dataclass(frozen=True)
class Chapter:
    start_s: float
    label: str


def stamp(seconds: float) -> str:
    """`M:SS`, or `H:MM:SS` from one hour (YouTube accepts both; 00:00 for the first)."""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def chapters(moments: Sequence[Moment], cold_open_s: float) -> list[Chapter]:
    """One chapter per moment in playback order; the first starts at 0 (it includes the cold open)."""
    out: list[Chapter] = []
    t = cold_open_s
    for i, m in enumerate(moments):
        label = f"#{m.position} {m.creator}: {m.title}".strip()
        out.append(Chapter(0.0 if i == 0 else t, label[:90]))
        t += m.duration_s
    return out


def valid(chs: Sequence[Chapter], total_s: float) -> bool:
    """True when YouTube will show these as chapters."""
    if len(chs) < MIN_CHAPTERS or chs[0].start_s != 0.0:
        return False
    ends = [c.start_s for c in chs[1:]] + [total_s]
    return all(end - c.start_s >= MIN_CHAPTER_S for c, end in zip(chs, ends, strict=True))


def title(n: int, day: date, featured: str | None = None) -> str:
    """The template title (fallback when the P14 writer call fails), at most 100 characters."""
    if featured:
        return f"TOP {n} {featured} GTA 6 Moments ({day.strftime('%b')} {day.day})"[:TITLE_MAX]
    return f"TOP {n} Funniest GTA 6 Stream Moments ({day.strftime('%b')} {day.day})"[:TITLE_MAX]


def template_intro(n: int, day: date, featured: str | None = None) -> str:
    """The template description intro (fallback when the P14 writer call fails)."""
    if featured:
        return (
            f"{featured}'s {n} best GTA 6 stream moments, counted down to #1.\n"
            f"Every clip plays in full. Go follow {featured}."
        )
    return (
        f"The {n} best GTA 6 stream moments of {day.strftime('%B')} {day.day}, counted down to #1.\n"
        "Every clip plays in full. Go follow the streamers."
    )


def description(
    moments: Sequence[Moment],
    chs: Sequence[Chapter],
    day: date,
    footer: str,
    intro: str | None = None,
    hashtags: Sequence[str] = (),
) -> str:
    """Intro, the chapter list, the streamer credits, the footer and the hashtags (last: YouTube shows the
    first three hashtags of a description above the title). No #Shorts: this is a long-form video."""
    lines = [(intro if intro else template_intro(len(moments), day)).strip(), ""]
    lines += [f"{stamp(c.start_s)} {c.label}" for c in chs]
    credits = sorted({m.credit for m in moments if m.credit})
    if credits:
        lines += ["", "Streamers in this episode:", *credits]
    if footer:
        lines += ["", footer]
    tail = "\n\n" + " ".join(hashtags) if hashtags else ""
    return "\n".join(lines)[: DESCRIPTION_MAX - len(tail)] + tail

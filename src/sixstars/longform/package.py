"""Generated packaging of an episode (M20 go-live): one P14 writer call for the title, description intro,
hashtags, tags, thumbnail words and thumbnail picture. `clean` enforces the limits in code (and repairs small
slips instead of paying for a retry); any error falls back to the template text, so packaging never blocks
an episode.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sixstars import db
from sixstars.ai import calls
from sixstars.ai.schemas import EpisodePackage
from sixstars.clients.anthropic_client import AnthropicClient
from sixstars.logging import get_logger
from sixstars.longform import chapters as ch

log = get_logger(__name__)

TITLE_MAX = 100
INTRO_MAX = 600
HASHTAGS = 3
TAG_MAX = 30
TAGS_TOTAL_MAX = 450  # YouTube allows 500 characters of tags; leave room for the separators it counts
TAGS_MAX = 20
THUMB_TEXT_WORDS = 3
SCENE_MAX = 400
BASE_TAGS = ("gta 6", "gta vi", "gta 6 funny moments", "gta 6 streamers", "gta 6 highlights")
RECENT_TITLES = 10

RECENT_SQL = """
select title from episodes where title is not null and episode_date < %s order by episode_date desc limit %s
"""


@dataclass(frozen=True)
class Package:
    """What the episode shows before a click. `source` is "ai" or "template"."""

    title: str
    intro: str
    hashtags: tuple[str, ...]
    tags: tuple[str, ...]
    thumb_text: str
    thumb_scene: str
    source: str


def _hashtag(raw: str) -> str:
    word = re.sub(r"[^0-9A-Za-z_]", "", raw)
    return f"#{word}" if word else ""


def hashtags_for(raw: Sequence[str], featured: str | None) -> tuple[str, ...]:
    """Exactly three distinct hashtags, #GTA6 first; the streamer second on a creator day."""
    wanted = ["#GTA6"]
    if featured:
        wanted.append(_hashtag(featured))
    wanted += [_hashtag(h) for h in raw]
    wanted += ["#GTA6Clips", "#GTAVI", "#Gaming"]
    out: list[str] = []
    for tag in wanted:
        if tag and tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
    return tuple(out[:HASHTAGS])


def tags_for(raw: Sequence[str], creators: Sequence[str]) -> tuple[str, ...]:
    """Lowercase search tags without '#', each short, together under the YouTube limit; the base tags and
    the creators fill in when the writer gave few."""
    pool = [*raw, *BASE_TAGS, *creators]
    out: list[str] = []
    total = 0
    for t in pool:
        tag = re.sub(r"\s+", " ", t.replace("#", "").replace(",", " ")).strip().lower()
        if not tag or len(tag) > TAG_MAX or tag in out:
            continue
        if total + len(tag) + 1 > TAGS_TOTAL_MAX or len(out) >= TAGS_MAX:
            break
        out.append(tag)
        total += len(tag) + 1
    return tuple(out)


def clean_title(raw: str, fallback: str) -> str:
    text = re.sub(r"\s+", " ", raw.replace("\n", " ")).strip().strip('"').strip()
    if not text:
        return fallback
    if len(text) > TITLE_MAX:
        text = text[:TITLE_MAX].rsplit(" ", 1)[0]
    return text


def clean_thumb_text(raw: str) -> str:
    words = re.sub(r"[^0-9A-Za-z#'!?\- ]", "", raw).upper().split()
    return " ".join(words[:THUMB_TEXT_WORDS])


def clean(
    reply: EpisodePackage, *, n: int, day: date, featured: str | None, creators: Sequence[str]
) -> Package:
    """The writer's reply within every limit."""
    intro = reply.intro.strip()
    if len(intro) > INTRO_MAX:
        intro = intro[:INTRO_MAX].rsplit(".", 1)[0] + "."
    return Package(
        title=clean_title(reply.title, ch.title(n, day, featured)),
        intro=intro or ch.template_intro(n, day, featured),
        hashtags=hashtags_for(reply.hashtags, featured),
        tags=tags_for(reply.tags, creators),
        thumb_text=clean_thumb_text(reply.thumb_text),
        thumb_scene=reply.thumb_scene.strip()[:SCENE_MAX],
        source="ai",
    )


def template(n: int, day: date, featured: str | None, creators: Sequence[str], best_title: str) -> Package:
    """The package without the writer (any P14 error)."""
    return Package(
        title=ch.title(n, day, featured),
        intro=ch.template_intro(n, day, featured),
        hashtags=hashtags_for((), featured),
        tags=tags_for((), creators),
        thumb_text=f"TOP {n}",
        thumb_scene=best_title,
        source="template",
    )


def moment_lines(moments: Sequence[ch.Moment]) -> str:
    return "\n".join(f"#{m.position} {m.creator}: {m.title}" for m in moments)


def recent_titles(day: date) -> list[str]:
    return [str(r["title"]) for r in db.fetch_all(RECENT_SQL, (day, RECENT_TITLES))]


def write(
    episode_id: str,
    moments: Sequence[ch.Moment],
    *,
    day: date,
    fmt: str,
    featured: str | None,
    length_s: float,
    client: AnthropicClient | None = None,
) -> Package:
    """Package one episode. Never raises for a writer problem: the template is used instead."""
    n = len(moments)
    creators = list(dict.fromkeys(m.creator for m in moments))
    best_title = moments[-1].title if moments else ""
    minutes, seconds = divmod(int(length_s), 60)
    values: dict[str, object] = {
        "format": fmt,
        "featured": featured or "(none: many streamers)",
        "count": n,
        "length": f"{minutes}:{seconds:02d}",
        "day": day.isoformat(),
        "recent_titles": "; ".join(recent_titles(day)) or "(none yet)",
        "moments": moment_lines(moments),
    }
    try:
        reply = calls.write_episode_package(values, client=client, episode_id=episode_id)
    except Exception as exc:  # noqa: BLE001 - any writer failure falls back to the template text
        log.warning("episode_package_fallback", episode_id=episode_id, error=str(exc)[:300])
        return template(n, day, featured, creators, best_title)
    return clean(reply, n=n, day=day, featured=featured, creators=creators)

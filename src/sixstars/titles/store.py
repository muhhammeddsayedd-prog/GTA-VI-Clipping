"""Database reads and writes of the title writer v2. All SQL of the feature is here."""

from collections.abc import Mapping, Sequence
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db

# Statuses in which the title may still be changed: the clip has not been posted.
OPEN_STATUSES = ("gating", "precheck_pending", "approved")
# A creator is in the name book when we named them on purpose or clipped them this recently.
BOOK_RECENT_DAYS = 30


def clip_row(clip_id: str) -> Mapping[str, Any] | None:
    """The clip with its creator and the spike it was cut around (chat sample, position on the stream)."""
    return db.fetch_one(
        """
        select c.id::text as id, c.status, c.title, c.caption, c.hashtags, c.hook, c.topic, c.spoiler,
               c.transcript, c.start_s, c.duration_s, c.score_parts, c.style_texts, c.gate_profile,
               c.source_type, c.source_platform, c.spike_id::text as spike_id,
               c.creator_id::text as creator_id,
               cr.handle, cr.display_name, cr.title_name, cr.title_aliases, cr.follower_count,
               s.mode as spike_mode, s.spike_offset_s, s.chat_sample,
               extract(epoch from (s.spike_wall - ls.started_at)) as live_offset_s
          from clips c
          join creators cr on cr.id = c.creator_id
          left join spikes s on s.id = c.spike_id
          left join live_sessions ls on ls.id = s.live_session_id
         where c.id = %s
        """,
        (clip_id,),
    )


def book_rows() -> list[dict[str, Any]]:
    """Creators for the name book: those with a title name set, and those clipped recently."""
    return db.fetch_all(
        """
        select cr.handle, cr.display_name, cr.title_name, cr.title_aliases, cr.follower_count
          from creators cr
         where cr.title_name is not null
            or exists (select 1 from clips c
                        where c.creator_id = cr.id and c.created_at > now() - make_interval(days => %s))
         order by cr.follower_count desc nulls last
        """,
        (BOOK_RECENT_DAYS,),
    )


def insert_run(
    clip_id: str,
    *,
    config_version: int,
    model: str | None,
    facts: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    chosen_index: int | None,
    chosen_title: str,
    caption_short: str | None,
    hashtags: Sequence[str],
    explored: bool,
    source: str,
    features: Mapping[str, Any],
    prior_title: str | None,
) -> int:
    """One `clip_titles` row (not yet applied). Returns its id."""
    row = db.fetch_one(
        """
        insert into clip_titles (clip_id, config_version, model, facts, candidates, chosen_index,
                                 chosen_title, caption_short, hashtags, explored, source, features,
                                 prior_title)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            clip_id,
            config_version,
            model,
            Jsonb(dict(facts)),
            Jsonb(list(candidates)),
            chosen_index,
            chosen_title,
            caption_short,
            list(hashtags),
            explored,
            source,
            Jsonb(dict(features)),
            prior_title,
        ),
    )
    assert row is not None
    return int(row["id"])


def apply(run_id: int, clip_id: str, title: str, caption: str, hashtags: Sequence[str]) -> bool:
    """Write the chosen title, caption and tags on the clip while it is not posted, and mark the run as
    applied, in one transaction. False when the clip was already posted (nothing is changed)."""
    with db.transaction():
        changed = db.execute(
            "update clips set title = %s, caption = %s, hashtags = %s where id = %s and status = any(%s)",
            (title, caption, list(hashtags), clip_id, list(OPEN_STATUSES)),
        )
        if changed:
            db.execute("update clip_titles set applied = true where id = %s", (run_id,))
    return changed > 0


def already_titled(clip_id: str) -> bool:
    """True when a v2 title was already applied to the clip."""
    return (
        db.fetch_one("select 1 from clip_titles where clip_id = %s and applied limit 1", (clip_id,))
        is not None
    )


def queue_ids(limit: int) -> list[str]:
    """Approved clips waiting to post without an applied v2 title, best ranked first."""
    rows = db.fetch_all(
        """
        select c.id::text as id from clips c
         where c.status = 'approved'
           and not exists (select 1 from clip_titles t where t.clip_id = c.id and t.applied)
         order by c.rank_score desc nulls last, c.approved_at desc
         limit %s
        """,
        (limit,),
    )
    return [str(row["id"]) for row in rows]


def recent_ids(limit: int, statuses: Sequence[str]) -> list[str]:
    """The most recent clips in `statuses` that have a transcript, newest first (for a dry comparison)."""
    rows = db.fetch_all(
        """
        select c.id::text as id from clips c
         where c.status = any(%s) and c.transcript is not null and c.title is not null
         order by coalesce(c.posted_at, c.approved_at, c.created_at) desc
         limit %s
        """,
        (list(statuses), limit),
    )
    return [str(row["id"]) for row in rows]

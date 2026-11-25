"""Load the candidate moments for one episode from the database (M20 L2).

Hard excludes live here, in SQL: removed clips, blocked or restricted creators (P6 "no compilations" sets
`restricted`), spoilers, safety flags, music rejects, non-English speech (episode subtitles are the local
transcript, never a paid translation), and clips used in an episode within `reuse_after_days`.
Ranking and variety are pure logic in `selector.py`.
"""

from datetime import datetime, timedelta
from typing import Any

from sixstars import db
from sixstars.longform.config import LongformConfig
from sixstars.longform.selector import Candidate

POOL_SQL = """
select c.id::text as clip_id,
       c.creator_id::text as creator_id,
       coalesce(cr.title_name, cr.display_name, cr.handle) as creator_name,
       coalesce(c.title, c.hook, '') as title,
       c.score,
       c.end_s - c.start_s as clip_s,
       s.window_end_s - s.window_start_s as window_s,
       s.score_z,
       (select max(p.views) from posts p where p.clip_id = c.id) as views,
       c.topic,
       c.created_at
  from clips c
  join creators cr on cr.id = c.creator_id
  left join spikes s on s.id = c.spike_id
 where c.created_at >= %(since)s
   and c.score >= %(min_score)s
   and (c.status = any(%(statuses)s)
        or (c.status = 'rejected' and c.reject_reason = any(%(reasons)s)))
   and cr.tier not in ('restricted', 'blocked')
   and cr.blocked_at is null
   and coalesce(c.spoiler, false) = false
   and coalesce(c.original_language, 'en') = 'en'
   and not (jsonb_typeof(c.safety_flags) = 'object'
            and exists (select 1 from jsonb_each(c.safety_flags) f where f.value = 'true'::jsonb))
   and not exists (
         select 1 from episode_items ei
           join episodes e on e.id = ei.episode_id
          where ei.clip_id = c.id
            and e.status not in ('skipped', 'failed')
            and e.episode_date >= %(reuse_since)s::date)
"""


def to_candidate(row: dict[str, Any]) -> Candidate:
    """A pool row as a `Candidate` (numeric columns arrive as Decimal)."""
    return Candidate(
        clip_id=str(row["clip_id"]),
        creator_id=str(row["creator_id"]),
        creator_name=str(row["creator_name"] or ""),
        title=str(row["title"] or ""),
        score=float(row["score"]),
        clip_s=float(row["clip_s"]),
        window_s=None if row["window_s"] is None else float(row["window_s"]),
        score_z=None if row["score_z"] is None else float(row["score_z"]),
        views=None if row["views"] is None else int(row["views"]),
        topic=row["topic"],
        created_at=row["created_at"],
    )


def load_candidates(cfg: LongformConfig, now: datetime, pool_hours: float | None = None) -> list[Candidate]:
    """Every clip that may be a moment of today's episode (`pool_hours` overrides the mix-day window)."""
    hours = cfg.pool_hours if pool_hours is None else pool_hours
    rows = db.fetch_all(
        POOL_SQL,
        {
            "since": now - timedelta(hours=hours),
            "min_score": cfg.min_score,
            "statuses": list(cfg.pool_statuses),
            "reasons": list(cfg.pool_reject_reasons),
            "reuse_since": (now - timedelta(days=cfg.reuse_after_days)).date(),
        },
    )
    return [to_candidate(dict(r)) for r in rows]

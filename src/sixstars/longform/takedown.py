"""Takedowns for long-form episodes (M20 L6). An episode follows every creator in it: when the guardian acts
on a creator (`guardian.enforce.act_on_creator`), each episode with one of their moments is taken down.

- Posted (public or private): the YouTube video is unpublished through Upload-Post (`delete_post`, YouTube
  is supported) and the episode becomes `removed`. If the call fails, the owner gets the manual step.
- Not posted yet (planned .. ready, publishing): the episode becomes `removed` so it is never published.
Never raises into the guardian: a failure here is reported, the creator action goes on.
"""

from dataclasses import dataclass
from typing import Literal

from sixstars import db, events, notify
from sixstars.clients.upload_post import UploadPostClient
from sixstars.logging import get_logger

log = get_logger(__name__)

EPISODES_SQL = """
select distinct e.id::text as id, e.episode_date, e.status, e.platform_post_id, e.post_url,
       a.upload_post_user
  from episodes e
  join episode_items ei on ei.episode_id = e.id and ei.status <> 'dropped'
  left join platform_accounts a on a.id = e.account_id
 where ei.creator_id = %s
   and e.status in ('planned', 'fetching', 'rendering', 'assembling', 'ready', 'publishing',
                    'posted', 'posted_private')
"""


@dataclass(frozen=True)
class EpisodeRemoval:
    episode_id: str
    was_posted: bool
    done: bool
    note: str = ""


def remove_episodes_of_creator(
    creator_id: str, reason: str, upload_post: UploadPostClient
) -> list[EpisodeRemoval]:
    """Take down every live or upcoming episode with a moment of `creator_id`."""
    out: list[EpisodeRemoval] = []
    try:
        rows = db.fetch_all(EPISODES_SQL, (creator_id,))
    except Exception as exc:  # noqa: BLE001 - never break the creator action
        log.warning("episode_takedown_lookup_failed", creator_id=creator_id, error=str(exc)[:200])
        return out
    for row in rows:
        episode_id = str(row["id"])
        posted = row["status"] in ("posted", "posted_private") and bool(row["platform_post_id"])
        done, note = True, ""
        if posted:
            try:
                upload_post.delete_post(str(row["upload_post_user"]), "youtube", str(row["platform_post_id"]))
            except Exception as exc:  # noqa: BLE001 - reported to the owner below
                done, note = False, str(exc)[:300]
        if done:
            db.execute(
                "update episodes set status = 'removed', error = %s where id = %s",
                (f"takedown: {reason}"[:500], episode_id),
            )
        level: Literal["critical", "warn"] = "critical" if posted else "warn"
        message = "unpublished" if posted and done else "manual removal needed" if posted else "cancelled"
        events.emit(
            "episode.removed" if done else "episode.remove_failed",
            level,
            "episode",
            episode_id,
            f"takedown of a creator in the episode: {message}",
            {"creator_id": creator_id, "reason": reason[:300], "note": note},
        )
        body = f"Reason: {reason[:300]}"
        if posted and not done:
            body += (
                f"\nDelete it by hand in YouTube Studio: {row['post_url'] or row['platform_post_id']}\n{note}"
            )
        notify.send(level, f"Episode {row['episode_date']} {message} (creator takedown)", body)
        out.append(EpisodeRemoval(episode_id, posted, done, note))
    return out

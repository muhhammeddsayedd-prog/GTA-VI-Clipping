"""What the guardian does to a creator after a claim, a platform removal or a takedown request:
block them for good, drop their clips that were not posted yet, and take down what was posted.

The same steps are run again for every blocked creator on each claims check (`sweep_blocked`), which
catches a post that was in flight when the block happened and retries YouTube removals that failed.
"""

from dataclasses import dataclass, field
from typing import Any

from sixstars import db, events
from sixstars.clients.upload_post import UploadPostClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.discovery import store as discovery_store
from sixstars.discovery import tiers
from sixstars.guardian import removal
from sixstars.guardian.removal import Removal
from sixstars.logging import get_logger

SERVICE = "guardian"
MAX_REASON_CHARS = 300
ACTIONED_EVENT = "guardian.creator_actioned"


@dataclass(slots=True)
class CreatorActions:
    creator_id: str
    handle: str
    platform: str
    newly_blocked: bool = False
    clips_dropped: int = 0
    posts_cancelled: int = 0
    removals: list[Removal] = field(default_factory=list)
    episodes_removed: int = 0  # M20: long-form episodes taken down or cancelled

    @property
    def manual(self) -> list[Removal]:
        return [item for item in self.removals if not item.done]

    @property
    def complete(self) -> bool:
        return not self.manual

    def as_actions(self) -> list[dict[str, Any]]:
        head: dict[str, Any] = {
            "action": "block_creator",
            "creator_id": self.creator_id,
            "creator": self.handle,
            "platform": self.platform,
            "newly_blocked": self.newly_blocked,
            "clips_dropped": self.clips_dropped,
            "pending_posts_cancelled": self.posts_cancelled,
        }
        return [head, *(item.as_action() for item in self.removals)]

    def summary_lines(self) -> list[str]:
        removed = sum(1 for item in self.removals if item.done)
        lines = [
            f"Creator {self.handle} ({self.platform}) is blocked"
            + ("" if self.newly_blocked else " (was blocked already)")
            + f"; {self.clips_dropped} waiting clip(s) dropped, "
            + f"{self.posts_cancelled} pending post(s) cancelled.",
            f"Posts removed automatically: {removed}. Posts needing your hands: {len(self.manual)}.",
        ]
        return lines


def drop_unposted_work(creator_id: str, reason: str) -> tuple[int, int]:
    """Clips of the creator that are not posted yet can never be posted; pending posts are cancelled.
    Returns (clips dropped, pending posts cancelled)."""
    clip_rows = db.fetch_all(
        """
        update clips set status = 'removed', reject_reason = coalesce(reject_reason, %s)
        where creator_id = %s and status in ('rendering', 'gating', 'precheck_pending', 'approved')
        returning id::text as id
        """,
        (reason[:MAX_REASON_CHARS], creator_id),
    )
    for row in clip_rows:
        events.emit(
            "clip.removed",
            level="warn",
            entity_type="clip",
            entity_id=row["id"],
            message=f"clip dropped before posting: {reason}"[:500],
            data={"reason": reason, "creator_id": creator_id},
            service=SERVICE,
        )
    post_rows = db.fetch_all(
        """
        update posts p set status = 'skipped', error = %s
        from clips c
        where c.id = p.clip_id and c.creator_id = %s and p.status = 'pending' and not p.dry_run
        returning p.id::text as id
        """,
        (f"cancelled by the guardian: {reason}"[:MAX_REASON_CHARS], creator_id),
    )
    for row in post_rows:
        events.emit(
            "post.cancelled",
            level="warn",
            entity_type="post",
            entity_id=row["id"],
            message=f"pending post cancelled: {reason}"[:500],
            data={"reason": reason, "creator_id": creator_id},
            service=SERVICE,
        )
    return len(clip_rows), len(post_rows)


def settle_clips(creator_id: str) -> None:
    """A posted clip with no post left in public is `removed`."""
    db.execute(
        """
        update clips c set status = 'removed'
        where c.creator_id = %s and c.status = 'posted'
          and not exists (
            select 1 from posts p
            where p.clip_id = c.id and not p.dry_run and p.status in ('published', 'pending', 'remove_failed')
          )
        """,
        (creator_id,),
    )


def act_on_creator(
    creator_id: str,
    reason: str,
    upload_post: UploadPostClient,
    youtube: YouTubeClient,
    *,
    incident_id: str | None = None,
) -> CreatorActions | None:
    """Block the creator, drop their waiting work and take down their posts. `incident_id` (a takedown
    row) makes the claim counter go up once per incident even when the incident is processed twice."""
    rows = discovery_store.creators_by_id([creator_id])
    row = rows.get(creator_id)
    if row is None:
        return None
    creator = discovery_store.creator_from_row(row)
    actions = CreatorActions(creator.id, creator.handle, creator.platform)
    counted = incident_id is not None and db.fetch_one(
        "select 1 as hit from events "
        "where type = %s and entity_id = %s and data->>'incident_id' = %s limit 1",
        (ACTIONED_EVENT, creator.id, incident_id),
    )
    if not counted:
        db.execute("update creators set claims_count = claims_count + 1 where id = %s", (creator.id,))
        events.emit(
            ACTIONED_EVENT,
            level="critical",
            entity_type="creator",
            entity_id=creator.id,
            message=f"guardian acts on {creator.platform} {creator.handle}: {reason}"[:500],
            data={"incident_id": incident_id, "reason": reason},
            service=SERVICE,
        )
    if creator.tier != "blocked":
        tiers.block(creator, reason[:MAX_REASON_CHARS])
        actions.newly_blocked = True
    actions.clips_dropped, actions.posts_cancelled = drop_unposted_work(creator.id, reason)
    for post in removal.live_posts_of_creator(creator.id):
        actions.removals.append(removal.remove_post(post, upload_post, youtube, reason))
    settle_clips(creator.id)
    from sixstars.longform.takedown import remove_episodes_of_creator

    actions.episodes_removed = len(remove_episodes_of_creator(creator.id, reason, upload_post))
    get_logger(SERVICE).warning(
        "creator_actioned",
        creator_id=creator.id,
        newly_blocked=actions.newly_blocked,
        clips_dropped=actions.clips_dropped,
        removed=sum(1 for item in actions.removals if item.done),
        manual=len(actions.manual),
    )
    return actions


def sweep_blocked(upload_post: UploadPostClient, youtube: YouTubeClient) -> list[Removal]:
    """Blocked creators must have nothing waiting and nothing public. Returns the removals tried for
    posts that were still `published` (new work for the owner if they need hands); YouTube posts that
    failed before are retried quietly."""
    rows = db.fetch_all(
        """
        select distinct cr.id::text as id
        from creators cr
        join clips c on c.creator_id = cr.id
        left join posts p on p.clip_id = c.id and not p.dry_run
        where cr.tier = 'blocked'
          and (c.status in ('rendering', 'gating', 'precheck_pending', 'approved')
               or p.status in ('published', 'pending')
               or (p.status = 'remove_failed' and p.platform = 'youtube'))
        """
    )
    fresh: list[Removal] = []
    for row in rows:
        creator_id = str(row["id"])
        reason = "creator is blocked"
        drop_unposted_work(creator_id, reason)
        for post in removal.live_posts_of_creator(creator_id):
            if post.status == "remove_failed" and post.platform != "youtube":
                continue  # waits for the owner; the claims check notices when it is gone
            result = removal.remove_post(post, upload_post, youtube, reason)
            if post.status == "published":
                fresh.append(result)
        settle_clips(creator_id)
    return fresh

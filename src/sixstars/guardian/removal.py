"""Taking posts down (docs/12 section 3).

- YouTube: `videos.delete` with the owner's OAuth token when one is configured; without it, or when that
  call fails, Upload-Post's unpublish endpoint.
- TikTok, Instagram: Upload-Post cannot delete these (`NotSupported`). The post is marked `remove_failed`
  and the owner gets a critical message with the link and the steps.

A post that could not be removed stays `remove_failed`. The claims check keeps reading it and marks it
`removed` once it is gone from the platform (the owner deleted it), and YouTube posts are tried again on
every pass. Dry-run rows are never touched: nothing of theirs is public.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from sixstars import db, events
from sixstars.clients.upload_post import NotSupported, UploadPostClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.logging import get_logger

SERVICE = "guardian"
MAX_REASON_CHARS = 300
MANUAL_POSTS_PER_MESSAGE = 12

Outcome = Literal["removed", "already_gone", "manual", "failed"]

POST_SQL = """
select p.id::text as id, p.clip_id::text as clip_id, c.creator_id::text as creator_id,
       cr.handle as creator_handle, p.account_id, p.platform, a.upload_post_user, a.handle as account_handle,
       a.privacy, p.platform_post_id, p.upload_request_id, p.post_url, p.status, p.published_at,
       p.claim_status, c.title
from posts p
join clips c on c.id = p.clip_id
join creators cr on cr.id = c.creator_id
join platform_accounts a on a.id = p.account_id
"""

MANUAL_STEPS: Mapping[str, str] = {
    "tiktok": "open the link in the TikTok app, signed in as {handle} > tap the three dots (or the "
    "Share arrow) > Delete > confirm",
    "instagram": "open the link in the Instagram app, signed in as {handle} > tap the three dots at "
    "the top right > Delete > confirm",
    "youtube": "open studio.youtube.com signed in as {handle} > Content > Shorts > find the video > "
    "three dots > Delete forever",
}


@dataclass(frozen=True, slots=True)
class PostRef:
    """A real (not dry-run) post with what is needed to check it and to take it down."""

    id: str
    clip_id: str
    creator_id: str
    creator_handle: str
    account_id: str
    platform: str
    upload_post_user: str
    account_handle: str
    privacy: str | None
    platform_post_id: str | None
    upload_request_id: str | None
    post_url: str | None
    status: str
    published_at: datetime | None = None
    claim_status: str | None = None
    title: str | None = None

    @property
    def is_public(self) -> bool:
        """True unless the account posts privately (staging: `private`, `SELF_ONLY`)."""
        return (self.privacy or "public").lower() in ("public", "public_to_everyone")


@dataclass(frozen=True, slots=True)
class Removal:
    post: PostRef
    outcome: Outcome
    method: str
    detail: str = ""

    @property
    def done(self) -> bool:
        return self.outcome in ("removed", "already_gone")

    def as_action(self) -> dict[str, Any]:
        return {
            "action": "remove_post",
            "post_id": self.post.id,
            "platform": self.post.platform,
            "account_id": self.post.account_id,
            "outcome": self.outcome,
            "method": self.method,
            "post_url": self.post.post_url,
            "detail": self.detail,
        }


def post_from_row(row: Mapping[str, Any]) -> PostRef:
    return PostRef(**{name: row[name] for name in PostRef.__slots__})


def youtube_video_id(post: PostRef) -> str | None:
    """The YouTube video id of a post: the stored platform id, else read from the stored link."""
    return post.platform_post_id or youtube_id_from_url(post.post_url)


def youtube_id_from_url(url: str | None) -> str | None:
    """The video id in a YouTube link (`watch?v=`, `youtu.be/`, `/shorts/`), or None."""
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    segments = [segment for segment in parts.path.split("/") if segment]
    if host == "youtu.be" and segments:
        return segments[0]
    if host == "youtube.com":
        if len(segments) >= 2 and segments[0] in ("shorts", "embed", "live", "v"):
            return segments[1]
        return next(iter(parse_qs(parts.query).get("v") or []), None)
    return None


def _short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_REASON_CHARS]


def delete_on_platform(post: PostRef, upload_post: UploadPostClient, youtube: YouTubeClient) -> Removal:
    """Ask the platform to delete one post. Touches no database row."""
    log = get_logger(SERVICE)
    post_id = youtube_video_id(post) if post.platform == "youtube" else post.platform_post_id
    oauth_error = ""
    if post.platform == "youtube" and post_id and youtube.has_oauth():
        try:
            existed = youtube.delete_video(post_id)
            return Removal(post, "removed" if existed else "already_gone", "youtube_oauth")
        except Exception as exc:  # the token may be revoked: fall through to Upload-Post
            oauth_error = _short(exc)
            log.warning("youtube_oauth_delete_failed", post_id=post.id, error=type(exc).__name__)
    if not post_id:
        return Removal(post, "manual", "none", "the platform id of the post is unknown")
    try:
        existed = upload_post.delete_post(post.upload_post_user, post.platform, post_id)
    except NotSupported:
        return Removal(post, "manual", "upload_post", f"Upload-Post cannot delete {post.platform} posts")
    except Exception as exc:
        log.error("upload_post_delete_failed", post_id=post.id, error=type(exc).__name__)
        detail = _short(exc) + (f" (YouTube OAuth: {oauth_error})" if oauth_error else "")
        return Removal(post, "failed", "upload_post", detail)
    return Removal(post, "removed" if existed else "already_gone", "upload_post")


def record(removal: Removal, reason: str) -> None:
    """Write the outcome of a removal to `posts` and `events`."""
    post = removal.post
    if removal.done:
        mark_removed(post, reason, removal.method)
        return
    changed = db.execute(
        """
        update posts set status = 'remove_failed', remove_reason = %s
        where id = %s and status in ('published', 'remove_failed')
        """,
        (reason[:MAX_REASON_CHARS], post.id),
    )
    if changed and post.status != "remove_failed":
        events.emit(
            "post.remove_failed",
            level="critical",
            entity_type="post",
            entity_id=post.id,
            message=f"{post.platform} post needs manual removal: {removal.detail}"[:500],
            data={**removal.as_action(), "reason": reason, "clip_id": post.clip_id},
            service=SERVICE,
        )


def mark_removed(post: PostRef, reason: str, method: str) -> bool:
    """The post is gone from the platform: `removed`. Returns False when it already was."""
    changed = db.execute(
        """
        update posts set status = 'removed', removed_at = now(), remove_reason = coalesce(remove_reason, %s)
        where id = %s and status in ('published', 'remove_failed')
        """,
        (reason[:MAX_REASON_CHARS], post.id),
    )
    if changed:
        events.emit(
            "post.removed",
            level="warn",
            entity_type="post",
            entity_id=post.id,
            message=f"{post.platform} post removed ({method}): {reason}"[:500],
            data={
                "method": method,
                "reason": reason,
                "clip_id": post.clip_id,
                "account_id": post.account_id,
                "post_url": post.post_url,
            },
            service=SERVICE,
        )
    return bool(changed)


def remove_post(post: PostRef, upload_post: UploadPostClient, youtube: YouTubeClient, reason: str) -> Removal:
    """Delete one post on its platform and record the outcome."""
    removal = delete_on_platform(post, upload_post, youtube)
    record(removal, reason)
    return removal


def live_posts_of_creator(creator_id: str) -> list[PostRef]:
    """The creator's real posts that are (or may still be) public."""
    rows = db.fetch_all(
        POST_SQL + " where c.creator_id = %s and not p.dry_run and p.status in ('published', 'remove_failed')"
        " order by p.published_at",
        (creator_id,),
    )
    return [post_from_row(row) for row in rows]


def manual_steps(post: PostRef) -> str:
    template = MANUAL_STEPS.get(post.platform, "delete the post by hand on {handle}")
    return template.format(handle=post.account_handle)


def manual_removal_lines(removals: Sequence[Removal]) -> list[str]:
    """The numbered list of posts the owner must delete by hand, with the link and the steps."""
    lines: list[str] = []
    for index, removal in enumerate(removals, start=1):
        post = removal.post
        link = post.post_url or f"(no link stored; platform id {post.platform_post_id or 'unknown'})"
        lines.append(f"{index}. {post.platform} {post.account_handle}: {link}")
        lines.append(f"   How: {manual_steps(post)}.")
    return lines


def manual_removal_messages(removals: Sequence[Removal], why: str) -> list[tuple[str, str]]:
    """Title and body of the critical message(s) for posts that need the owner's hands. Plain text: the
    n8n relay (WF07) escapes it for Telegram and cuts a body at 3,500 characters, so long lists are
    split over several messages."""
    manual = [removal for removal in removals if not removal.done]
    messages: list[tuple[str, str]] = []
    for start in range(0, len(manual), MANUAL_POSTS_PER_MESSAGE):
        chunk = manual[start : start + MANUAL_POSTS_PER_MESSAGE]
        part = f" ({start // MANUAL_POSTS_PER_MESSAGE + 1})" if len(manual) > MANUAL_POSTS_PER_MESSAGE else ""
        body = "\n".join(
            [
                f"Why: {why}",
                "These posts are still public. The system cannot delete them (Upload-Post has no delete "
                "for TikTok and Instagram), so please delete each one by hand:",
                *manual_removal_lines(chunk),
                "Nothing else is needed: the creator is already blocked, and the guardian marks each "
                "post as removed once it is gone. Until then it stays in the daily report.",
            ]
        )
        messages.append((f"ACTION NEEDED: delete {len(chunk)} post(s) by hand{part}", body))
    return messages

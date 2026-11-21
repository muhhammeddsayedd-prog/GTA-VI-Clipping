"""Publishes one `posts` row through the Upload-Post client and records the result.

Safety rules, in the order they are applied:
1. Only a `pending`, non-dry row is ever sent. Dry-run rows and finished rows are left alone.
2. Before the first send the gate (`check_gate`) is run again with fresh settings, the clip must still be
   approved, an hourly post must still be inside its slot and a spike must not be too old. A row that fails
   is cancelled (`skipped`) and nothing is sent.
3. Every send of one post uses the same idempotency key and request id (`post:<clip_id>:<account_id>`).
   After an attempt with an unknown outcome (timeout, network error, 5xx) the status is read first, and the
   upload is sent again only when Upload-Post does not know the request; the key makes that harmless.
4. The per-platform `success` flag decides the outcome, not the HTTP status.
5. A rate limit pauses the account and may lower its cap (`on_rate_limit`); an authorisation error pauses the
   account and raises a critical alert.

`publish` never raises for a posting problem: it returns a `PublishResult` and leaves the row `published`,
`failed`, `skipped` (cancelled) or `pending` with a `post.retry` job queued.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from sixstars.clients._http import NotConfiguredError
from sixstars.clients.upload_post import (
    AccountAuthError,
    PlatformResult,
    PostingBlocked,
    PostRejected,
    RateLimited,
    UploadPostClient,
    UploadStatus,
)
from sixstars.logging import get_logger
from sixstars.posting.caps import on_rate_limit
from sixstars.posting.captions import UploadRequest, build_upload_request
from sixstars.posting.gate import check_gate
from sixstars.posting.models import CLIP_APPROVED, CLIP_POSTED, Account, Lane, Platform
from sixstars.posting.params import PostingParams
from sixstars.posting.slots import slot_is_open
from sixstars.posting.store import LoadedClip, PostRow, PublisherStore, utcnow

# docs/11 §8 and M10 fix these numbers; they have no settings key in docs/16.
INLINE_WAIT_S = 30.0  # how long one call waits for the upload status before handing over to `post.retry`
RETRY_BACKOFF_S = (30, 120, 300)  # delays before the 3 retries of a send with an unknown outcome
POLL_AGAIN_S = 30  # delay before the status of an accepted upload is read again
PENDING_DEADLINE = timedelta(minutes=20)  # 3 retries (450 s) plus 10 minutes of status polling
RECOVERY_DELAY_S = 600  # safety job queued with every planned post, in case its worker dies
LINK_REFRESH_S = 300  # a published post without a URL (TikTok) is read again after this long
LINK_REFRESH_WINDOW = timedelta(minutes=20)
AUTH_PAUSE = timedelta(hours=24)  # pause after an authorisation error; a human has to reconnect
MAX_ERROR_CHARS = 500
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


class Outcome(StrEnum):
    PUBLISHED = "published"
    FAILED = "failed"
    PENDING = "pending"  # not final yet: a `post.retry` job is queued
    CANCELLED = "cancelled"  # nothing was sent; the row is `skipped`
    NOOP = "noop"  # nothing to do for this row


@dataclass(frozen=True, slots=True)
class PublishResult:
    post_id: str
    outcome: Outcome
    detail: str = ""
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Work:
    """Everything loaded for one publish call."""

    post: PostRow
    account: Account
    loaded: LoadedClip
    params: PostingParams
    now: datetime


def _short(text: str) -> str:
    return text[:MAX_ERROR_CHARS]


def post_link(platform: Platform, result: PlatformResult) -> str | None:
    """The link to store for a published post.

    For a private YouTube upload Upload-Post answers with a note instead of a URL ("Post uploaded as
    Private. No public URL available.", seen on the first real upload), and it never sends one later. The
    video id is there, so the watch link is built from it: the owner can open it, and the guardian and the
    report have a link. Other platforms keep what Upload-Post gave (TikTok sends the URL later).
    """
    if result.post_url is not None:
        return result.post_url
    if platform is Platform.YOUTUBE and result.post_id and YOUTUBE_ID.fullmatch(result.post_id):
        return YOUTUBE_WATCH_URL.format(video_id=result.post_id)
    return None


def _key_in_use(exc: PostRejected) -> bool:
    """HTTP 409 `IDEMPOTENCY_KEY_IN_USE`: the same upload is being processed right now."""
    text = str(exc).lower()
    return "http 409" in text or "idempotency" in text


class Publisher:
    """Sends pending posts. `client` is the Upload-Post client (a fake in every test)."""

    def __init__(
        self,
        client: UploadPostClient,
        store: PublisherStore,
        clock: Callable[[], datetime] = utcnow,
        wait_s: float = INLINE_WAIT_S,
    ) -> None:
        self._client = client
        self._store = store
        self._clock = clock
        self._wait_s = wait_s
        self._log = get_logger("posting")

    # ── entry points ─────────────────────────────────────────

    def publish_all(self, post_ids: Iterable[str]) -> tuple[PublishResult, ...]:
        """Publish several planned posts. An unexpected error on one post (for example the database) is
        logged and the others still go out; the failed one is picked up by its recovery `post.retry` job."""
        results: list[PublishResult] = []
        for post_id in post_ids:
            try:
                results.append(self.publish(post_id))
            except Exception as exc:
                self._log.error(
                    "post_publish_crashed", post_id=post_id, error=type(exc).__name__, exc_info=True
                )
                results.append(PublishResult(post_id, Outcome.PENDING, f"error: {type(exc).__name__}"))
        return tuple(results)

    def publish(self, post_id: str) -> PublishResult:
        """Send, resume or finish the post `post_id`. Safe to call again at any time."""
        post = self._store.get_post(post_id)
        if post is None:
            return PublishResult(post_id, Outcome.NOOP, "no such post")
        if post.dry_run:
            return PublishResult(post_id, Outcome.NOOP, "dry-run row: nothing is ever sent")
        if post.status == "published":
            return self._refresh_link(post)
        if post.status != "pending":
            return PublishResult(post_id, Outcome.NOOP, f"post is {post.status}")
        now = self._clock()
        account = self._store.get_account(post.account_id)
        loaded = self._store.get_clip(post.clip_id)
        if account is None or loaded is None:
            return self._fail(post, "the account or the clip of this post no longer exists")
        try:
            params = self._store.load_params()
        except (KeyError, TypeError, ValueError) as exc:
            return self._cancel_or_fail(post, f"posting settings are unusable: {exc}")
        work = _Work(post, account, loaded, params, now)
        if post.attempts > 0:
            return self._resume(work)
        refusal = self._refusal(work)
        if refusal is not None:
            return self._cancel(post, refusal)
        return self._send(work)

    # ── sending ──────────────────────────────────────────────

    def _refusal(self, work: _Work) -> str | None:
        """Why this post must not be sent now, or None. Runs with fresh settings right before a send."""
        post, clip = work.post, work.loaded.clip
        gate = check_gate(work.account, post.lane, work.now, work.params)
        if not gate.may_send:
            return f"{gate.code.value}: {gate.reason}"
        sendable = (
            {CLIP_APPROVED, CLIP_POSTED}
            if post.lane is Lane.HOURLY and work.params.requeues_posted
            else {CLIP_APPROVED}
        )
        if clip.status not in sendable:
            return f"clip_not_approved: clip {clip.id} is {clip.status!r}"
        if work.now - post.created_at > PENDING_DEADLINE:
            return "too_late: the post was planned too long ago"
        if post.lane is Lane.HOURLY and (post.slot_at is None or not slot_is_open(post.slot_at, work.now)):
            return "slot_expired: the hourly slot of this post is over"
        if post.lane is Lane.SPIKE and clip.spike_at is not None:
            age_s = (work.now - clip.spike_at).total_seconds()
            if age_s > work.params.spike_max_age_s:
                return f"spike_too_old: the spike is {int(age_s)} s old"
        return None

    def _send(self, work: _Work) -> PublishResult:
        post = work.post
        render_id, local = self._store.file_for_post(post.clip_id, post.account_id, work.loaded.local_path)
        path = Path(local) if local else None
        if path is None or not path.is_file():
            return self._fail(post, "video file is missing on this server")
        try:
            request = build_upload_request(work.loaded.clip, work.account, work.params)
        except ValueError as exc:  # includes UnsafePostError
            return self._fail(post, f"upload fields refused: {exc}")
        self._store.mark_sending(post.id, request.request_id, render_id)
        self._log.info("post_sending", clip_id=post.clip_id, post_id=post.id, account_id=post.account_id)
        try:
            status = self._client.upload_video(
                request.user,
                request.platform,
                path,
                request.fields,
                request.idempotency_key,
                request.request_id,
            )
        except PostingBlocked as exc:
            return self._cancel_or_fail(post, f"posting_blocked: {exc}")
        except RateLimited as exc:
            return self._rate_limited(work, str(exc))
        except AccountAuthError as exc:
            return self._auth_failed(work, str(exc))
        except NotConfiguredError as exc:
            self._store.notify("critical", "Upload-Post is not configured", _short(str(exc)))
            return self._fail(post, f"not_configured: {exc}", alert=False)
        except PostRejected as exc:
            if _key_in_use(exc):
                return self._poll_later(work, "the upload is already being processed")
            return self._fail(post, f"rejected: {exc}")
        except Exception as exc:
            return self._retry_later(work, f"{type(exc).__name__}: {exc}")
        return self._after_upload(work, request, status)

    def _after_upload(self, work: _Work, request: UploadRequest, status: UploadStatus) -> PublishResult:
        # Sending the file takes a minute or more (75 to 80 s for 24 MB on the first real uploads), so the
        # time read before it is stale: everything after the upload is timed from now.
        work = replace(work, now=self._clock())
        if not status.finished:
            try:
                status = self._client.wait_for_upload(request.request_id, timeout_s=self._wait_s)
            except Exception as exc:
                return self._poll_later(work, f"status not readable: {type(exc).__name__}")
        return self._resolve(work, status)

    def _resume(self, work: _Work) -> PublishResult:
        """An upload was attempted before. Read its status; send again only when it is unknown there."""
        post = work.post
        request_id = post.upload_request_id or post.idempotency_key
        try:
            status = self._client.get_upload_status(request_id)
        except Exception as exc:
            return self._poll_later(work, f"status not readable: {type(exc).__name__}")
        if status.status != "not_found":
            return self._resolve(work, status)
        refusal = self._refusal(work)
        if refusal is not None:
            return self._fail(post, f"not sent again ({refusal}); the earlier attempt has no known result")
        if post.attempts > len(RETRY_BACKOFF_S):
            return self._fail(post, f"gave up after {post.attempts} attempts without a result")
        return self._send(work)

    # ── reading the result ───────────────────────────────────

    def _resolve(self, work: _Work, status: UploadStatus) -> PublishResult:
        post = work.post
        if not status.finished or status.status == "not_found":
            return self._poll_later(work, f"upload status is {status.status}")
        result = status.result_for(post.platform.value)
        if result is None:
            return self._fail(post, f"no result for {post.platform.value}: {status.message or status.status}")
        if result.skipped:
            return self._auth_failed(work, f"{post.platform.value} is not connected to the profile")
        if result.success:
            if result.raw.get("fallback_to_inbox"):
                return self._fail(
                    post, "tiktok_inbox: the video went to the TikTok drafts inbox, it is not public"
                )
            return self._published(work, result)
        message = f"{result.error_code or 'error'}: {result.error or 'no message'}"
        if result.error_kind == "rate_limited":
            return self._rate_limited(work, message)
        if result.error_kind == "auth":
            return self._auth_failed(work, message)
        return self._fail(post, f"rejected: {message}")

    def _published(self, work: _Work, result: PlatformResult) -> PublishResult:
        post = work.post
        now = work.now
        post_url = post_link(post.platform, result)
        if not self._store.mark_published(post.id, result.post_id, post_url, now):
            return PublishResult(post.id, Outcome.NOOP, "the post was already final")
        self._store.emit(
            "post.published",
            entity_type="post",
            entity_id=post.id,
            message=f"{post.lane.value} post published on {post.account_id}",
            data={
                "clip_id": post.clip_id,
                "account_id": post.account_id,
                "lane": post.lane.value,
                "platform_post_id": result.post_id,
                "post_url": post_url,
            },
        )
        self._log.info("post_published", clip_id=post.clip_id, post_id=post.id, account_id=post.account_id)
        if post.lane is Lane.SPIKE:
            self._store.record_spike_to_post(post.clip_id, now)
        self._store.settle_clip(post.clip_id, now)
        if post_url is None:
            self._store.schedule_publish_retry(
                post.id, post.clip_id, post.lane, now + timedelta(seconds=LINK_REFRESH_S)
            )
        return PublishResult(post.id, Outcome.PUBLISHED, post_url or "published, URL not known yet")

    def _refresh_link(self, post: PostRow) -> PublishResult:
        """A published post without a URL: read the status once more and store the URL when it is there."""
        if post.post_url is not None or post.upload_request_id is None or post.published_at is None:
            return PublishResult(post.id, Outcome.NOOP, "post is published")
        now = self._clock()
        try:
            status = self._client.get_upload_status(post.upload_request_id)
        except Exception as exc:
            self._log.warning("post_link_refresh_failed", post_id=post.id, error=type(exc).__name__)
            return PublishResult(post.id, Outcome.NOOP, "status not readable")
        result = status.result_for(post.platform.value)
        if result is not None and result.success and result.post_url is not None:
            self._store.set_post_link(post.id, result.post_id, result.post_url)
            self._store.emit(
                "post.link_updated",
                entity_type="post",
                entity_id=post.id,
                data={"clip_id": post.clip_id, "post_url": result.post_url},
            )
            return PublishResult(post.id, Outcome.PUBLISHED, result.post_url)
        if now - post.published_at < LINK_REFRESH_WINDOW:
            self._store.schedule_publish_retry(
                post.id, post.clip_id, post.lane, now + timedelta(seconds=LINK_REFRESH_S)
            )
        return PublishResult(post.id, Outcome.NOOP, "URL still not known")

    # ── outcomes ─────────────────────────────────────────────

    def _cancel(self, post: PostRow, reason: str) -> PublishResult:
        """Nothing was sent: the row becomes `skipped` and stops counting."""
        if self._store.void_post(post.id, _short(reason)):
            self._store.emit(
                "post.cancelled",
                "warn",
                entity_type="post",
                entity_id=post.id,
                message=_short(reason),
                data={"clip_id": post.clip_id, "account_id": post.account_id, "lane": post.lane.value},
            )
            self._log.warning(
                "post_cancelled", clip_id=post.clip_id, post_id=post.id, account_id=post.account_id
            )
            self._store.settle_clip(post.clip_id, self._clock())
        return PublishResult(post.id, Outcome.CANCELLED, reason)

    def _cancel_or_fail(self, post: PostRow, reason: str) -> PublishResult:
        """Cancel a post that was never sent. After an earlier attempt the row stays as `failed`, because
        that attempt may have reached the platform."""
        if post.attempts == 0:
            return self._cancel(post, reason)
        return self._fail(post, reason)

    def _fail(self, post: PostRow, error: str, alert: bool = True) -> PublishResult:
        if self._store.mark_failed(post.id, _short(error)):
            self._store.emit(
                "post.failed",
                "error",
                entity_type="post",
                entity_id=post.id,
                message=_short(error),
                data={"clip_id": post.clip_id, "account_id": post.account_id, "lane": post.lane.value},
            )
            self._log.error("post_failed", clip_id=post.clip_id, post_id=post.id, account_id=post.account_id)
            if alert:
                self._store.notify("warn", f"Post failed on {post.account_id}", _short(error))
            self._store.settle_clip(post.clip_id, self._clock())
        return PublishResult(post.id, Outcome.FAILED, error)

    def _rate_limited(self, work: _Work, message: str) -> PublishResult:
        """The platform (or Upload-Post) reported a limit: pause the account and learn the cap."""
        account = work.account
        history = self._store.load_history(work.now)
        outcome = on_rate_limit(account, history, work.now, work.params)
        self._store.pause_account(
            account.id,
            outcome.paused_until,
            outcome.reason,
            outcome.new_cap_24h if outcome.cap_changed else None,
        )
        self._store.emit(
            "account.rate_limited",
            "warn",
            entity_type="account",
            entity_id=account.id,
            message=outcome.reason,
            data={
                "paused_until": outcome.paused_until.isoformat(),
                "cap_24h": outcome.new_cap_24h,
                "cap_changed": outcome.cap_changed,
                "clip_id": work.post.clip_id,
            },
        )
        self._store.notify("warn", f"Rate limit on {account.id}", outcome.reason)
        return self._fail(work.post, f"rate_limited: {message}", alert=False)

    def _auth_failed(self, work: _Work, message: str) -> PublishResult:
        """The account is not authorised: pause it and tell a human."""
        account = work.account
        until = work.now + AUTH_PAUSE
        reason = f"authorisation error on {account.id}: {_short(message)}"
        self._store.pause_account(account.id, until, reason)
        self._store.emit(
            "account.auth_failed",
            "critical",
            entity_type="account",
            entity_id=account.id,
            message=reason,
            data={"paused_until": until.isoformat(), "clip_id": work.post.clip_id},
        )
        self._store.notify(
            "critical",
            f"Reconnect {account.id} in Upload-Post",
            f"{reason}. The account is paused until {until.isoformat()}.",
        )
        return self._fail(work.post, f"auth: {message}", alert=False)

    def _retry_later(self, work: _Work, error: str) -> PublishResult:
        """The send has no known outcome: try again later with the same key, at most 3 times."""
        post = work.post
        sends = post.attempts + 1
        if sends > len(RETRY_BACKOFF_S):
            return self._fail(post, f"gave up after {sends} attempts: {error}")
        run_at = work.now + timedelta(seconds=RETRY_BACKOFF_S[sends - 1])
        return self._queue(work, run_at, "post.retry_scheduled", error)

    def _poll_later(self, work: _Work, detail: str) -> PublishResult:
        """The upload was accepted (or may have been) but has no final status yet."""
        if work.now - work.post.created_at > PENDING_DEADLINE:
            return self._fail(work.post, f"no final status after {PENDING_DEADLINE}: {detail}")
        return self._queue(work, work.now + timedelta(seconds=POLL_AGAIN_S), "post.status_pending", detail)

    def _queue(self, work: _Work, run_at: datetime, event: str, detail: str) -> PublishResult:
        post = work.post
        self._store.schedule_publish_retry(post.id, post.clip_id, post.lane, run_at)
        self._store.emit(
            event,
            "warn" if event == "post.retry_scheduled" else "info",
            entity_type="post",
            entity_id=post.id,
            message=_short(detail),
            data={"clip_id": post.clip_id, "account_id": post.account_id, "run_at": run_at.isoformat()},
        )
        self._log.info("post_retry_queued", clip_id=post.clip_id, post_id=post.id, kind=event)
        return PublishResult(post.id, Outcome.PENDING, detail, retry_at=run_at)

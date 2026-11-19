"""An in-memory `PublisherStore` for the publisher unit tests: the same contract as `DbStore`, in dicts."""

import itertools
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from sixstars.posting.captions import idempotency_key
from sixstars.posting.models import Account, Clip, Lane, PostRecord
from sixstars.posting.params import PostingParams
from sixstars.posting.store import LoadedClip, PostRow, void_key


class MemoryStore:
    def __init__(self, params: PostingParams) -> None:
        self.params = params
        self.posts: dict[str, PostRow] = {}
        self.accounts: dict[str, Account] = {}
        self.clips: dict[str, LoadedClip] = {}
        self.events: list[tuple[str, str, str | None]] = []  # (type, level, entity id)
        self.notifications: list[tuple[str, str, str]] = []
        self.retries: list[tuple[str, datetime]] = []
        self.pauses: list[tuple[str, datetime, int | None]] = []
        self.spike_to_post: list[str] = []
        self._ids = itertools.count(1)

    # ── test setup ───────────────────────────────────────────

    def add_post(
        self,
        clip: Clip,
        account: Account,
        now: datetime,
        *,
        video: str | None,
        slot_at: datetime | None = None,
        dry_run: bool = False,
    ) -> str:
        """A planned post, as the lanes create it."""
        self.accounts[account.id] = account
        self.clips[clip.id] = LoadedClip(clip, video, None)
        post_id = f"post-{next(self._ids)}"
        self.posts[post_id] = PostRow(
            id=post_id,
            clip_id=clip.id,
            account_id=account.id,
            platform=account.platform,
            lane=clip.lane,
            slot_at=slot_at,
            status="skipped" if dry_run else "pending",
            dry_run=dry_run,
            idempotency_key=f"dry:{clip.id}:{account.id}"
            if dry_run
            else idempotency_key(clip.id, account.id),
            upload_request_id=None,
            platform_post_id=None,
            post_url=None,
            error=None,
            attempts=0,
            created_at=now,
            published_at=None,
        )
        return post_id

    def event_types(self) -> list[str]:
        return [event[0] for event in self.events]

    # ── PublisherStore ───────────────────────────────────────

    def load_params(self) -> PostingParams:
        return self.params

    def get_post(self, post_id: str) -> PostRow | None:
        return self.posts.get(post_id)

    def get_account(self, account_id: str) -> Account | None:
        return self.accounts.get(account_id)

    def get_clip(self, clip_id: str) -> LoadedClip | None:
        return self.clips.get(clip_id)

    def load_history(self, now: datetime, clip_ids: Iterable[str] = ()) -> list[PostRecord]:
        return [
            PostRecord(
                account_id=p.account_id,
                platform=p.platform,
                clip_id=p.clip_id,
                lane=p.lane,
                status=p.status,
                created_at=p.created_at,
                published_at=p.published_at,
                dry_run=p.dry_run,
                creator_id=self.clips[p.clip_id].clip.creator_id,
                slot_at=p.slot_at,
            )
            for p in self.posts.values()
        ]

    def _update(self, post_id: str, only: str, **changes: Any) -> bool:
        post = self.posts[post_id]
        if post.status != only:
            return False
        self.posts[post_id] = replace(post, **changes)
        return True

    def file_for_post(
        self, clip_id: str, account_id: str, fallback: str | None
    ) -> tuple[str | None, str | None]:
        """Per-account renders (`factory.variants`) are not modelled here: always the clip's own file."""
        return None, fallback

    def mark_sending(self, post_id: str, request_id: str, render_id: str | None = None) -> None:
        attempts = self.posts[post_id].attempts + 1
        self._update(post_id, "pending", attempts=attempts, upload_request_id=request_id)

    def mark_published(
        self, post_id: str, platform_post_id: str | None, post_url: str | None, now: datetime
    ) -> bool:
        return self._update(
            post_id,
            "pending",
            status="published",
            platform_post_id=platform_post_id,
            post_url=post_url,
            published_at=now,
        )

    def set_post_link(self, post_id: str, platform_post_id: str | None, post_url: str | None) -> None:
        post = self.posts[post_id]
        self._update(
            post_id,
            "published",
            platform_post_id=platform_post_id or post.platform_post_id,
            post_url=post_url or post.post_url,
        )

    def mark_failed(self, post_id: str, error: str) -> bool:
        return self._update(post_id, "pending", status="failed", error=error)

    def void_post(self, post_id: str, reason: str) -> bool:
        return self._update(
            post_id, "pending", status="skipped", error=reason, idempotency_key=void_key(post_id)
        )

    def pause_account(
        self, account_id: str, until: datetime, reason: str, new_cap_24h: int | None = None
    ) -> None:
        account = self.accounts[account_id]
        cap = account.cap_24h if new_cap_24h is None else min(account.cap_24h, new_cap_24h)
        self.accounts[account_id] = replace(account, paused_until=until, cap_24h=cap)
        self.pauses.append((account_id, until, new_cap_24h))

    def settle_clip(self, clip_id: str, now: datetime, exclude_job_id: int | None = None) -> bool:
        rows = [p for p in self.posts.values() if p.clip_id == clip_id and not p.dry_run]
        loaded = self.clips[clip_id]
        done = any(p.status == "published" for p in rows) and not any(p.status == "pending" for p in rows)
        if not done or loaded.clip.status != "approved":
            return False
        self.clips[clip_id] = replace(loaded, clip=replace(loaded.clip, status="posted"))
        return True

    def record_spike_to_post(self, clip_id: str, now: datetime) -> None:
        self.spike_to_post.append(clip_id)

    def schedule_publish_retry(self, post_id: str, clip_id: str, lane: Lane, run_at: datetime) -> None:
        self.retries.append((post_id, run_at))

    def emit(
        self,
        type: str,  # noqa: A002
        level: str = "info",
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        message: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.events.append((type, level, entity_id))

    def notify(self, severity: str, title: str, body: str = "") -> None:
        self.notifications.append((severity, title, body))

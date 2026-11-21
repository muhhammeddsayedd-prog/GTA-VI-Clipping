"""Database side of the posting engine: loads `platform_accounts`, `posts`, `clips` and the posting settings
into the types of the pure rules, and writes every state change of a post. All SQL of the posting engine is
here and goes through `sixstars.db`.

The planning lock (`planning_lock`) is one transaction-level advisory lock shared by both lanes. A planner
takes it, reads the clock, loads the history, decides and inserts its `posts` rows before it commits, so two
workers can never both be given the last free post of an account.
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sixstars import db, events, jobs, notify
from sixstars.config import env
from sixstars.posting.caps import DAY
from sixstars.posting.models import Account, Clip, Lane, Platform, PostRecord, ensure_utc
from sixstars.posting.params import OPTIONAL_KEYS, REQUIRED_KEYS, PostingParams

SERVICE = "posting"
PLANNING_LOCK_NAME = "sixstars.posting.plan"
LOCK_TIMEOUT_S = 30  # a planner that cannot get the lock in this time fails (and posts nothing)
HISTORY_MARGIN = timedelta(hours=1)  # loaded on top of the 24 h the rules need
QUEUE_LIMIT = 200  # best-ranked approved hourly clips read per slot (a query bound, not a posting rule)
RETRY_JOB_TYPE = "post.retry"
RETRY_QUEUE: jobs.Queue = "express"
SPIKE_RETRY_PRIORITY = 90  # M10: `post.retry` on the express queue with priority 90
HOURLY_RETRY_PRIORITY = 60
SPIKE_RETRY_MARGIN = timedelta(seconds=1)  # a deferred spike is tried just after its gap has ended

_CLIP_SQL = """
select c.id::text as id, c.creator_id::text as creator_id, c.lane, c.rank_score, c.status, c.approved_at,
       s.spike_wall, cr.handle as creator_handle, c.source_platform, c.title, c.caption, c.hook,
       c.hashtags, c.local_path, c.spike_id::text as spike_id,
       coalesce(c.parent_clip_id, c.id)::text as family_id, c.duration_s
from clips c
join creators cr on cr.id = c.creator_id
left join spikes s on s.id = c.spike_id
"""
_ACCOUNT_SQL = """
select id, platform, upload_post_user, handle, lanes, cap_24h, spike_reserve, min_gap_s, privacy,
       hourly_hours_utc, enabled, paused_until, hourly_min_gap_s
from platform_accounts
"""
_POST_COLUMNS = """
p.id::text as id, p.clip_id::text as clip_id, p.account_id, p.platform, p.lane, p.slot_at, p.status,
p.dry_run, p.idempotency_key, p.upload_request_id, p.platform_post_id, p.post_url, p.error, p.attempts,
p.created_at, p.published_at
"""


def utcnow() -> datetime:
    """The current UTC time. Every decision reads the clock once and passes the value on."""
    return datetime.now(UTC)


def app_env() -> str:
    """`APP_ENV` as the rules know it. Anything that is not production gets the stricter staging rules."""
    return "production" if env().APP_ENV == "production" else "staging"


@dataclass(frozen=True, slots=True)
class LoadedClip:
    """A clip as the rules see it, plus the columns only the publisher needs."""

    clip: Clip
    local_path: str | None
    spike_id: str | None


@dataclass(frozen=True, slots=True)
class PostRow:
    """One row of `posts` as the publisher needs it."""

    id: str
    clip_id: str
    account_id: str
    platform: Platform
    lane: Lane
    slot_at: datetime | None
    status: str
    dry_run: bool
    idempotency_key: str
    upload_request_id: str | None
    platform_post_id: str | None
    post_url: str | None
    error: str | None
    attempts: int
    created_at: datetime
    published_at: datetime | None


class PublisherStore(Protocol):
    """What the publisher needs from storage. `DbStore` is the real one; unit tests use one in memory."""

    def load_params(self) -> PostingParams: ...
    def get_post(self, post_id: str) -> PostRow | None: ...
    def get_account(self, account_id: str) -> Account | None: ...
    def get_clip(self, clip_id: str) -> LoadedClip | None: ...
    def load_history(self, now: datetime, clip_ids: Iterable[str] = ()) -> list[PostRecord]: ...
    def file_for_post(
        self, clip_id: str, account_id: str, fallback: str | None
    ) -> tuple[str | None, str | None]: ...
    def mark_sending(self, post_id: str, request_id: str, render_id: str | None = None) -> None: ...
    def mark_published(
        self, post_id: str, platform_post_id: str | None, post_url: str | None, now: datetime
    ) -> bool: ...
    def set_post_link(self, post_id: str, platform_post_id: str | None, post_url: str | None) -> None: ...
    def mark_failed(self, post_id: str, error: str) -> bool: ...
    def void_post(self, post_id: str, reason: str) -> bool: ...
    def pause_account(
        self, account_id: str, until: datetime, reason: str, new_cap_24h: int | None = None
    ) -> None: ...
    def settle_clip(self, clip_id: str, now: datetime, exclude_job_id: int | None = None) -> bool: ...
    def record_spike_to_post(self, clip_id: str, now: datetime) -> None: ...
    def schedule_publish_retry(self, post_id: str, clip_id: str, lane: Lane, run_at: datetime) -> None: ...
    def emit(
        self,
        type: str,  # noqa: A002
        level: events.Level = "info",
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        message: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None: ...
    def notify(self, severity: notify.Severity, title: str, body: str = "") -> None: ...


def account_from_row(row: Mapping[str, Any]) -> Account:
    """A `platform_accounts` row as an `Account`."""
    hours = row["hourly_hours_utc"]
    return Account(
        id=row["id"],
        platform=Platform(row["platform"]),
        upload_post_user=row["upload_post_user"],
        handle=row["handle"],
        lanes=frozenset(Lane(lane) for lane in row["lanes"]),
        cap_24h=int(row["cap_24h"]),
        spike_reserve=int(row["spike_reserve"]),
        min_gap_s=int(row["min_gap_s"]),
        privacy=row["privacy"],
        hourly_hours_utc=None if hours is None else frozenset(int(hour) for hour in hours),
        enabled=bool(row["enabled"]),
        paused_until=None if row["paused_until"] is None else ensure_utc(row["paused_until"]),
        hourly_min_gap_s=None if row.get("hourly_min_gap_s") is None else int(row["hourly_min_gap_s"]),
    )


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


def record_from_row(row: Mapping[str, Any]) -> PostRecord:
    """A `posts` row joined with `clips.creator_id` as a `PostRecord`."""
    return PostRecord(
        account_id=row["account_id"],
        platform=Platform(row["platform"]),
        clip_id=row["clip_id"],
        lane=Lane(row["lane"]),
        status=row["status"],
        created_at=ensure_utc(row["created_at"]),
        published_at=_utc(row["published_at"]),
        dry_run=bool(row["dry_run"]),
        creator_id=row["creator_id"],
        slot_at=_utc(row["slot_at"]),
        family_id=row.get("family_id"),
    )


def clip_from_row(row: Mapping[str, Any]) -> LoadedClip:
    """A `clips` row joined with its creator's handle and its spike's wall time."""
    clip = Clip(
        id=row["id"],
        creator_id=row["creator_id"],
        lane=Lane(row["lane"]),
        rank_score=float(row["rank_score"]) if row["rank_score"] is not None else 0.0,
        status=row["status"],
        approved_at=_utc(row["approved_at"]),
        spike_at=_utc(row["spike_wall"]),
        creator_handle=row["creator_handle"] or "",
        source_platform=row["source_platform"],
        title=row["title"],
        caption=row["caption"],
        hook=row["hook"],
        hashtags=tuple(row["hashtags"] or ()),
        family_id=row.get("family_id"),
        duration_s=float(row["duration_s"]) if row.get("duration_s") is not None else None,
    )
    return LoadedClip(clip=clip, local_path=row["local_path"], spike_id=row["spike_id"])


def post_from_row(row: Mapping[str, Any]) -> PostRow:
    return PostRow(
        id=row["id"],
        clip_id=row["clip_id"],
        account_id=row["account_id"],
        platform=Platform(row["platform"]),
        lane=Lane(row["lane"]),
        slot_at=_utc(row["slot_at"]),
        status=row["status"],
        dry_run=bool(row["dry_run"]),
        idempotency_key=row["idempotency_key"],
        upload_request_id=row["upload_request_id"],
        platform_post_id=row["platform_post_id"],
        post_url=row["post_url"],
        error=row["error"],
        attempts=int(row["attempts"]),
        created_at=ensure_utc(row["created_at"]),
        published_at=_utc(row["published_at"]),
    )


def dry_run_key(clip_id: str, account_id: str, slot_at: datetime | None) -> str:
    """The `idempotency_key` of a dry-run row. It never equals the key of a real post
    (`post:<clip>:<account>`), so a dry-run row cannot block the real row later. One dry-run row exists per
    clip, account and hourly slot (or per clip and account in the spike lane)."""
    suffix = "spike" if slot_at is None else ensure_utc(slot_at).strftime("%Y%m%dT%H%MZ")
    return f"dry:{clip_id}:{account_id}:{suffix}"


def void_key(post_id: str) -> str:
    """The key of a real row that was cancelled before anything was sent; frees `post:<clip>:<account>`."""
    return f"void:{post_id}"


def clips_only(loaded: Sequence[LoadedClip]) -> list[Clip]:
    return [item.clip for item in loaded]


class DbStore:
    """Posting storage on Postgres."""

    # ── locking ──────────────────────────────────────────────

    @contextmanager
    def planning_lock(self) -> Iterator[None]:
        """One transaction holding the posting advisory lock. Decide and insert `posts` rows inside it."""
        with db.transaction():
            db.execute(f"set local lock_timeout = '{LOCK_TIMEOUT_S}s'")
            db.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (PLANNING_LOCK_NAME,))
            yield

    # ── loaders ──────────────────────────────────────────────

    def load_params(self) -> PostingParams:
        """The posting settings, read from the table now (not from the 30 s cache)."""
        rows = db.fetch_all(
            "select key, value from settings where key = any(%s)", ([*REQUIRED_KEYS, *OPTIONAL_KEYS],)
        )
        return PostingParams.from_settings({row["key"]: row["value"] for row in rows}, app_env())

    def load_accounts(self) -> list[Account]:
        return [account_from_row(row) for row in db.fetch_all(_ACCOUNT_SQL + " order by id")]

    def get_account(self, account_id: str) -> Account | None:
        row = db.fetch_one(_ACCOUNT_SQL + " where id = %s", (account_id,))
        return None if row is None else account_from_row(row)

    def load_history(self, now: datetime, clip_ids: Iterable[str] = ()) -> list[PostRecord]:
        """The posts the rules need at `now`: everything of the last 24 h (plus a margin), the latest
        hourly post of each account (R9) and every post of the clips in `clip_ids`."""
        since = ensure_utc(now, "now") - DAY - HISTORY_MARGIN
        rows = db.fetch_all(
            """
            select p.account_id, p.platform, p.clip_id::text as clip_id, p.lane, p.status, p.created_at,
                   p.published_at, p.dry_run, p.slot_at, c.creator_id::text as creator_id,
                   coalesce(c.parent_clip_id, c.id)::text as family_id
            from posts p
            join clips c on c.id = p.clip_id
            where p.created_at >= %(since)s
               or p.published_at >= %(since)s
               or p.clip_id = any(%(clip_ids)s::uuid[])
               or p.id in (
                 select distinct on (account_id, dry_run) id
                 from posts
                 where lane = 'hourly' and (dry_run or status <> 'skipped')
                 order by account_id, dry_run, coalesce(published_at, created_at) desc
               )
            """,
            {"since": since, "clip_ids": list(clip_ids)},
        )
        return [record_from_row(row) for row in rows]

    def load_hourly_queue(self) -> list[LoadedClip]:
        """Approved hourly clips, best rank first."""
        rows = db.fetch_all(
            _CLIP_SQL
            + """
            where c.status = 'approved' and c.lane = 'hourly'
            order by c.rank_score desc nulls last, c.approved_at, c.id
            limit %s
            """,
            (QUEUE_LIMIT,),
        )
        return [clip_from_row(row) for row in rows]

    def load_spike_clips(self) -> list[LoadedClip]:
        """Approved clips still in the spike lane."""
        rows = db.fetch_all(_CLIP_SQL + " where c.status = 'approved' and c.lane = 'spike'")
        return [clip_from_row(row) for row in rows]

    def get_clip(self, clip_id: str) -> LoadedClip | None:
        row = db.fetch_one(_CLIP_SQL + " where c.id = %s", (clip_id,))
        return None if row is None else clip_from_row(row)

    def get_post(self, post_id: str) -> PostRow | None:
        row = db.fetch_one(f"select {_POST_COLUMNS} from posts p where p.id = %s", (post_id,))
        return None if row is None else post_from_row(row)

    def posts_of_clip(self, clip_id: str) -> list[PostRow]:
        rows = db.fetch_all(
            f"select {_POST_COLUMNS} from posts p where p.clip_id = %s order by p.created_at, p.id",
            (clip_id,),
        )
        return [post_from_row(row) for row in rows]

    # ── post rows ────────────────────────────────────────────

    def insert_post(
        self,
        clip_id: str,
        account: Account,
        lane: Lane,
        slot_at: datetime | None,
        now: datetime,
        *,
        dry_run: bool,
        idempotency_key: str,
    ) -> str | None:
        """Insert a `pending` row (or a `skipped` dry-run row) and return its id, or None when a row with
        this key already exists."""
        row = db.fetch_one(
            """
            insert into posts
              (clip_id, account_id, platform, lane, slot_at, status, dry_run, idempotency_key, created_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (idempotency_key) do nothing
            returning id::text as id
            """,
            (
                clip_id,
                account.id,
                account.platform.value,
                lane.value,
                slot_at,
                "skipped" if dry_run else "pending",
                dry_run,
                idempotency_key,
                now,
            ),
        )
        return None if row is None else str(row["id"])

    def file_for_post(
        self, clip_id: str, account_id: str, fallback: str | None
    ) -> tuple[str | None, str | None]:
        """`(clip_renders.id, local file)` of the account's design style (development-plan/M18), or
        `(None, fallback)` for a clip rendered before migration 0027."""
        from sixstars.factory.variants import render_for_post

        render_id, path = render_for_post(clip_id, account_id)
        return (render_id, path) if path else (None, fallback)

    def mark_sending(self, post_id: str, request_id: str, render_id: str | None = None) -> None:
        db.execute(
            "update posts set attempts = attempts + 1, upload_request_id = %s, "
            "render_id = coalesce(%s::uuid, render_id) where id = %s and status = 'pending'",
            (request_id, render_id, post_id),
        )

    def mark_published(
        self, post_id: str, platform_post_id: str | None, post_url: str | None, now: datetime
    ) -> bool:
        changed = db.execute(
            """
            update posts
            set status = 'published', platform_post_id = %s, post_url = %s, published_at = %s, error = null
            where id = %s and status = 'pending'
            """,
            (platform_post_id, post_url, now, post_id),
        )
        return changed == 1

    def set_post_link(self, post_id: str, platform_post_id: str | None, post_url: str | None) -> None:
        db.execute(
            """
            update posts
            set platform_post_id = coalesce(%s, platform_post_id), post_url = coalesce(%s, post_url)
            where id = %s and status = 'published'
            """,
            (platform_post_id, post_url, post_id),
        )

    def mark_failed(self, post_id: str, error: str) -> bool:
        changed = db.execute(
            "update posts set status = 'failed', error = %s where id = %s and status = 'pending'",
            (error[:2000], post_id),
        )
        return changed == 1

    def void_post(self, post_id: str, reason: str) -> bool:
        """Cancel a pending row that was never sent: `skipped`, and its key no longer blocks a real post."""
        changed = db.execute(
            """
            update posts set status = 'skipped', error = %s, idempotency_key = %s
            where id = %s and status = 'pending'
            """,
            (reason[:2000], void_key(post_id), post_id),
        )
        return changed == 1

    # ── accounts, clips, spikes ──────────────────────────────

    def pause_account(
        self, account_id: str, until: datetime, reason: str, new_cap_24h: int | None = None
    ) -> None:
        """Pause an account (never shortening a pause) and optionally lower its cap (never raising it)."""
        db.execute(
            """
            update platform_accounts
            set paused_until = greatest(coalesce(paused_until, %(until)s), %(until)s),
                pause_reason = %(reason)s,
                cap_24h = least(cap_24h, coalesce(%(cap)s, cap_24h))
            where id = %(id)s
            """,
            {"until": until, "reason": reason[:500], "cap": new_cap_24h, "id": account_id},
        )

    def settle_clip(self, clip_id: str, now: datetime, exclude_job_id: int | None = None) -> bool:
        """Mark the clip `posted` once it has a published post and nothing is still on its way: no pending
        post and no deferred spike attempt (other than job `exclude_job_id`). True when it was marked."""
        changed = db.execute(
            """
            update clips set status = 'posted', posted_at = %(now)s
            where id = %(clip)s and status = 'approved'
              and exists (
                select 1 from posts where clip_id = %(clip)s and not dry_run and status = 'published')
              and not exists (
                select 1 from posts where clip_id = %(clip)s and not dry_run and status = 'pending')
              and not exists (
                select 1 from jobs
                where type = %(type)s and status in ('queued', 'running')
                  and payload->>'kind' = 'spike' and payload->>'clip_id' = %(clip_text)s
                  and id <> coalesce(%(job)s, -1))
            """,
            {
                "now": now,
                "clip": clip_id,
                "clip_text": clip_id,
                "type": RETRY_JOB_TYPE,
                "job": exclude_job_id,
            },
        )
        if changed == 1:
            self.emit("clip.posted", entity_type="clip", entity_id=clip_id)
        return changed == 1

    def demote_clip(self, clip_id: str, rank_score: float, *, dry: bool) -> bool:
        """R7: move a spike clip that has no post (published or on its way) to the hourly lane. In dry
        mode (`dry`) a clip with a dry-run record counts as posted and stays where it is."""
        changed = db.execute(
            """
            update clips set lane = 'hourly', rank_score = %(rank)s
            where id = %(clip)s and status = 'approved' and lane = 'spike'
              and not exists (
                select 1 from posts
                where clip_id = %(clip)s
                  and (status in ('published', 'pending') or (dry_run and %(dry)s)))
            """,
            {"rank": rank_score, "clip": clip_id, "dry": dry},
        )
        return changed == 1

    def record_spike_to_post(self, clip_id: str, now: datetime) -> None:
        """`spikes.spike_to_post_s`: seconds from the spike to its first published post (set once)."""
        db.execute(
            """
            update spikes s
            set spike_to_post_s = greatest(0, extract(epoch from (%s - s.spike_wall)))::int
            from clips c
            where c.id = %s and s.id = c.spike_id and s.spike_to_post_s is null and s.spike_wall is not null
            """,
            (now, clip_id),
        )

    # ── jobs ─────────────────────────────────────────────────

    def schedule_publish_retry(self, post_id: str, clip_id: str, lane: Lane, run_at: datetime) -> None:
        """Queue a `post.retry` job that resumes one post (re-send with the same key, or read its status)."""
        jobs.enqueue(
            RETRY_QUEUE,
            RETRY_JOB_TYPE,
            {"kind": "publish", "post_id": post_id, "clip_id": clip_id},
            priority=SPIKE_RETRY_PRIORITY if lane is Lane.SPIKE else HOURLY_RETRY_PRIORITY,
            run_after=run_at,
            dedupe_key=f"{RETRY_JOB_TYPE}:publish:{post_id}:{int(run_at.timestamp())}",
        )

    def schedule_spike_retry(self, clip_id: str, run_at: datetime, after_job: int | None = None) -> None:
        """Queue a `post.retry` job that tries a deferred spike clip again (R4), just after `run_at`.

        `after_job` is the retry job that is asking, if any: its number is part of the dedupe key, so a job
        that has to wait once more is not refused as a duplicate of itself.
        """
        jobs.enqueue(
            RETRY_QUEUE,
            RETRY_JOB_TYPE,
            {"kind": "spike", "clip_id": clip_id},
            priority=SPIKE_RETRY_PRIORITY,
            run_after=run_at + SPIKE_RETRY_MARGIN,
            dedupe_key=f"{RETRY_JOB_TYPE}:spike:{clip_id}:{int(run_at.timestamp())}:{after_job or 0}",
        )

    # ── events and alerts ────────────────────────────────────

    def emit(
        self,
        type: str,  # noqa: A002
        level: events.Level = "info",
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        message: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        events.emit(
            type,
            level,
            entity_type=entity_type,
            entity_id=entity_id,
            message=message,
            data=data,
            service=SERVICE,
        )

    def notify(self, severity: notify.Severity, title: str, body: str = "") -> None:
        notify.send(severity, title, body)

    def slot_was_empty(self, slot_at: datetime) -> bool:
        """True when a `posting.slot_empty` event exists for this slot."""
        row = db.fetch_one(
            "select 1 as hit from events where type = 'posting.slot_empty' and entity_id = %s limit 1",
            (ensure_utc(slot_at).isoformat(),),
        )
        return row is not None

"""Helpers for the guardian and stats integration tests: fake platforms whose answers a test can set,
and builders for creators with published posts. Nothing here talks to a network."""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from posting_support import add_clip, add_creator, clean_posting_tables, seed, use_env

from sixstars import db
from sixstars.ai.schemas import TakedownVerdict
from sixstars.clients.fakes import FakeUploadPostClient, FakeYouTubeClient
from sixstars.clients.upload_post import PostRejected
from sixstars.clients.youtube import YouTubeVideoStatus

IG, YT, TT = "instagram_main", "youtube_main", "tiktok_main"
PLATFORM = {IG: "instagram", YT: "youtube", TT: "tiktok"}
TIKTOK_GONE = "TikTok video not found (ID: {id})."
INSTAGRAM_GONE = "Instagram API error (HTTP 400). The post may have been deleted or the token expired."


def now() -> datetime:
    return datetime.now(UTC)


def db_now() -> datetime:
    """The database clock, for tests that compare heartbeat ages with a limit to the second."""
    found = db.fetch_one("select now() as now")
    assert found is not None
    stamp: datetime = found["now"]
    return stamp


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clean tables, the production accounts (public posts) and `APP_ENV=production`."""
    use_env(monkeypatch, "production")
    clean_posting_tables()
    db.execute("truncate takedowns, account_stats")
    seed("production")
    yield
    db.execute("truncate takedowns, account_stats")
    clean_posting_tables()


class UploadPost(FakeUploadPostClient):
    """Upload-Post whose analytics answers are set per platform post id. Unknown ids read fine."""

    def __init__(self) -> None:
        super().__init__()
        self.gone: set[str] = set()  # platform post ids that are no longer on the platform
        self.metrics: dict[str, dict[str, Any]] = {}
        self.fail: Exception | None = None
        self.delete_error: Exception | None = None

    def _entry(self, platform: str, platform_post_id: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"success": True, "platform_post_id": platform_post_id}
        if platform_post_id in self.gone:
            entry["post_metrics_error"] = (
                INSTAGRAM_GONE if platform == "instagram" else TIKTOK_GONE.format(id=platform_post_id)
            )
        else:
            entry["post_metrics"] = self.metrics.get(
                platform_post_id, {"views": 1000, "likes": 50, "comments": 5, "shares": 2}
            )
        return {"success": True, "post": {"platform": platform}, "platforms": {platform: entry}}

    def get_post_analytics(self, request_id: str, platform: str | None = None) -> dict[str, Any]:
        self._record("get_post_analytics", request_id=request_id, platform=platform)
        raise PostRejected("GET post-analytics: HTTP 404: Post not found.")

    def get_post_analytics_by_platform_id(
        self, user: str, platform: str, platform_post_id: str
    ) -> dict[str, Any]:
        self._record("by_platform_id", user=user, platform=platform, platform_post_id=platform_post_id)
        if self.fail is not None:
            raise self.fail
        return self._entry(platform, platform_post_id)

    def delete_post(self, user: str, platform: str, post_id: str) -> bool:
        if self.delete_error is not None:
            self._record("delete_post", user=user, platform=platform, post_id=post_id)
            raise self.delete_error
        return super().delete_post(user, platform, post_id)


class YouTube(FakeYouTubeClient):
    """YouTube whose videos can be missing or restricted."""

    def __init__(self, oauth: bool = False) -> None:
        super().__init__(oauth=oauth)
        self.missing: set[str] = set()
        self.items: dict[str, dict[str, Any]] = {}
        self.fail_list: Exception | None = None
        self.deleted: list[str] = []

    def videos_list(self, ids: Sequence[str], parts: Sequence[str] = ("snippet",)) -> list[dict[str, Any]]:
        self._record("videos_list", ids=list(ids), parts=list(parts))
        if self.fail_list is not None:
            raise self.fail_list
        out = []
        for video_id in dict.fromkeys(ids):
            if video_id in self.missing:
                continue
            item: dict[str, Any] = {
                "id": video_id,
                "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                "contentDetails": {},
                "statistics": {"viewCount": "5000", "likeCount": "200", "commentCount": "20"},
            }
            item.update(self.items.get(video_id, {}))
            out.append(item)
        return out

    def get_status(self, video_id: str) -> YouTubeVideoStatus | None:
        self._record("get_status", video_id=video_id)
        if video_id in self.missing:
            return None
        return YouTubeVideoStatus(video_id, "processed", "private", None, None, "youtube", False, (), None)

    def delete_video(self, video_id: str) -> bool:
        self._record("delete_video", video_id=video_id)
        self.deleted.append(video_id)
        return True


def creator(handle: str, display_name: str | None = None) -> str:
    creator_id = add_creator(handle)
    if display_name:
        db.execute("update creators set display_name = %s where id = %s", (display_name, creator_id))
    return creator_id


def published(
    creator_id: str,
    account_id: str,
    platform_post_id: str,
    *,
    age: timedelta = timedelta(hours=2),
    url: str | None = None,
    status: str = "published",
    dry_run: bool = False,
    title: str = "A clip",
    lane: str = "hourly",
    views: int | None = None,
) -> str:
    """A clip of the creator with one post on the account. Returns the post id."""
    clip_id = add_clip(creator_id, None, status="posted", title=title, lane=lane)
    at = now() - age
    row = db.fetch_one(
        """
        insert into posts (clip_id, account_id, platform, lane, status, dry_run, idempotency_key,
                           platform_post_id, post_url, created_at, published_at, slot_at, views)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, date_trunc('hour', %s::timestamptz), %s)
        returning id::text as id
        """,
        (
            clip_id,
            account_id,
            PLATFORM[account_id],
            lane,
            status,
            dry_run,
            f"{'dry' if dry_run else 'post'}:{clip_id}:{account_id}",
            platform_post_id,
            url or f"https://example.test/{PLATFORM[account_id]}/{platform_post_id}",
            at,
            at if status != "pending" else None,
            at,
            views,
        ),
    )
    assert row is not None
    return str(row["id"])


def row(table: str, row_id: str) -> dict[str, Any]:
    found = db.fetch_one(f"select * from {table} where id = %s", (row_id,))
    assert found is not None
    return found


def post_states() -> dict[str, tuple[str, str | None]]:
    """`platform_post_id -> (status, claim_status)` of every post."""
    rows = db.fetch_all("select platform_post_id, status, claim_status from posts")
    return {r["platform_post_id"]: (r["status"], r["claim_status"]) for r in rows}


def outbox() -> list[tuple[str, str, str]]:
    rows = db.fetch_all("select severity, title, body from notify_outbox order by id")
    return [(r["severity"], r["title"], r["body"]) for r in rows]


def titles(severity: str | None = None) -> list[str]:
    return [title for level, title, _ in outbox() if severity is None or level == severity]


def event_types(prefix: str = "") -> list[str]:
    rows = db.fetch_all("select type from events order by id")
    return [r["type"] for r in rows if r["type"].startswith(prefix)]


def takedown_rows() -> list[dict[str, Any]]:
    return db.fetch_all("select *, id::text as takedown_id from takedowns order by received_at, id")


def setting(key: str) -> Any:
    found = db.fetch_one("select value from settings where key = %s", (key,))
    assert found is not None
    return found["value"]


def verdict(
    is_takedown: bool = True,
    handles: Sequence[str] = (),
    urls: Sequence[str] = (),
    kind: str = "copyright",
    summary: str = "They ask us to remove their clip.",
) -> TakedownVerdict:
    return TakedownVerdict(
        is_takedown=is_takedown,
        kind=kind if is_takedown else "not_takedown",  # type: ignore[arg-type]
        creator_handles=list(handles),
        urls=list(urls),
        platforms=[],
        summary=summary,
    )


class Classifier:
    """Stands in for prompt P7: returns the verdict it was given and records what it was shown."""

    def __init__(self, answer: TakedownVerdict | Exception) -> None:
        self.answer = answer
        self.seen: list[tuple[str, str, str]] = []

    def __call__(self, sender: str, subject: str, body: str) -> TakedownVerdict:
        self.seen.append((sender, subject, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

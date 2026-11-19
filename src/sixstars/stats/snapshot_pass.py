"""The snapshot pass of a stats run (docs/28 Part A, L1): take the due fixed-age readings first.

Called by `collector.collect` before the `posts.*` totals refresh (owner amendment 3, 19 Nov 2026).
YouTube readings use one Data API call for all due videos (counts) plus one Analytics API call when it is
switched on (`youtube_analytics` in the flag) and OAuth exists; the rest use Upload-Post post analytics,
one call per post, paced by a token bucket. Every reading also refreshes the post's totals, so the
totals pass can skip those posts.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db
from sixstars.clients.upload_post import AccountAuthError, PostRejected, RateLimited, UploadPostClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.clients.youtube_analytics import YouTubeAnalyticsClient
from sixstars.guardian.removal import youtube_id_from_url
from sixstars.learning import snapshots
from sixstars.learning.snapshots import DuePost, DueReading, SnapshotNumbers, SnapshotRules
from sixstars.logging import get_logger

SERVICE = "stats"


class TokenBucket:
    """At most `capacity` calls at once, refilled at `refill_per_s` (Upload-Post: 100 per 5 minutes)."""

    def __init__(self, capacity: float, refill_per_s: float, clock: Callable[[], float] = time.monotonic):
        self.capacity, self.refill, self._clock = capacity, refill_per_s, clock
        self._tokens, self._at = capacity, clock()

    def try_take(self) -> bool:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._at) * self.refill)
        self._at = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False


@dataclass(slots=True)
class PassResult:
    taken: int = 0
    missing: int = 0  # the platform had no numbers yet
    left: int = 0  # due readings not taken in this pass (budget, bucket or rate limit)
    rate_limited: bool = False
    refreshed: set[str] = field(default_factory=set)  # post ids whose totals were refreshed
    errors: list[str] = field(default_factory=list)


_DUE_SQL = """
select p.id::text as id, p.platform, p.published_at, p.account_id, p.platform_post_id, p.upload_request_id,
       p.post_url, p.boosted, a.upload_post_user, a.privacy,
       coalesce(array_agg(s.target_age_h) filter (where s.id is not null), '{}') as taken
from posts p
join platform_accounts a on a.id = p.account_id
left join post_metrics_snapshots s on s.post_id = p.id
where p.status = 'published' and not p.dry_run and p.published_at >= %s
group by p.id, a.upload_post_user, a.privacy
"""


def load_due(now: datetime, rules: SnapshotRules) -> tuple[list[DueReading], dict[str, dict[str, Any]]]:
    since = now - timedelta(hours=snapshots.oldest_target_window_h(rules))
    rows = db.fetch_all(_DUE_SQL, (since,))
    by_id = {r["id"]: r for r in rows}
    posts = [
        DuePost(r["id"], r["platform"], r["published_at"], frozenset(float(t) for t in r["taken"]))
        for r in rows
    ]
    return snapshots.due_readings(posts, now, rules), by_id


def insert_snapshot(
    reading: DueReading, row: Mapping[str, Any], numbers: SnapshotNumbers, source: str, raw: Any
) -> bool:
    values = asdict(numbers)
    values.pop("extra")
    for key in ("impression_sources", "audience_types"):
        values[key] = Jsonb(values[key]) if values[key] is not None else None
    inserted = db.execute(
        """
        insert into post_metrics_snapshots
          (post_id, account_id, platform, target_age_h, age_h, source, views, likes, comments, shares, saves,
           reach, new_followers, completion_rate, avg_view_pct, avg_watch_s, total_watch_s,
           impression_sources, audience_types, paid, raw)
        values (%(post_id)s, %(account_id)s, %(platform)s, %(target)s, %(age)s, %(source)s, %(views)s,
                %(likes)s, %(comments)s, %(shares)s, %(saves)s, %(reach)s, %(new_followers)s,
                %(completion_rate)s, %(avg_view_pct)s, %(avg_watch_s)s, %(total_watch_s)s,
                %(impression_sources)s, %(audience_types)s, %(paid)s, %(raw)s)
        on conflict (post_id, target_age_h) do nothing
        """,
        values
        | {
            "post_id": reading.post_id,
            "account_id": row["account_id"],
            "platform": reading.platform,
            "target": reading.target_age_h,
            "age": reading.age_h,
            "source": source,
            "paid": bool(row["boosted"]),
            "raw": Jsonb(raw) if raw is not None else None,
        },
    )
    return inserted > 0


def _video_id(row: Mapping[str, Any]) -> str | None:
    if row["platform_post_id"]:
        return str(row["platform_post_id"])
    return youtube_id_from_url(row["post_url"])


def _is_public(row: Mapping[str, Any]) -> bool:
    return str(row["privacy"] or "public").lower() in ("public", "public_to_everyone")


def run(
    now: datetime,
    rules: SnapshotRules,
    flag: Mapping[str, Any],
    upload_post: UploadPostClient,
    youtube: YouTubeClient,
    analytics: YouTubeAnalyticsClient | None,
    deadline: float,
    clock: Callable[[], float],
    save_post: Callable[[str, SnapshotNumbers], None],
    save_link: Callable[[str, str | None, str | None], None],
    parse_link: Callable[[Any, str], tuple[str | None, str | None]],
) -> PassResult:
    result = PassResult()
    due, rows = load_due(now, rules)
    yt = [
        d
        for d in due
        if d.platform == "youtube" and _is_public(rows[d.post_id]) and _video_id(rows[d.post_id])
    ]
    yt_ids = {d.post_id for d in yt}
    others = [d for d in due if d.post_id not in yt_ids]
    if yt:
        others.extend(_youtube(yt, rows, youtube, analytics, flag, now, result, save_post))
    bucket = TokenBucket(
        float(flag.get("bucket_capacity", 90) or 90),
        float(flag.get("bucket_refill_per_s", 0.3) or 0.3),
        clock,
    )
    _upload_post(others, rows, upload_post, bucket, deadline, clock, result, save_post, save_link, parse_link)
    return result


def _youtube(
    due: Sequence[DueReading],
    rows: Mapping[str, Mapping[str, Any]],
    youtube: YouTubeClient,
    analytics: YouTubeAnalyticsClient | None,
    flag: Mapping[str, Any],
    now: datetime,
    result: PassResult,
    save_post: Callable[[str, SnapshotNumbers], None],
) -> list[DueReading]:
    """Take YouTube readings with the Data API (+ Analytics). Returns the readings it could not serve."""
    if not youtube.is_configured():
        return list(due)
    ids = {d.post_id: str(_video_id(rows[d.post_id])) for d in due}
    try:
        items = {str(i.get("id", "")): i for i in youtube.videos_list(list(ids.values()), ("statistics",))}
    except Exception as exc:
        result.errors.append(f"youtube api: {type(exc).__name__}")
        return list(due)
    extra: dict[str, Any] = {}
    if analytics is not None and flag.get("youtube_analytics") is True and analytics.is_configured():
        try:
            start = min(rows[d.post_id]["published_at"] for d in due).date()
            extra = analytics.video_report(list(ids.values()), start, now.date())
        except Exception as exc:
            result.errors.append(f"youtube analytics: {type(exc).__name__}")
    for d in due:
        video = ids[d.post_id]
        numbers = snapshots.from_youtube_statistics(items[video]) if video in items else SnapshotNumbers()
        source = "youtube_data"
        if (report := extra.get(video)) is not None:
            numbers = snapshots.merge(
                numbers,
                SnapshotNumbers(
                    new_followers=report.subscribers_gained,
                    avg_view_pct=report.avg_view_pct,
                    avg_watch_s=report.avg_view_duration_s,
                    total_watch_s=report.minutes_watched * 60 if report.minutes_watched is not None else None,
                ),
            )
            source = "youtube_data+analytics"
        if numbers.empty:
            result.missing += 1
            continue
        raw = {"statistics": items[video].get("statistics")} if video in items else None
        if insert_snapshot(d, rows[d.post_id], numbers, source, raw):
            result.taken += 1
        save_post(d.post_id, numbers)
        result.refreshed.add(d.post_id)
    return []


def _upload_post(
    due: Sequence[DueReading],
    rows: Mapping[str, Mapping[str, Any]],
    upload_post: UploadPostClient,
    bucket: TokenBucket,
    deadline: float,
    clock: Callable[[], float],
    result: PassResult,
    save_post: Callable[[str, SnapshotNumbers], None],
    save_link: Callable[[str, str | None, str | None], None],
    parse_link: Callable[[Any, str], tuple[str | None, str | None]],
) -> None:
    log = get_logger(SERVICE)
    if not upload_post.is_configured():
        result.left += len(due)
        return
    for index, reading in enumerate(due):
        if clock() >= deadline or not bucket.try_take():
            result.left += len(due) - index
            return
        row = rows[reading.post_id]
        body: Mapping[str, Any] | None = None
        try:
            if row["upload_request_id"]:
                try:
                    body = upload_post.get_post_analytics(str(row["upload_request_id"]), reading.platform)
                except PostRejected:
                    body = None
            if body is None and row["platform_post_id"]:
                body = upload_post.get_post_analytics_by_platform_id(
                    str(row["upload_post_user"]), reading.platform, str(row["platform_post_id"])
                )
        except RateLimited:
            result.rate_limited = True
            result.left += len(due) - index
            result.errors.append("snapshots: rate limited")
            return
        except AccountAuthError:
            result.left += len(due) - index
            result.errors.append("snapshots: not authorised")
            return
        except Exception as exc:
            log.warning("snapshot_read_failed", post_id=reading.post_id, error=type(exc).__name__)
            result.missing += 1
            continue
        late_id, late_url = parse_link(body, reading.platform)
        if (late_id and not row["platform_post_id"]) or (late_url and not row["post_url"]):
            save_link(reading.post_id, late_id, late_url)
        numbers = snapshots.parse_snapshot(body, reading.platform)
        if numbers.empty:
            result.missing += 1
            continue
        if insert_snapshot(
            reading, row, numbers, "upload_post", snapshots.metrics_of(body, reading.platform)
        ):
            result.taken += 1
        save_post(reading.post_id, numbers)
        result.refreshed.add(reading.post_id)

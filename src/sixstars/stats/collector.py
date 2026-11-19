"""Stats collection: `POST /v1/stats/collect` (WF05) and the worker job `stats.collect`.

- Account level: one Upload-Post call per profile (`GET /api/analytics/{profile}?platforms=...`) gives
  followers, reach and impressions per platform; one `account_stats` row per account.
- Post level: views, likes, comments and shares of the posts published in the last
  `stats.post_lookback_d` days are written to `posts`. YouTube comes from the public Data API
  (`videos.list?part=statistics`, 50 ids per call) when the API key is set and the account posts
  publicly; everything else comes from Upload-Post post analytics, one call per post.
- `creators.avg_clip_views` is recomputed from the posts.

A pass is bounded by `stats.max_posts_per_run` and `stats.collect_budget_s`; posts with the oldest
numbers go first, so the rest is picked up by the next pass. A rate limit ends the pass early. Calling
it twice in a row stores nothing twice: accounts get at most one snapshot per
`stats.account_min_interval_s`, and post numbers are overwritten.

Real answers of the profile analytics endpoint (checked 2026-11-17 on profile `default`): an object keyed
by platform. TikTok: `followers, reach, impressions, profileViews, likes, comments, shares, video_count,
following, reach_timeseries[{date, value}], metric_type, available_metrics, metric_labels,
primary_impressions_field`. Instagram adds `views, saves, accounts_engaged, total_interactions, ...` and
its numbers are floats. YouTube adds `watch_time_minutes, average_view_duration_seconds, data_source,
stale, stale_reason`. Only Instagram has a `views` key; for the others the field named by
`primary_impressions_field` (`impressions`) is the view count.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db, events, jobs, settings
from sixstars.clients.upload_post import AccountAuthError, PostRejected, RateLimited, UploadPostClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.clients.youtube_analytics import YouTubeAnalyticsClient
from sixstars.guardian.claims import single_pass
from sixstars.guardian.removal import youtube_id_from_url
from sixstars.learning import delivery, snapshots
from sixstars.logging import get_logger
from sixstars.scoring import config as scoring_config
from sixstars.stats import snapshot_pass

SERVICE = "stats"
LOCK_NAME = "sixstars.stats.collect"
JOB_COLLECT = "stats.collect"
RAW_DROPPED_SUFFIXES = ("_timeseries", "_demographics")
RAW_DROPPED_KEYS = frozenset({"breakdowns", "metric_labels", "available_metrics"})

_VIEW_KEYS = ("views", "view_count", "viewCount", "video_views", "play_count", "plays", "impressions")
_LIKE_KEYS = ("likes", "like_count", "likeCount", "digg_count")
_COMMENT_KEYS = ("comments", "comment_count", "commentCount")
_SHARE_KEYS = ("shares", "share_count", "shareCount")


@dataclass(frozen=True, slots=True)
class PostNumbers:
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None

    @property
    def empty(self) -> bool:
        return self.views is None and self.likes is None and self.comments is None and self.shares is None


@dataclass(frozen=True, slots=True)
class AccountNumbers:
    followers: int | None
    views_total: int | None
    reach: int | None
    impressions: int | None
    raw: dict[str, Any]


@dataclass(slots=True)
class CollectResult:
    status: str = "ok"  # ok | already_running
    accounts: int = 0
    posts: int = 0
    skipped: int = 0
    rate_limited: bool = False
    snapshots: int = 0
    snapshots_left: int = 0
    handed_to_job: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accounts": self.accounts,
            "posts": self.posts,
            "skipped": self.skipped,
            "rate_limited": self.rate_limited,
            "snapshots": self.snapshots,
            "snapshots_left": self.snapshots_left,
            "handed_to_job": self.handed_to_job,
            "errors": self.errors,
        }


# ── pure parsing ─────────────────────────────────────────────


def to_int(value: Any) -> int | None:
    """A count from an API answer: an int, a float or a numeric string; anything else is None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value == value and value != float("inf") else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            number = float(text)
        except ValueError:
            return None
        return to_int(number)
    return None


def _first(source: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        number = to_int(source.get(key))
        if number is not None:
            return number
    return None


def parse_account(body: Any) -> AccountNumbers | None:
    """One platform entry of the profile analytics answer. None when it carries no numbers (an error
    entry, or a platform that is not connected)."""
    if not isinstance(body, Mapping):
        return None
    followers = to_int(body.get("followers"))
    primary = body.get("primary_impressions_field")
    views = to_int(body.get("views"))
    if views is None and isinstance(primary, str):
        views = to_int(body.get(primary))
    reach = to_int(body.get("reach"))
    impressions = to_int(body.get("impressions"))
    if followers is None and views is None and reach is None and impressions is None:
        return None
    raw = {
        key: value
        for key, value in body.items()
        if key not in RAW_DROPPED_KEYS
        and not key.endswith(RAW_DROPPED_SUFFIXES)
        and isinstance(value, str | int | float | bool | type(None))
    }
    return AccountNumbers(followers, views, reach, impressions, raw)


def parse_post_metrics(body: Any, platform: str) -> PostNumbers:
    """Numbers of one post from an Upload-Post post-analytics answer (`platforms.<platform>.post_metrics`,
    or `post_metrics` at the top). A post without metrics gives an empty result."""
    if not isinstance(body, Mapping):
        return PostNumbers()
    platforms = body.get("platforms")
    entry = platforms.get(platform) if isinstance(platforms, Mapping) else None
    metrics = (entry if isinstance(entry, Mapping) else body).get("post_metrics")
    if not isinstance(metrics, Mapping):
        return PostNumbers()
    return PostNumbers(
        _first(metrics, _VIEW_KEYS),
        _first(metrics, _LIKE_KEYS),
        _first(metrics, _COMMENT_KEYS),
        _first(metrics, _SHARE_KEYS),
    )


def parse_post_link(body: Any, platform: str) -> tuple[str | None, str | None]:
    """The platform's own post id and the link of a post in an Upload-Post post-analytics answer
    (`platforms.<platform>.platform_post_id` and `.post_url`); each is None when the answer has none.

    TikTok releases the post id after its moderation, minutes to hours after the upload, and Upload-Post
    then adds it to this answer (first real upload: both were null at first). `post_url` can also hold a
    note instead of a link ("Post uploaded as Private. No public URL available."): only a real link counts.
    """
    if not isinstance(body, Mapping):
        return None, None
    platforms = body.get("platforms")
    entry = platforms.get(platform) if isinstance(platforms, Mapping) else None
    if not isinstance(entry, Mapping):
        return None, None
    raw_id, raw_url = entry.get("platform_post_id"), entry.get("post_url")
    post_id = str(raw_id).strip() if isinstance(raw_id, str | int) and not isinstance(raw_id, bool) else ""
    url = raw_url.strip() if isinstance(raw_url, str) else ""
    return post_id or None, url if url.startswith(("https://", "http://")) else None


def parse_youtube_statistics(item: Mapping[str, Any]) -> PostNumbers:
    """Numbers from a `videos.list` item with part `statistics` (the API sends them as strings)."""
    stats = item.get("statistics")
    if not isinstance(stats, Mapping):
        return PostNumbers()
    return PostNumbers(
        to_int(stats.get("viewCount")),
        to_int(stats.get("likeCount")),
        to_int(stats.get("commentCount")),
        None,
    )


# ── accounts ─────────────────────────────────────────────────


def collect_accounts(upload_post: UploadPostClient, result: CollectResult) -> None:
    min_interval_s = settings.get_float("stats.account_min_interval_s")
    accounts = db.fetch_all(
        """
        select a.id, a.platform, a.upload_post_user,
               (select max(captured_at) from account_stats s where s.account_id = a.id) as last_at,
               now() as db_now
        from platform_accounts a where a.enabled order by a.id
        """
    )
    due = [
        row
        for row in accounts
        if row["last_at"] is None or (row["db_now"] - row["last_at"]).total_seconds() >= min_interval_s
    ]
    by_user: dict[str, list[dict[str, Any]]] = {}
    for row in due:
        by_user.setdefault(str(row["upload_post_user"]), []).append(row)
    log = get_logger(SERVICE)
    for user, rows in by_user.items():
        platforms = sorted({str(row["platform"]) for row in rows})
        try:
            body = upload_post.get_profile_analytics(user, platforms)
        except RateLimited:
            result.rate_limited = True
            result.errors.append("profile analytics: rate limited")
            return
        except Exception as exc:
            log.error("profile_analytics_failed", error=type(exc).__name__)
            result.errors.append(f"profile analytics: {type(exc).__name__}")
            continue
        for row in rows:
            numbers = parse_account(body.get(row["platform"]))
            if numbers is None:
                result.errors.append(f"{row['id']}: no numbers")
                continue
            db.execute(
                """
                insert into account_stats (account_id, followers, views_total, reach, impressions, raw)
                values (%s, %s, %s, %s, %s, %s)
                """,
                (
                    row["id"],
                    numbers.followers,
                    numbers.views_total,
                    numbers.reach,
                    numbers.impressions,
                    Jsonb(numbers.raw),
                ),
            )
            result.accounts += 1


# ── posts ────────────────────────────────────────────────────


def posts_due(now: datetime) -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        select p.id::text as id, p.platform, p.platform_post_id, p.upload_request_id, p.post_url,
               a.upload_post_user, a.privacy
        from posts p join platform_accounts a on a.id = p.account_id
        where p.status = 'published' and not p.dry_run and p.published_at >= %s
        order by p.stats_updated_at nulls first, p.published_at desc
        limit %s
        """,
        (
            now - timedelta(days=settings.get_float("stats.post_lookback_d")),
            max(1, settings.get_int("stats.max_posts_per_run")),
        ),
    )


def save_post(post_id: str, numbers: PostNumbers) -> None:
    db.execute(
        """
        update posts
        set views = coalesce(%s, views), likes = coalesce(%s, likes), comments = coalesce(%s, comments),
            shares = coalesce(%s, shares), stats_updated_at = now()
        where id = %s
        """,
        (numbers.views, numbers.likes, numbers.comments, numbers.shares, post_id),
    )


def save_link(post_id: str, platform_post_id: str | None, post_url: str | None) -> None:
    """Fill in a post id or link that was not known when the post was published. Never replaces one."""
    db.execute(
        """
        update posts
        set platform_post_id = coalesce(platform_post_id, %s), post_url = coalesce(post_url, %s)
        where id = %s and (platform_post_id is null or post_url is null)
        """,
        (platform_post_id, post_url, post_id),
    )


def _is_public(row: Mapping[str, Any]) -> bool:
    return str(row["privacy"] or "public").lower() in ("public", "public_to_everyone")


def _video_id(row: Mapping[str, Any]) -> str | None:
    if row["platform_post_id"]:
        return str(row["platform_post_id"])
    return youtube_id_from_url(row["post_url"])


def collect_youtube(
    rows: Sequence[dict[str, Any]], youtube: YouTubeClient, result: CollectResult
) -> list[dict[str, Any]]:
    """Read public YouTube posts with the API key. Returns the rows this source could not serve."""
    if not youtube.is_configured():
        return list(rows)
    public = [(row, _video_id(row)) for row in rows if _is_public(row)]
    usable = [(row, video_id) for row, video_id in public if video_id]
    usable_ids = {row["id"] for row, _ in usable}
    rest = [row for row in rows if row["id"] not in usable_ids]
    if not usable:
        return rest
    try:
        items = youtube.videos_list([video_id for _, video_id in usable], ("statistics",))
    except Exception as exc:
        get_logger(SERVICE).error("youtube_statistics_failed", error=type(exc).__name__)
        result.errors.append(f"youtube api: {type(exc).__name__}")
        return [*rest, *(row for row, _ in usable)]
    by_id = {str(item.get("id", "")): item for item in items}
    for row, video_id in usable:
        item = by_id.get(video_id)
        numbers = parse_youtube_statistics(item) if item is not None else PostNumbers()
        if numbers.empty:
            result.skipped += 1  # gone or hidden: the claims check deals with it
            continue
        save_post(row["id"], numbers)
        result.posts += 1
    return rest


def collect_upload_post(
    rows: Sequence[dict[str, Any]],
    upload_post: UploadPostClient,
    result: CollectResult,
    deadline: float,
    clock: Callable[[], float],
) -> None:
    log = get_logger(SERVICE)
    for index, row in enumerate(rows):
        if clock() >= deadline:
            result.skipped += len(rows) - index
            result.errors.append("time budget used; the remaining posts wait for the next pass")
            return
        body: Mapping[str, Any] | None = None
        try:
            if row["upload_request_id"]:
                try:
                    body = upload_post.get_post_analytics(str(row["upload_request_id"]), str(row["platform"]))
                except PostRejected:
                    body = None  # HTTP 404: Upload-Post does not know this request id
            if body is None and row["platform_post_id"]:
                body = upload_post.get_post_analytics_by_platform_id(
                    str(row["upload_post_user"]), str(row["platform"]), str(row["platform_post_id"])
                )
        except RateLimited:
            result.rate_limited = True
            result.skipped += len(rows) - index
            result.errors.append("post analytics: rate limited")
            return
        except AccountAuthError:
            result.skipped += len(rows) - index
            result.errors.append("post analytics: not authorised")
            return
        except Exception as exc:
            log.warning("post_analytics_failed", post_id=row["id"], error=type(exc).__name__)
            result.skipped += 1
            continue
        late_id, late_url = parse_post_link(body, str(row["platform"]))
        if (late_id and not row["platform_post_id"]) or (late_url and not row["post_url"]):
            save_link(row["id"], late_id, late_url)
        numbers = parse_post_metrics(body, str(row["platform"]))
        if numbers.empty:
            result.skipped += 1
            continue
        save_post(row["id"], numbers)
        result.posts += 1


def _save_snapshot_totals(post_id: str, numbers: snapshots.SnapshotNumbers) -> None:
    save_post(post_id, PostNumbers(numbers.views, numbers.likes, numbers.comments, numbers.shares))


def update_creator_averages() -> int:
    return db.execute(
        """
        update creators cr set avg_clip_views = s.average
        from (
          select c.creator_id, avg(p.views) as average
          from posts p join clips c on c.id = p.clip_id
          where p.views is not null and not p.dry_run
            and p.status in ('published', 'removed', 'remove_failed')
          group by c.creator_id
        ) s
        where cr.id = s.creator_id and cr.avg_clip_views is distinct from s.average
        """
    )


def collect(
    upload_post: UploadPostClient,
    youtube: YouTubeClient,
    now: datetime,
    clock: Callable[[], float] = time.monotonic,
    *,
    analytics: YouTubeAnalyticsClient | None = None,
    budget_s: float | None = None,
    from_job: bool = False,
) -> CollectResult:
    """One stats pass. With `learning.snapshots` on, the due fixed-age readings go first; readings left
    when the budget ends are handed to the worker job `stats.collect` (not from inside that job)."""
    with single_pass(LOCK_NAME) as locked:
        if not locked:
            return CollectResult(status="already_running")
        result = CollectResult()
        budget = budget_s if budget_s is not None else settings.get_float("stats.collect_budget_s")
        deadline = clock() + budget
        if not upload_post.is_configured():
            result.errors.append("upload-post: no API key")
        else:
            collect_accounts(upload_post, result)
        refreshed: set[str] = set()
        flag = scoring_config.flag(snapshots.FLAG)
        if flag is not None:
            snap = snapshot_pass.run(
                now,
                snapshots.SnapshotRules.from_flag(flag),
                flag,
                upload_post,
                youtube,
                analytics,
                deadline,
                clock,
                _save_snapshot_totals,
                save_link,
                parse_post_link,
            )
            refreshed = snap.refreshed
            result.snapshots, result.snapshots_left = snap.taken, snap.left
            result.errors.extend(snap.errors)
            result.rate_limited = result.rate_limited or snap.rate_limited
            if snap.left and not from_job and not snap.rate_limited:
                job = jobs.enqueue("normal", JOB_COLLECT, {}, dedupe_key=JOB_COLLECT)
                result.handed_to_job = job is not None
        rows = [row for row in posts_due(now) if row["id"] not in refreshed]
        rest = collect_youtube([row for row in rows if row["platform"] == "youtube"], youtube, result)
        others = [*rest, *(row for row in rows if row["platform"] != "youtube")]
        if result.rate_limited or not upload_post.is_configured():
            result.skipped += len(others)
        else:
            collect_upload_post(others, upload_post, result, deadline, clock)
        update_creator_averages()
        delivery.run(now)
        events.emit(
            "stats.collected",
            message=f"stats: {result.accounts} account(s), {result.posts} post(s)",
            data=result.as_dict(),
            service=SERVICE,
        )
        get_logger(SERVICE).info(
            "stats_collected", **{k: v for k, v in result.as_dict().items() if k != "errors"}
        )
        return result

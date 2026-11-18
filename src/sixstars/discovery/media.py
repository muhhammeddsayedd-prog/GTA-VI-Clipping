"""Console media from the platforms: creator profile pictures and source thumbnails (docs/26).

- `fill_avatars` looks up `creators.avatar_url` for creators that have never been looked up or whose last
  lookup is older than `console.avatar_refresh_days`, at most `console.avatars_per_pass` per call: Twitch
  Get Users `profile_image_url` (100 ids per request), YouTube `channels.list` `snippet.thumbnails` (50 ids
  per request, 1 quota unit each, inside `discovery.youtube_quota_daily_units`). `avatar_checked_at` is set
  for every creator the platform answered for, also when it had no picture, so a creator without one is
  not asked about again on every pass. A platform whose keys are missing, whose call fails or whose quota
  is used up is left for a later pass. Called at the end of each discovery pass and by
  `sixstars console-media-backfill`; it never raises into the discovery pass.
- `backfill_source_thumbnails` fills `videos.thumbnail_url` and `viewer_clips.thumbnail_url` for rows made
  before migration 0022 (Twitch Get Videos / Get Clips by id, YouTube `videos.list` part snippet). New rows
  get theirs when discovery stores them.

Thumbnail URLs are only ever taken from API answers: no URL form is documented for YouTube, and Twitch's
are templates the API hands out.
"""

import math
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db, settings
from sixstars.clients._http import ClientError
from sixstars.clients.twitch import IDS_PER_REQUEST_MAX as TWITCH_IDS_MAX
from sixstars.clients.twitch import VIDEO_THUMB_SIZE, TwitchClient, TwitchUser, fill_thumbnail
from sixstars.clients.youtube import CHANNEL_THUMB_PREFERENCE, QUOTA_LIST, YouTubeClient, pick_thumbnail
from sixstars.clients.youtube import IDS_PER_REQUEST_MAX as YOUTUBE_IDS_MAX
from sixstars.logging import get_logger

_log = get_logger("discovery")

# Asked with the number of YouTube quota units a call needs; True when they may be spent.
QuotaCheck = Callable[[int], bool]


@dataclass(frozen=True, slots=True)
class AvatarParams:
    """The `console.avatar_*` settings."""

    refresh_days: float
    per_pass: int

    @classmethod
    def load(cls) -> "AvatarParams":
        return cls(
            refresh_days=settings.get_float("console.avatar_refresh_days"),
            per_pass=settings.get_int("console.avatars_per_pass"),
        )


@dataclass
class MediaResult:
    """What one fill did: rows looked up and rows that got a URL, per platform, and quota spent."""

    checked: dict[str, int] = field(default_factory=dict)
    found: dict[str, int] = field(default_factory=dict)
    youtube_units: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, platform: str, checked: int, found: int) -> None:
        self.checked[platform] = self.checked.get(platform, 0) + checked
        self.found[platform] = self.found.get(platform, 0) + found

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": dict(self.checked),
            "found": dict(self.found),
            "youtube_units": self.youtube_units,
            "errors": list(self.errors),
        }


# ── pure helpers ─────────────────────────────────────────────


def batches[T](items: Sequence[T], size: int) -> Iterator[list[T]]:
    """`items` in consecutive lists of at most `size` (at least 1)."""
    step = max(1, size)
    for start in range(0, len(items), step):
        yield list(items[start : start + step])


def youtube_units(count: int) -> int:
    """Quota units of `list` calls for `count` ids (50 ids per call, 1 unit each)."""
    return math.ceil(max(0, count) / YOUTUBE_IDS_MAX) * QUOTA_LIST


def by_platform(rows: Iterable[dict[str, Any]], key: str) -> dict[str, list[str]]:
    """`{platform: [row[key], ...]}`, order kept, duplicates dropped."""
    out: dict[str, list[str]] = {}
    for row in rows:
        ids = out.setdefault(str(row["platform"]), [])
        value = str(row[key])
        if value not in ids:
            ids.append(value)
    return out


def twitch_avatars(users: Iterable[TwitchUser]) -> dict[str, str | None]:
    """`{user id: profile_image_url or None}` from Get Users answers."""
    return {str(user.id): (user.profile_image_url or None) for user in users}


def youtube_avatars(items: Iterable[dict[str, Any]]) -> dict[str, str | None]:
    """`{channel id: picture URL or None}` from `channels.list` items."""
    return {
        str(item.get("id")): pick_thumbnail(item, CHANNEL_THUMB_PREFERENCE)
        for item in items
        if item.get("id")
    }


# ── avatars ──────────────────────────────────────────────────


def due_creators(limit: int, refresh_days: float, now: datetime) -> list[dict[str, Any]]:
    """Creators never looked up first, then the longest unchecked, at most `limit`."""
    if limit <= 0:
        return []
    return db.fetch_all(
        """
        select id::text as id, platform, channel_id from creators
        where avatar_checked_at is null or avatar_checked_at < %s
        order by avatar_checked_at nulls first, discovered_at desc
        limit %s
        """,
        (now - timedelta(days=refresh_days), limit),
    )


def save_avatars(platform: str, found: dict[str, str | None], now: datetime) -> int:
    """Write the answers of one platform: the picture when there is one (an earlier picture is kept when
    the platform now has none) and `avatar_checked_at` for every channel answered. Returns rows changed."""
    if not found:
        return 0
    rows = [{"channel_id": channel_id, "url": url} for channel_id, url in found.items()]
    return db.execute(
        """
        update creators c set avatar_url = coalesce(k.url, c.avatar_url), avatar_checked_at = %s
        from jsonb_to_recordset(%s) as k(channel_id text, url text)
        where c.platform = %s and c.channel_id = k.channel_id
        """,
        (now, Jsonb(rows), platform),
    )


def fill_avatars(
    twitch: TwitchClient | None,
    youtube: YouTubeClient | None,
    now: datetime,
    *,
    quota_ok: QuotaCheck,
    limit: int | None = None,
    params: AvatarParams | None = None,
) -> MediaResult:
    """Look up the profile pictures of the creators that are due (see the module docstring). `limit`
    overrides `console.avatars_per_pass` (the backfill passes a larger one)."""
    params = params or AvatarParams.load()
    result = MediaResult()
    due = due_creators(params.per_pass if limit is None else limit, params.refresh_days, now)
    ids = by_platform(due, "channel_id")

    twitch_ids = ids.get("twitch", [])
    if twitch_ids and twitch is not None and twitch.is_configured():
        for chunk in batches(twitch_ids, TWITCH_IDS_MAX):
            try:
                answered = twitch_avatars(twitch.get_users(chunk))
            except ClientError as exc:
                result.errors.append(f"twitch: {str(exc)[:200]}")
                _log.warning("avatar_lookup_failed", platform="twitch", error=str(exc)[:200])
                break
            # Unknown or banned users are absent from the answer: they are checked, with no picture.
            found = {channel_id: answered.get(channel_id) for channel_id in chunk}
            save_avatars("twitch", found, now)
            result.add("twitch", len(chunk), sum(1 for url in found.values() if url))

    youtube_ids = ids.get("youtube", [])
    if youtube_ids and youtube is not None and youtube.is_configured():
        for chunk in batches(youtube_ids, YOUTUBE_IDS_MAX):
            units = youtube_units(len(chunk))
            if not quota_ok(units):
                result.errors.append("youtube: quota budget used up")
                break
            try:
                items = youtube.channels_list(chunk, parts=("snippet",))
            except ClientError as exc:
                result.youtube_units += units  # a refused call counts against quota too
                result.errors.append(f"youtube: {str(exc)[:200]}")
                _log.warning("avatar_lookup_failed", platform="youtube", error=str(exc)[:200])
                break
            result.youtube_units += units
            answered = youtube_avatars(items)
            found = {channel_id: answered.get(channel_id) for channel_id in chunk}
            save_avatars("youtube", found, now)
            result.add("youtube", len(chunk), sum(1 for url in found.values() if url))
    return result


# ── source thumbnails (backfill) ─────────────────────────────


def _save_urls(table: str, platform: str, found: dict[str, str]) -> int:
    if not found:
        return 0
    rows = [{"source_id": source_id, "url": url} for source_id, url in found.items()]
    return db.execute(
        f"""
        update {table} t set thumbnail_url = k.url
        from jsonb_to_recordset(%s) as k(source_id text, url text)
        where t.platform = %s and t.source_id = k.source_id and t.thumbnail_url is null
        """,  # noqa: S608 - the table name is one of two literals
        (Jsonb(rows), platform),
    )


def backfill_source_thumbnails(
    twitch: TwitchClient | None,
    youtube: YouTubeClient | None,
    *,
    quota_ok: QuotaCheck,
    limit: int,
) -> MediaResult:
    """Fill `videos.thumbnail_url` and `viewer_clips.thumbnail_url` where they are null, newest rows first,
    at most `limit` rows of each table. Rows the platform no longer has stay null."""
    result = MediaResult()
    videos = db.fetch_all(
        "select platform, source_id from videos where thumbnail_url is null "
        "order by created_at desc limit %s",
        (max(0, limit),),
    )
    ids = by_platform(videos, "source_id")
    twitch_ok = twitch is not None and twitch.is_configured()
    if ids.get("twitch") and twitch is not None and twitch_ok:
        try:
            for chunk in batches(ids["twitch"], TWITCH_IDS_MAX):
                found = {
                    video.id: url
                    for video in twitch.get_videos_by_id(chunk)
                    if (url := fill_thumbnail(video.thumbnail_url, *VIDEO_THUMB_SIZE))
                }
                _save_urls("videos", "twitch", found)
                result.add("twitch_videos", len(chunk), len(found))
        except ClientError as exc:
            result.errors.append(f"twitch videos: {str(exc)[:200]}")
    if ids.get("youtube") and youtube is not None and youtube.is_configured():
        for chunk in batches(ids["youtube"], YOUTUBE_IDS_MAX):
            units = youtube_units(len(chunk))
            if not quota_ok(units):
                result.errors.append("youtube: quota budget used up")
                break
            try:
                items = youtube.videos_list(chunk, parts=("snippet",))
            except ClientError as exc:
                result.youtube_units += units
                result.errors.append(f"youtube videos: {str(exc)[:200]}")
                break
            result.youtube_units += units
            found = {
                str(item["id"]): url for item in items if item.get("id") and (url := pick_thumbnail(item))
            }
            _save_urls("videos", "youtube", found)
            result.add("youtube_videos", len(chunk), len(found))

    clips = db.fetch_all(
        "select source_id from viewer_clips where thumbnail_url is null and platform = 'twitch' "
        "order by created_at desc limit %s",
        (max(0, limit),),
    )
    clip_ids = [str(row["source_id"]) for row in clips]
    if clip_ids and twitch is not None and twitch_ok:
        try:
            for chunk in batches(clip_ids, TWITCH_IDS_MAX):
                found = {
                    clip.id: clip.thumbnail_url
                    for clip in twitch.get_clips_by_id(chunk)
                    if clip.thumbnail_url
                }
                _save_urls("viewer_clips", "twitch", found)
                result.add("twitch_clips", len(chunk), len(found))
        except ClientError as exc:
            result.errors.append(f"twitch clips: {str(exc)[:200]}")
    return result

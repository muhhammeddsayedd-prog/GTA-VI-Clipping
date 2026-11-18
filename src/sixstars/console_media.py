"""One-time backfill of console media (`sixstars console-media-backfill`, docs/26).

Rows made before migration 0022 have no clip thumbnail, no source thumbnail and no creator picture. The
backfill fills them in three idempotent steps, each bounded by `limit`:

1. clip frames: `factory.thumbs.backfill_frames` (local file when it still exists, else a signed URL of
   the stored mp4 read by ffmpeg over https);
2. source thumbnails: `discovery.media.backfill_source_thumbnails` (Twitch by id, YouTube `videos.list`
   inside the discovery quota budget);
3. creator pictures: `discovery.media.fill_avatars` for every creator that is due, `limit` at a time.

Running it again only touches rows that are still missing something.
"""

from datetime import UTC, datetime
from typing import Any

from sixstars.clients.twitch import TwitchClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.discovery import media
from sixstars.discovery import store as discovery_store
from sixstars.discovery.config import DiscoveryConfig
from sixstars.factory import thumbs
from sixstars.storage import ClipStorage


def backfill(
    limit: int,
    *,
    twitch: TwitchClient | None = None,
    youtube: YouTubeClient | None = None,
    storage: ClipStorage | None = None,
    ffmpeg: str = "ffmpeg",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the three steps and return what each did."""
    config = DiscoveryConfig.from_settings()
    twitch = twitch or TwitchClient()
    youtube = youtube or YouTubeClient()

    def quota_ok(units: int) -> bool:
        return discovery_store.youtube_units_today() + units <= config.youtube_quota_daily_units

    frames = thumbs.backfill_frames(limit, storage=storage, ffmpeg=ffmpeg)
    sources = media.backfill_source_thumbnails(twitch, youtube, quota_ok=quota_ok, limit=limit)
    moment = now or datetime.now(UTC)
    avatars = media.MediaResult()
    while True:  # every creator that is due, `limit` per round, until a round finds nothing to do
        done = media.fill_avatars(twitch, youtube, moment, quota_ok=quota_ok, limit=limit)
        for platform, count in done.checked.items():
            avatars.add(platform, count, done.found.get(platform, 0))
        avatars.youtube_units += done.youtube_units
        avatars.errors.extend(done.errors)
        if not done.checked or done.errors:
            break
    return {"frames": frames, "sources": sources.as_dict(), "avatars": avatars.as_dict()}

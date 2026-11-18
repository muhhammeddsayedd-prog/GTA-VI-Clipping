# YouTube Data API quota fix (2026-11-18)

Owner's request: raise the internal YouTube cap, count the quota day from midnight Pacific, stop wasted
`channels.list` calls, deploy straight to production.

## Before the change (rollback point)

- Production ran `main` at `a0c740b3dadb7de54bfd02115e33df2dee128f92` (deploy run 2026-11-18 10:05 UTC), so
  the image was `IMAGE_TAG=prod-a0c740b3dadb7de54bfd02115e33df2dee128f92`.
- `settings` in production: `discovery.youtube_quota_daily_units = 3000`. The keys
  `discovery.youtube_quota_timezone` and `discovery.youtube_channel_cache_ttl_s` did not exist.
- No `.env` value changes.
- Rollback:
  1. On the server, set `IMAGE_TAG` in `/opt/6stars/.env` to the tag above and run `docker compose up -d`, or
     revert the merge on `main`.
  2. Then run `update settings set value = '3000' where key = 'discovery.youtube_quota_daily_units'`.
  3. The new table and settings are additive and can stay.

Baseline, production `api_usage` (`channel_feed` and yt-dlp excluded), 2026-11-18:

| | Per hour, 02:00–09:00 UTC | Per hour, 10:00–12:00 UTC (after the GTA 6 switch) | UTC day up to 13:35 |
|---|---|---|---|
| `channels.list` | 45–65 rows = 45–65 units | 78–128 rows ≈ the same in units | 942 rows, 943 units |
| `videos.list` | 10–11 rows, 68–71 units | 11–12 rows, 47–50 units | 145 rows, 860 units |

Failed jobs in the 6 hours before the change: 0. `creator.permission`: 450 done in 3 hours.

## Findings

- The accounting was already right:
  - `YouTubeClient._list` sends one request per 50 ids and writes one `api_usage` row per invocation, with
    `units` = number of requests. The dashboard's "calls" is `count(*)` of those rows.
  - "137 videos.list calls, 821 units" means one batched lookup per discovery pass, of about 250–300
    videos (about 6 requests).
  - Nothing is charged per id or per part.
- `search.list`: the official quota page (checked 2026-11-18) says it is in its own bucket of 100 calls a
  day, at 1 unit per call; it is no longer 100 units from the 10,000.
  - The code already did that, so it stays at 1 unit in its own bucket. This differs from the request,
    which said 100 units.
  - It is used as the discovery fallback when the yt-dlp search is refused (`discovery.search_api_*`,
    at most 60 a day).
  - It is never used to list a channel's uploads, so there is nothing to move to `playlistItems.list`.
- Where the `channels.list` rows came from:
  - Mostly `creator.permission` jobs: one channel per job and per retry, up to 25 jobs a pass.
  - Up to 3 small calls per pass: follower counts, the in-pass permission checks, and avatars.
- The over-budget behavior was only a log line plus a `skipped` source report. There was no alert and no
  pause. It is unchanged.

## What changed

- Migration `0024_youtube_quota.sql`:
  - Raises the cap to 9000, but only where it still held the seeded 3000.
  - Adds `discovery.youtube_quota_timezone = "America/Los_Angeles"` and
    `discovery.youtube_channel_cache_ttl_s = 3600`.
  - Adds table `youtube_channel_cache`.
- `discovery/quota.py`: pure `day_start` / `next_reset` for the quota day (zoneinfo, DST-correct).
- `discovery/store.py`:
  - `youtube_units_today()` and `youtube_search_calls_today()` count from that start, not from 00:00 UTC.
  - Cache helpers `cached_youtube_channels` / `save_youtube_channels`.
  - The money budgets keep their UTC day; their queries are separate and were not touched.
- `discovery/tiers.py`:
  - `youtube_channel_items` reads the cache first and fetches only the misses, in one batched call.
  - A channel the API does not return is cached as missing.
  - The follower counts, the permission checks and jobs, and the avatars (`media.fill_avatars`) all go
    through it.
- `discovery/run.check_permissions`: `prefetch_youtube_channels` makes one batched `channels.list` for the
  in-pass checks plus the creators about to get a job, so the jobs read their channel from the cache.
- Guardian (`guardian/claims.observe_youtube`):
  - The per-post OAuth `get_status` loop is now `YouTubeClient.get_statuses`: 50 ids per request, one row.
  - The precheck still uses `get_status` for its single video.
- Console: the YouTube quota labels say "resets 00:00 Pacific".
- Tests:
  - `tests/unit/discovery/test_youtube_quota.py`: DST in both directions, the midnight edges.
  - `tests/unit/discovery/test_youtube_channel_cache.py`: hit, miss, partial hit, stale, missing channel,
    broken cache, 120 creators in one prefetch.
  - `tests/unit/test_youtube_client.py`: 120 ids → 3 requests and 3 units; cost per request whatever the
    parts; documented costs; `get_statuses` batching.
  - `tests/integration/test_youtube_quota_db.py`: the Pacific window over real rows, the timezone setting,
    the cache table.

## Comparing with Google Cloud Console

Go to APIs & Services → YouTube Data API v3 → Quotas and look at "Queries per day" (the 10,000 bucket),
which resets at midnight Pacific. Our number is `youtube_units_today()`, which is also the Spend dashboard's
"YouTube Data API units".

The two count the same things:
- Every `*.list` row except `search.list`, from Pacific midnight.
- The owner-channel OAuth calls, which share the project.

The Console graph lags by a few minutes. Google also counts requests that failed before our client recorded
them, for example connection errors after the request reached Google. So expect our number to be within a
few percent of Google's and at most slightly lower. `search.list` and `videos.insert` show as their own
quota rows in the Console.

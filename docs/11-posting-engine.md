# 11 — Posting Engine

Two lanes, one cap manager, one publisher (Upload-Post).

## 1. Accounts (from `platform_accounts`)

| id | Platform | Upload-Post profile | Lanes | cap_24h | spike_reserve | Hourly hours (UTC) |
|---|---|---|---|---|---|---|
| `tiktok_main` | TikTok @6starsclips | `6stars-main` | hourly | 14 | 0 | 0,1,2,11,12,14,15,17,18,19,20,21,22,23 |
| `tiktok_live` | TikTok @6starsclips.live | `6stars-live` | spike | 14 | 14 | — |
| `instagram_main` | Instagram @6starsclips | `6stars-main` | hourly, spike | 45 | 10 | all 24 |
| `youtube_main` | YouTube @6StarsClips | `6stars-main` | hourly, spike | 40 | 8 | all 24 |

Why: TikTok's API allows about 15 posts/day/account (shared across all apps), so hourly and spike posts need separate TikTok accounts and TikTok main only takes the 14 best hours. Instagram via Upload-Post allows 50/24 h. YouTube's real limit is unpublished; 40 is a starting cap that the cap manager lowers automatically on "limit reached" errors.

## 2. Gate checks before any post (both lanes)

```
if settings.control.emergency_stop: stop
dry = settings.launch.dry_run or now < settings.launch.launch_at
account.enabled and (account.paused_until is null or < now)
```
In dry mode the publisher writes a `posts` row with `dry_run = true, status = skipped` and logs what it would have posted.

## 3. Cap manager (`posting/caps.py`)

For an account and lane at time `now`:
```
used_24h      = published posts (not dry) in the rolling 24 h
last_post_at  = most recent published post on the account
spikes_1h     = spike-lane posts in the rolling hour
free          = cap_24h − used_24h
hourly_allowed = free > spike_reserve_remaining         (hourly may not eat the spike reserve)
spike_allowed  = free > 0 and spikes_1h < posting.spike_max_per_hour
gap_ok         = last_post_at is null or now − last_post_at ≥ min_gap_s
```
`spike_reserve_remaining` = `spike_reserve − spike posts in the last 24 h` (floor 0). At 18:00 UTC each day, unused spike reserve beyond 4 is released to the hourly lane for the remaining hours.

On a platform "limit reached" / HTTP 429 response: set `paused_until` = now + 1 h (TikTok, Instagram) or the time the oldest post in the window turns 24 h old (YouTube), lower `cap_24h` by 2 for YouTube (min 10), event + alert.

## 4. Precedence rules (`posting/precedence.py`, pure functions, fully unit-tested)

| # | Situation | Rule |
|---|---|---|
| R1 | Spike clip approved | Try to post now on every account whose lanes include `spike` |
| R2 | Spike and hourly slot due together | Spike first; hourly waits for the next slot on that account |
| R3 | Hourly slot fires; a spike posted on this account < `skip_hourly_after_spike_s` (30 min) ago | Skip this slot on this account (event `posting.slot_skipped`); clip stays queued |
| R4 | Spike ready but `gap_ok` false | Wait until the gap is met (re-check every 30 s) up to `spike_max_wait_s` (15 min) |
| R5 | Two spikes ready together | Higher `rank_score` first; second waits for the gap |
| R6 | `spikes_1h ≥ spike_max_per_hour` | Spike demoted on that account only |
| R7 | Spike clip older than `spike_max_age_s` (30 min) since `spike_wall` | Lane becomes `hourly`, `rank_score` boosted ×1.5 |
| R8 | Account at cap | Skip that account only; others still post |
| R9 | Hourly lane: same creator as the last hourly post on this account | Pick the next clip |
| R10 | Hourly queue empty | Post the best unposted compilation if any; else skip the slot (event `posting.slot_empty`, alert if 2 in a row) |
| R11 | Emergency stop | Nothing posts |

Demotion means: the clip will not be posted on that account by the spike lane; it can still be posted to that account later by the hourly lane.

## 5. Ranking (`posting/ranking.py`)

```
age_h        = hours since clip approved (or since spike_wall for live clips)
half_life    = posting.freshness_half_life_h[live|vod]
freshness    = 0.5 ** (age_h / half_life)
spike_bonus  = 1.3 if tier S, 1.15 if A, 1.0 otherwise
rank_score   = score × freshness × spike_bonus × (1.1 if creator preferred else 1.0)
```
Recomputed every 10 minutes by the scheduler for approved clips; clips older than `posting.max_clip_age_h` → `expired`.

## 6. Hourly lane (`posting/hourly.py`)

Triggered by `POST /v1/posting/hourly` (n8n `WF02` at minute 0 each hour; failover by scheduler).
1. Record heartbeat `trigger:hourly`. Return 202 immediately.
2. Sleep a random 0..`jitter_max_s` (300 s).
3. For each enabled account with lane `hourly` (and, for `tiktok_main`, current UTC hour in `hourly_hours_utc`):
   a. Apply R3, R8, R11.
   b. Pick the highest `rank_score` approved clip with `lane = hourly` that has **not** been posted to this account, passes R9, and is not currently being posted elsewhere in this slot by a different clip choice. Preference: the same clip for all accounts in a slot (so each clip goes everywhere at once); take the top clip once and post it to every eligible account.
   c. Publish (§8).
4. Mark the clip `posted` when it has been published to every account that could take it in this slot.

## 7. Spike lane (`posting/spike.py`)

Called directly by the express worker after the gate passes (`try_post(clip)`):
1. For each enabled account with lane `spike`: apply R4–R8, R11.
2. Publish immediately where allowed; for accounts that need to wait for the gap, enqueue `post.retry` with `run_after` = gap end (only if within `spike_max_wait_s`).
3. If the clip could not be posted to any account → R7 demotion to hourly.
4. Set `spikes.spike_to_post_s` on first successful publish.

## 8. Publisher (`posting/publisher.py`)

Upload-Post `POST https://api.upload-post.com/api/upload` (multipart):

| Field | Value |
|---|---|
| Header `Authorization` | `Apikey <UPLOAD_POST_API_KEY>` |
| Header `Idempotency-Key` | `post:<clip_id>:<account_id>` |
| `user` | account.upload_post_user |
| `platform[]` | account.platform |
| `video` | the MP4 file (local `ready/` copy) |
| `title` | caption (TikTok/Instagram use it as caption); YouTube uses `youtube_title` |
| `youtube_title` | clip.title (≤ 100 chars) |
| `youtube_description` | caption + credit line + hashtags + `#Shorts` |
| `privacyStatus` | account.privacy for YouTube (`public` / `private` in staging) |
| `privacy_level` | account.privacy for TikTok (`PUBLIC_TO_EVERYONE` / `SELF_ONLY` in staging) |
| `media_type` | `REELS` (Instagram) |
| `first_comment` | `posting.first_comment` |
| `request_id` | `post:<clip_id>:<account_id>` |
| `async_upload` | `true`; then poll `/api/uploadposts/status?request_id=` (verify exact status path in docs) until done (max 10 min) |

Caption template (same for all platforms):
```
<hook-based caption line> 
🎥 @<creator> on <Twitch|YouTube>
<base_hashtags> <clip hashtags (max 5)>
```
Write `posts` row before the call (`pending`), update to `published` with `platform_post_id`/`post_url`, or `failed` with the error. Retries: 3 with backoff (30 s, 2 min, 5 min) using the same idempotency key.

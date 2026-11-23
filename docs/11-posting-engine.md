# 11 — Posting Engine

Two lanes, one cap manager, one publisher (Upload-Post).

## 1. Accounts (from `platform_accounts`)

| id | Platform | Upload-Post profile | Lanes | cap_24h | spike_reserve | Hourly hours (UTC) | hourly_min_gap_s |
|---|---|---|---|---|---|---|---|
| `tiktok_main` | TikTok @6starsclipping | `default` | hourly | 14 | 0 | 0,1,2,11,12,14,15,17,18,19,20,21,22,23 | — |
| `tiktok_live` | TikTok (disabled) | `default` | spike | 14 | 14 | — | — |
| `instagram_main` | Instagram @6starsclippy | `default` | hourly, spike | 45 | 10 | all 24 | — |
| `youtube_main` | YouTube @6starsClips-m3x | `default` | hourly, spike | 16 | 10 | 0,14,17,19,21,23 | 3000 |

Why: TikTok's API allows about 15 posts/day/account (shared across all apps), so TikTok main only takes the 14 best hours. Instagram via Upload-Post allows 50/24 h. YouTube's limit is set per channel by YouTube and is unpublished: the cap manager lowers `cap_24h` by 2 on every "limit reached" (floor 10). Migration 0034 (2026-11-21) raised it to 16 after the channel's ID verification, with 12 hourly hours (= 16 − 4 reserved for spikes) chosen for the audience (North America, Northern and Western Europe, Australia/NZ) and a 50-minute hourly gap. Migration 0056 (2026-11-23, D121) made spikes the main YouTube lane: 10 of the 16 slots are kept for spikes and the hourly lane posts in 6 of those hours (0, 14, 17, 19, 21, 23 UTC); the unused reserve is not released, so a day with fewer than 10 spikes posts fewer than 16. An account with hourly hours lists exactly `cap_24h − spike_reserve` of them (tested on the seeds).

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
hourly_gap_ok  = last_hourly_post_at is null or now − last_hourly_post_at ≥ account.hourly_min_gap_s
                 (hourly lane only; spike posts do not count, so a spike never pushes an hourly slot back
                 through it. null = no extra gap. 3000 s, not 3600, because slots fire 0..jitter_max_s late.)
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
| R12 | Several spike clips ready within a few minutes | Holding window: spike clips wait `posting.spike_hold_s` (7 min) after the first of them was approved, then the best-ranked posts first; the rest wait for the gap or move to the hourly queue boosted (R7). The release is never later than 2 min before a clip reaches `posting.spike_max_age_s`. |
| R13 | Hourly slot, the top clip went to another platform less than `posting.cross_platform_min_gap_s` (45 min) ago — including earlier in the same slot | The account takes its next best clip (reason `cross_platform_too_soon` when nothing is left). Platforms pick in `posting.platform_priority` order: YouTube, TikTok, Instagram (D116). Spike lane not affected. |
| R14 | Hourly slot, a short clip cut for the hourly lane does not fit the platform's hourly length window (`gate.hourly_len_by_platform`, YouTube 8–30 s; others `gate.hourly`) | The account takes its next best clip. Spike clips and story cuts are not judged by it. |

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
3. For each enabled account with lane `hourly` (and, where `hourly_hours_utc` is set — `tiktok_main`, `youtube_main` — current UTC hour in it; and `hourly_gap_ok`):
   a. Apply R3, R8, R11.
   b. Pick the highest `rank_score` approved clip with `lane = hourly` that has **not** been posted to this account, passes R9, and is not currently being posted elsewhere in this slot by a different clip choice. Preference: the same clip for all accounts in a slot (so each clip goes everywhere at once); take the top clip once and post it to every eligible account.
   c. Publish (§8).
4. Mark the clip `posted` when it has been published to every account that could take it in this slot.

## 7. Spike lane (`posting/spike.py`)

Called directly by the express worker after the gate passes (`try_post(clip)`):
1. For each enabled account with lane `spike`: apply R4–R8, R11, R12.
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

## Families of clips and platform lengths (development-plan/M18 Phase C)

- A short clip and its story cut form a family (`coalesce(clips.parent_clip_id, clips.id)`). The hourly lane does not give an account a clip of a family it posted less than `posting.sibling_gap_h` (12) hours ago.
- A clip longer than its platform takes (`posting.max_duration_s`: TikTok 600 s via the API and Instagram Reels 900 s per docs.upload-post.com/api/video-requirements, YouTube Shorts 180 s per YouTube Help) is not given to that platform's accounts.

## R15 Campaign lanes (D118, clipping campaigns PR 4)

Each account's day is shared by the Normal lane and the campaign lanes (`lane_quotas`: one row per account and open campaign, min/max posts a day). In an hourly slot (`posting/campaign_lane.order_candidates`, pure), an account tries:

1. campaign clips whose campaign is furthest below its daily minimum, while it is campaign hours for the platform (`lanes.campaign_hours_utc`: 08–22 UTC, YouTube 08–18);
2. Normal clips, which before `lanes.return_unused_at_utc` (18 UTC) leave room for the campaign minimums still open;
3. campaign clips of campaigns between their minimum and maximum.

A campaign clip is never posted as a Normal clip. It is skipped when:
- its campaign has no open lane on the account (paused, ended, budget gone, another platform);
- it is outside campaign hours;
- the campaign is at its daily maximum;
- the campaign posted on this account less than `lanes.campaign_gap_s` ago.

D116, caps, gaps and spike rules are unchanged. Spike posts of a campaign clip count toward its campaign's day.

Campaign posts:
- `posts.campaign_id` is copied from the clip.
- The publisher checks the campaign's status, dates and budget again right before sending (`campaign_closed: …`).
- Caption: clip line, credit, the brief's required line (CTA and mention), the brief's tags, then the base tags. Only the clip line is shortened.
- No first comment.
- Paid labels, per the Upload-Post docs of 2026-11-23:
  - YouTube: `hasPaidProductPlacement`.
  - TikTok: `brand_content_toggle`, left out on `SELF_ONLY` posts, which Upload-Post refuses.
  - Instagram: no API field; the label is set in the app.


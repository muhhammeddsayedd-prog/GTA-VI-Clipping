# M10 — Posting Engine (caps, precedence, ranking, lanes, publisher)

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 17:00–19:30 UTC · **Requires:** M07 · **Launch-critical:** yes

## Goal
Decide what posts where and when, and publish through Upload-Post — safely, idempotently, within every platform limit.

## Context you need
### Accounts (`platform_accounts`, seeded per environment)
| id | lanes | cap_24h | spike_reserve | hourly hours (UTC) |
|---|---|---|---|---|
| tiktok_main | hourly | 14 | 0 | 0,1,2,11,12,14,15,17,18,19,20,21,22,23 |
| tiktok_live | spike | 14 | 14 | — |
| instagram_main | hourly, spike | 45 | 10 | all |
| youtube_main | hourly, spike | 40 | 8 | all |
Why: TikTok API ≈ 15 posts/day/account shared across apps (so two TikTok accounts); Instagram via Upload-Post 50/24 h; YouTube's channel limit is unpublished (40 start, auto-lowered on errors). Staging uses `6stars-staging` profile, private privacy values, and `tiktok_live` disabled.

### Global posting guards
`control.emergency_stop` → nothing posts. `dry = launch.dry_run or now < launch.launch_at` → write `posts` rows with `dry_run = true, status = skipped`, publish nothing. Account must be enabled and not paused.

### Cap maths (rolling windows)
```
used_24h = published (non-dry) posts on the account in 24 h
free = cap_24h − used_24h
reserve_left = max(0, spike_reserve − spike posts on the account in 24 h)
hourly_allowed = free > reserve_left
spike_allowed = free > 0 and spike posts in the last hour < posting.spike_max_per_hour (2)
gap_ok = no published post on the account within posting.min_gap_s (900 s)
```
At 18:00 UTC daily: unused reserve beyond 4 is released to hourly for the rest of the day.
On "limit reached"/429: pause the account (`paused_until` now + 1 h for TikTok/Instagram; for YouTube until the oldest post in the window turns 24 h and lower `cap_24h` by 2, min 10); event + warn alert.

### Precedence rules (pure functions, table-tested)
R1 spike approved → try every spike-lane account now · R2 spike vs hourly at the same time → spike first · R3 hourly slot fires and a spike posted on that account < `posting.skip_hourly_after_spike_s` (1800 s) ago → skip slot on that account · R4 spike blocked by gap → wait (re-check every 30 s) up to `posting.spike_max_wait_s` (900 s) · R5 two spikes → higher rank first · R6 ≥ 2 spikes in the last hour → demote on that account · R7 spike clip older than `posting.spike_max_age_s` (1800 s) since the spike → lane becomes hourly, rank ×1.5 · R8 account at cap → skip that account only · R9 hourly: not the same creator as the account's previous hourly post · R10 hourly queue empty → best unposted compilation, else skip slot (event; alert if 2 in a row) · R11 emergency stop → nothing.

### Ranking
```
freshness = 0.5 ** (age_h / posting.freshness_half_life_h[live|vod])   (live 6 h, vod 24 h)
spike_bonus = 1.3 (S) | 1.15 (A) | 1.0
rank_score = score × freshness × spike_bonus × (1.1 if creator preferred)
```
Recomputed every 10 minutes (scheduler, M13); clips older than `posting.max_clip_age_h` (72) → `expired`.

### Hourly lane
Triggered at minute 0 (n8n) → `schedule_hourly_slot()` returns immediately, sleeps random 0..`posting.jitter_max_s` (300) in a background task, then: pick the top hourly clip (R9 per account), post it to every eligible hourly account (tiktok_main only in its hours), applying R3/R8/R11. Mark the clip `posted` once published everywhere it could go in this slot. Heartbeat `trigger:hourly` on call.

### Spike lane
`try_post_spike(clip_id)` called by the express worker after the gate: R4–R8/R11 per spike-lane account; publish now where allowed; enqueue `post.retry` (queue express, priority 90) with `run_after` = gap end where waiting is allowed; if nothing could post → R7 demotion. Record `spikes.spike_to_post_s` on the first publish.

### Publisher (Upload-Post)
- One `posts` row per clip×account, `idempotency_key = post:<clip_id>:<account_id>` (unique), written `pending` before the call.
- `POST /api/upload` fields: `user` (account.upload_post_user), `platform[]`, `video` (local `ready/` file), `title` (caption), `youtube_title` (clip.title ≤ 100), `youtube_description` (caption + credit + hashtags + `#Shorts`), `categoryId` "20", `privacyStatus` (YouTube, from account.privacy), `privacy_level` (TikTok, from account.privacy), `media_type` "REELS" (Instagram), `first_comment` (`posting.first_comment`), `request_id` = idempotency key, `async_upload` true; header `Idempotency-Key`.
- Poll status until done (≤ 10 min); store `platform_post_id`, `post_url`, `published_at`; failures → `failed` with error; retries 3× (30 s, 2 min, 5 min) with the same key.
- Caption (same everywhere): `<caption>\n🎥 @<creator> on <Twitch|YouTube>\n<posting.base_hashtags> <clip hashtags>` (YouTube adds `posting.youtube_extra_hashtags`).

## Build
`posting/caps.py`, `posting/precedence.py`, `posting/ranking.py`, `posting/captions.py`, `posting/publisher.py`, `posting/hourly.py`, `posting/spike.py`; job handler `post.retry`; CLI `sixstars post hourly [--dry-run]` and `sixstars post clip <clip_id> --account <id>` (staging only).

## Acceptance tests
- Table tests for every rule R1–R11 and the 10 cases in `docs/19` §3 (spike at slot; spike 10 min after a post waits 5; spike 20 min before slot skips it; third spike demoted; stale spike becomes hourly; TikTok at cap skipped; reserve protected; stop; dry run; creator alternation).
- Ranking maths; expiry.
- Idempotency: calling publish twice for the same clip/account creates one post.
- Staging real: with `launch.dry_run=false` temporarily, post one approved clip privately to all staging accounts; confirm on each platform that it's private; restore dry run.

## Definition of done
Posting engine merged; private staging post confirmed on all three platforms (screenshots/links in PROGRESS.md).

## Handoff
M11 exposes the hourly trigger; M14's express worker calls `try_post_spike`; M13 guardian reads `posts`.

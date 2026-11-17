# M13 — Guardian, Stats, Daily Report, Failover

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 23:00–Wed 01:30 UTC · **Requires:** M12 · **Launch-critical:** yes

## Goal
Keep the accounts safe and the system alive with nobody watching: detect claims and removals, act on takedown emails, stop posting after repeated strikes, self-heal common failures, run schedules itself if n8n goes quiet, and report the KPIs daily.

## Context you need
### Claims/removals (`POST /v1/guardian/check`, n8n every 15 min in launch week)
For published posts younger than `guardian.check_lookback_h` (72 h):
- YouTube: `videos.list?part=status,contentDetails&id=…` (API key, 50 ids/call). Problem if missing, `uploadStatus=rejected`, `regionRestriction.blocked` non-empty, or no longer public.
- TikTok/Instagram: Upload-Post post analytics for the post (⚠️ which id — see M02 notes); problem if it reports removed/not found.
On a problem: set `posts.claim_status`; insert `takedowns` (source `claim`/`platform`); creator `claims_count+1` and tier `blocked`; remove the creator's other posts; count strikes in `guardian.strike_window_d` (7 d); if ≥ `guardian.strike_limit` (2) → emergency stop; critical alert.

### Takedown emails (`POST /v1/guardian/takedown`, n8n Gmail trigger)
Body `{from, subject, body_text, received_at, message_id}` (idempotent on message_id). P7 classifies. Not a takedown → `ignored` (info). Takedown → match creators by handle/URL (fuzzy on handles; exact on URLs of our posts or the creator's channel), block them, remove all their posts, count a strike, critical alert listing actions.

### Removing posts
YouTube: `clients.youtube.delete_video` with the owner's OAuth (works on any video on the owner's channel). TikTok/Instagram: `upload_post.delete_post` if M02 found a delete endpoint; otherwise mark `remove_failed` and send a critical alert with direct links for manual removal (the only possible human step).

### Emergency stop
Set `control.emergency_stop = true` + `control.stop_reason`; posting halts; discovery/rendering continue; critical alert. Cleared only by `/start` (WF08) or the CLI.

### Health (scheduler loop, every 60 s)
Heartbeats older than `guardian.heartbeat_stale_s` (300 s) → warn · ≥ 3 restarts of a service in 1 h (heartbeat `info.started_at`) → critical · running jobs older than `guardian.stuck_job_s` (7200 s) → reset/fail · approved hourly clips < `posting.queue_low_watermark` (12) → warn (discovery already widens) · `spike_to_post_s` > `guardian.express_slow_s` (360) three times in a row → warn · 5 consecutive yt-dlp/streamlink failures (count in `events`) → upgrade tools in the worker container (`pip install -U yt-dlp streamlink` via a job each worker runs), alert if still failing · daily spend per provider over budget → pause VOD lane (`vod.paused_until` setting) + warn · `/data` > 80% → aggressive cleanup + warn · unsent `notify_outbox` rows → retry.

### Failover
If heartbeat `trigger:hourly` older than `guardian.failover_grace_s.hourly` (4200 s) → scheduler calls `schedule_hourly_slot()` itself and alerts; same for discovery (1500 s). Repeats until n8n triggers resume.

### Other scheduler duties
Ranking refresh every 10 min (M10); expire clips > 72 h; cleanup: Storage files 3 days after posting (`cleanup.posted_clip_days`), rejected after 24 h, local `ready/` files after posting, chat files after 7 days, prune `candidates`/`events` > 30 days; at 10:45 UTC on launch day post a health summary.

### Stats (`POST /v1/stats/collect`, every 2 h)
- Account level: Upload-Post `GET /api/analytics/{profile}?platforms=…` → `account_stats` (followers, views, reach, impressions) per account; YouTube `channels.list statistics` as a cross-check.
- Post level: YouTube `videos.list statistics` (views, likes, comments); TikTok/Instagram via Upload-Post post analytics → update `posts`.
- Update `creators.avg_clip_views`.

### Daily report (`GET /v1/reports/daily?format=telegram|json`, 00:00 UTC)
Followers per account (+change), total views (+change), median views per video per platform, top clip, YPP progress (subscribers vs 1,000; Shorts views in last 90 days vs 10M — from `reports.ypp_targets`), posts (hourly slots used/skipped, spikes, failures), spike funnel (detected/S/A/B/rejected, median spike→post), clips (rendered/approved/rejected by reason), discovery (slots used, videos processed, new creators by tier), guardian (claims, takedowns, strikes, stop state), spend per provider, issues (cap changes, pauses). HTML-safe for Telegram. Example layout: `docs/17-control-api.md`.

## Build
`guardian/claims.py`, `guardian/takedowns.py`, `guardian/stop.py`, `guardian/health.py`, `guardian/failover.py`, `stats/collector.py`, `stats/report.py`, full `services/scheduler.py`, API routes (replace M11 placeholders). Activate WF03–WF06 in n8n.

## Acceptance tests
- Unit: strike counting window; takedown matching; report formatting (snapshot); failover timing logic; stale-heartbeat detection.
- Staging: a private staging YouTube post marked as "problem" via a fake → creator blocked, video deleted, alert received; 2 such events → emergency stop; `/start` clears it.
- Send a real email to the takedown inbox naming a test creator → actions + alert within ~5 min.
- Stop WF02 for 75 min → failover posts the slot and alerts.
- `/v1/reports/daily?format=telegram` returns a full report with real staging numbers; WF06 delivers it.

## Definition of done
Guardian, stats, report and failover live in staging; n8n WF03–WF06 active.

## Handoff
M14 adds the live lane; M15 runs the full acceptance checklist.

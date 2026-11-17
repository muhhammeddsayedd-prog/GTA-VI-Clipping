# M16 — Production Deploy, Dry Run, Launch

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Wed 07:00–11:00 UTC (launch at **11:00 UTC**) · **Requires:** M15 · **Launch-critical:** yes

## Goal
Bring up production with identical code, fill the queue with GTA 6 clips in dry-run mode, and let posting go live automatically at 11:00 UTC.

## Context you need
- Production: Hetzner 16 dedicated vCPU / 32–64 GB, ≥ 500 GB disk; Supabase project `6stars-production` (migrated and seeded with `supabase/seed/production.sql` in M01); Upload-Post profiles `6stars-main` (TikTok @6starsclips, Instagram @6starsclips, YouTube @6StarsClips) and `6stars-live` (TikTok @6starsclips.live); domain `api.<domain>`.
- Settings that must be right in production: `env.target_game = "Grand Theft Auto VI"` (⚠️ confirm the exact Twitch category name with `GET /helix/games?name=` — it may be listed as "Grand Theft Auto VI"; adjust if not), `env.search_terms` GTA 6 terms, `launch.launch_at = "2026-11-18T11:00:00Z"`, `launch.dry_run = true` (until the go decision), `control.emergency_stop = false`, `live.max_slots = 6` (or 0 if M14 isn't ready), plus the tuning changes from M15.
- Before launch, GTA 6 isn't released: content is trailer reactions, countdown and preload streams. If supply is thin, widen terms ("GTA 6 trailer", "GTA 6 reaction", "GTA 6 countdown") and, for launch day only, lower `discovery.vod_score_cutoff` to 35 and `gate.hourly.min_score` to 6.
- Launch runbook: `docs/20-launch-runbook.md`.

## Steps
1. **07:00** Provision the production server with `scripts/provision_server.sh`; put `/opt/6stars/.env` (production values, `APP_ENV=production`).
2. Merge `develop` → `main`; approve the production deployment in GitHub; confirm `https://api.<domain>/v1/health`.
3. Apply M15 setting changes to production; double-check the launch settings above.
4. `sixstars youtube whoami` against the production channel (refresh token works). Upload-Post analytics call for `6stars-main` and `6stars-live` works.
5. Duplicate the n8n workflows as `[PROD]` with the production API base, production notify webhook path `6stars-notify-production`, production credentials; WF08 Telegram commands on production. Activate all. Put the production notify URL/secret in `.env`; redeploy (or restart containers).
6. `sixstars notify test` → production alert arrives.
7. **07:30–10:30** Dry run: discovery every 10 min; live slots recording (if M14); VOD lane processing; dry-run `posts` rows at 08:00, 09:00, 10:00. Watch `/v1/status`. Target ≥ 24 approved hourly clips by 10:30.
8. Capacity check: 6 live + VOD for 2 h — express p90 < 6 min, CPU < 90% average.
9. **10:30 go decision**: if green, set `launch.dry_run = false` (posting still waits for `launch_at`). If not green, fix or reduce scope (`live.max_slots = 0`), but keep the launch time.
10. **10:45** scheduler posts the launch health summary to Telegram.
11. **11:00** hourly slot fires and posts to TikTok main, Instagram, YouTube; spike lane live. Verify the first post on each platform is public and correct (open the links in the `posts` table).
12. Update PROGRESS.md: launch done, first post links, open issues.

## Acceptance
- First hourly post published on all three hourly accounts between 11:00 and 11:05 UTC.
- No dry-run rows after 11:00; no errors in the first two slots.
- If M14 is live: first spike-lane post within the first hours of streams.

## After launch (not blocking)
- Finish M14 if it was deferred; re-enable `live.max_slots`.
- 2026-11-25: switch WF03 to hourly.
- 2026-12-03: spoiler block ends automatically (`gate.spoiler_block_until`).
- Later phases: `docs/25-later-phases.md`.

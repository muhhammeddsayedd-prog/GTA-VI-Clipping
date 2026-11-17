# M15 — Staging Acceptance (end-to-end)

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Wed 05:00–07:00 UTC · **Requires:** M13 (+ M14 if ready) · **Launch-critical:** yes

## Goal
Prove the whole system works unattended in staging before anything touches production. Fix what fails. Produce a signed-off checklist.

## Context you need
Staging = GTA 5 content, private test accounts (`6stars-staging` Upload-Post profile, TikTok `SELF_ONLY`, YouTube `private`, Instagram private account), n8n `[STG]` workflows, Telegram alerts prefixed `[STG]`. For this milestone set `launch.dry_run = false` in staging so private posts actually happen; set it back to `true` at the end.

## Checklist (record pass/fail + evidence in PROGRESS.md)

| # | Check | Pass when |
|---|---|---|
| A1 | Discovery | ≥ 20 live candidates and ≥ 10 videos per run; new creators get tiers |
| A2 | Live recording (if M14) | 3 streams × 2 h unattended; buffer ≤ 30 min |
| A3 | Live spikes (if M14) | ≥ 5 confirmed spikes in 2 h; replay finds the same top spikes |
| A4 | Express (if M14) | ≥ 3 S-tier private posts, median spike→post < 4 min |
| A5 | VOD lane | One 4–6 h stream → ≥ 8 approved clips; ≥ 70% good on spot check |
| A6 | Render quality | Subtitles synced, highlight + keyword pop visible, safe zones respected, bleeps correct, layouts A and B correct, end card shows series number |
| A7 | Gate | Black frames, frozen video, music, watermark, spoiler phrase and low score each rejected with the right reason |
| A8 | Pre-check | A clip with copyrighted music is caught by the private YouTube pre-check (owner provides a test clip) |
| A9 | Hourly posting | 6 consecutive hourly slots posted privately to all staging accounts (jitter 0–5 min) |
| A10 | Precedence | A spike within 30 min before a slot causes the skip; min gap respected; 3rd spike in an hour demoted (simulate with `sixstars post` test commands if M14 isn't live) |
| A11 | Caps | Set staging `tiktok_main.cap_24h = 2` → TikTok skipped after 2, others continue; restore |
| A12 | Guardian | Fake takedown email → creator blocked, private YouTube post deleted, critical alert; 2 problems → emergency stop; `/start` clears (if WF08 exists for staging; else CLI) |
| A13 | Failover | WF02 disabled for 75 min → scheduler posts the slot and alerts; re-enable |
| A14 | Crash recovery | `docker kill` each service in turn → restarts, no duplicate posts (`posts.idempotency_key` unique) |
| A15 | Report | `/v1/reports/daily` and WF06 deliver a complete report with real numbers |
| A16 | Spend | 24 h staging spend within budgets; `api_usage` populated for every provider |
| A17 | Load | 3 live (if M14) + 2 VOD jobs + injected express renders every 2 min for 1 h: express p90 < 6 min, no stuck jobs, CPU avg < 90% |

## Build / fix
Fix every failure found, with tests for each bug. Small tuning (thresholds) goes into settings, not code. Keep a list of setting changes to copy to production.

## Definition of done
All non-live checks pass (A1, A5–A17), live checks pass if M14 is done. PROGRESS.md contains the completed table, the setting changes to carry to production, and a go/no-go note. `launch.dry_run` set back to `true` in staging. Merge `develop` → `main` is now allowed.

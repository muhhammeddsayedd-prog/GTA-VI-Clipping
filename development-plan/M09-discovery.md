# M09 — Discovery Engine + Creator Tiers

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 15:30–17:00 UTC · **Requires:** M08 · **Launch-critical:** yes

## Goal
Every 10 minutes, find the most-trending live streams and recent videos for the target game on Twitch and YouTube, score them, register creators with permission tiers, assign live recording slots and queue videos for the VOD lane. No seed list: the system finds creators itself.

## Context you need
- Target game and terms are settings: `env.target_game` (staging "Grand Theft Auto V", production "Grand Theft Auto VI"), `env.search_terms`.
- Sources: Twitch Helix streams by game id (paginate to 300), Twitch archives by game (`period=day`, `sort=views`), YouTube live + recent via Apify (async: start a run each pass, read the previous run's results; store last run id in settings `discovery.apify_last_run`), YouTube RSS for known creators.
- Trend score (0–100), weights in settings:
  - Live: viewers `min(1, log10(v+1)/5)` 35% · growth vs 30 min ago from `candidates` `clamp((now/then−1)/1, 0, 1)` (0.5 if unknown) 25% · chat rate 20% (0.5 proxy if unknown) · global reach 10% (1.0 English, 0.6 otherwise, +0.2 if `audience_spread` > 0.5, cap 1) · history 10% (percentile of `avg_clip_views`, 0.5 unknown, −0.5 per claim, floor 0).
  - Video: views/hour `min(1, log10(vph+1)/5)` 45% · engagement `(likes+2·comments)/views` scaled 0.08→1, 15% · size ratio `views/max(followers,1000)` scaled 1→1, 15% · global reach 10% · history 15%. +5 points if the video has a chat replay (Twitch VOD or YouTube past live).
  - × `discovery.non_english_penalty` (0.85) if not English; + `discovery.preferred_bonus` (15) for preferred creators; skip titles containing any `discovery.title_blocklist` term or "leak"; skip videos shorter than `discovery.min_vod_duration_s`.
- Tiers: new creators `auto`; P6 (permission_read) on the channel description → `preferred` / `auto` / `restricted`; re-check every 14 days; `restricted` and `blocked` are never clipped.
- Live slots: rank live candidates with score ≥ `discovery.live_score_cutoff` (55); one source per creator; keep current sessions unless below cutoff for `discovery.release_after_below_s` (1200 s, tracked with `below_cutoff_since`); fill up to `live.max_slots` → insert `live_sessions` (status `assigned`); set dropped ones to `released`. (The live-manager that acts on these arrives in M14; until then assignments simply accumulate — set `live.max_slots = 0` in staging until M14 if you prefer.)
- VOD queue: score ≥ `discovery.vod_score_cutoff` (50), not already in `videos`, up to `discovery.max_vod_per_run` (6) → `videos` rows + `vod.process` jobs (priority 30, `dedupe_key vod:<id>`). If approved hourly clips < `posting.queue_low_watermark` (12): double the per-run max and lower the cutoff by 10 for this run.
- Every observation goes into `candidates`. Heartbeat `trigger:discovery`. Event `discovery.run` with counts.

## Build
1. `discovery/sources.py` — fetchers returning normalised `Candidate` objects.
2. `discovery/scoring.py` — pure scoring functions (unit-tested).
3. `discovery/tiers.py` — creator upsert, permission read job (`creator.permission` job type, queue normal, priority 20).
4. `discovery/assign.py` — live slot logic (pure function on current sessions + ranked candidates → actions).
5. `discovery/run.py` — `run_discovery() -> DiscoverySummary` (≤ 60 s).
6. CLI `sixstars discovery run-once` prints the summary and top 10 live/video candidates with scores.

## Acceptance tests
- Unit: scoring maths with fixed inputs; title blocklist; non-English penalty; slot assignment (keep, release after 20 min below cutoff, one source per creator, fill to max).
- Integration (fakes): a run creates creators, candidates, sessions and video jobs; second run 10 minutes later computes growth from history.
- Real (staging): `sixstars discovery run-once` finds ≥ 20 live GTA 5 streams and ≥ 10 videos; P6 tiers a sample of creators (check a couple by hand); queued videos flow through the VOD lane into approved clips.

## Definition of done
Discovery merged and producing VOD work automatically in staging.

## Handoff
M11 exposes `run_discovery` at `POST /v1/discovery/run`; M12 schedules it every 10 minutes in n8n; M14 consumes the live assignments.

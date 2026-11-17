# M08 — VOD Lane (chat-first + transcript fallback)

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 13:30–15:30 UTC · **Requires:** M04, M07 · **Launch-critical:** yes (it feeds the hourly lane)

## Goal
Process finished streams and uploads into approved hourly clips, reading the chat first and downloading only the minutes that matter.

## Context you need
- A `videos` row (status `new`) is created by discovery (M09) or by the live lane after a stream ends (M14, job `replay.scan`). Until M09 exists, create rows with the CLI.
- Chat-first: a 6-hour stream's chat is a few MB and downloads in seconds; the spike engine (M04 `scan_replay`) finds 10–40 spikes; P1 confirms them (batched); yt-dlp downloads only `[pad_start, pad_end]` of each; each becomes a `clip.render` job (M06 factory → M07 gate).
- No chat (normal YouTube uploads) → fallback: download ≤ 1080p, transcribe locally, P3 picks 3–8 moments, cut locally, render.
- Replay pass after a live stream: skip ranges in `live_sessions.clipped_ranges` (wall clock → VOD offset `offset = wall − live_sessions.started_at`, ±30 s). Replay spikes never go to the spike lane (S is routed like A).
- Settings: `vod.max_spikes_per_video` (25), `vod.fallback_moments` ([3,8]), `vod.whisper_model`, `spikes.*`, `discovery.min_vod_duration_s`.
- Priorities: render jobs 55 for S/A, 40 for B; `dedupe_key = render:<spike_id>`.
- Proxy: `PROXY_URL` for yt-dlp only for platforms in setting `downloads.proxy_platforms` (default `["youtube"]`; YouTube blocks data-centre IPs, Twitch usually doesn't). Max height: `downloads.max_height` (1080).

## Build
1. `vod/process.py` — handler for `vod.process` and `replay.scan`:
   1. Mark `processing`; attempts + 1.
   2. Get chat (`chat.twitch_replay` / `chat.youtube_replay`); save `chat_files` row; learn bots.
   3. `scan_replay` → keep top N by score; drop overlaps with clipped ranges; persist spikes.
   4. `confirm_spikes` in batches of 10 (respect `budget_ok("vod")`; if over budget → re-queue the job with `run_after` = next midnight UTC).
   5. For confirmed spikes: `vod/sections.py` downloads the padded window: `yt-dlp --download-sections "*<a>-<b>" --force-keyframes-at-cuts -f "bv*[height<=1080]+ba/b[height<=1080]" [--proxy] [--cookies] -o <dir>/<spike_id>.%(ext)s <url>`; enqueue `clip.render` with payload `{source_path, window_start_s, window_end_s, spike_floor_s (all relative to the downloaded file), spike_id, creator_id, lane:"hourly"}`.
   6. Mark `done` (or `failed` after 3 yt-dlp failures; event + continue).
2. `vod/fallback.py` — full download, local transcription, chunk to 30-second blocks, P3 picks, persist spikes with mode `transcript`, cut with ffmpeg, enqueue renders.
3. CLI: `sixstars vod process <url>` (creates the creator/video rows if missing, runs the job inline), `sixstars vod scan-only <url>` (chat + spikes table, no downloads).
4. Worker wiring: register `vod.process`, `replay.scan`, `clip.render`, `clip.precheck`, `clip.precheck_result` handlers in `services/worker.py` for queue `normal`; `express.render` for queue `express`. `sixstars worker --queue normal` now runs real jobs.

## Acceptance tests
- Unit: clipped-range conversion and overlap filtering; section maths; budget re-queue.
- Integration (fakes): a `vod.process` job with a fixture chat produces spikes, render jobs with correct payloads, and status `done`.
- Real (staging): `sixstars vod process <a 4–6 h GTA 5 Twitch VOD URL>` → ≥ 8 approved clips within 45 minutes; one YouTube past-live stream with chat replay works; one normal YouTube upload goes through the fallback.
- Spot check: watch the approved clips; ≥ 70% are genuinely good (record notes in PROGRESS.md and tune `spikes.lead_s` / `live.chat_offset_s` if starts are consistently early/late).

## Definition of done
VOD lane produces approved hourly clips unattended from a URL; numbers and tuning notes in PROGRESS.md.

## Handoff to M09
Discovery will create `videos` rows and enqueue `vod.process` jobs automatically.

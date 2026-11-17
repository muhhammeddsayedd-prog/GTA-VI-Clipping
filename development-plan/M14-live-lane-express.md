# M14 — Live Lane + Express Path

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Wed 01:30–05:00 UTC · **Requires:** M13 · **Launch-critical:** no — if not passing by Wed 05:00, launch without it and finish after launch (it joins without downtime)

## Goal
Record the top live streams, detect chat spikes in real time, and get S-tier moments posted within ~4 minutes; A/B moments go to the hourly queue; when a stream ends, hand its replay to the VOD lane.

## Context you need
- Discovery (M09) writes `live_sessions` with status `assigned` (start recording) or `released` (stop). Max `live.max_slots` (6 production, 3 staging).
- Chat adapters (M03): `chat.twitch_irc.stream(login)`, `chat.youtube_live` iterator. Spike engine (M04): `LiveSpikeDetector.feed/tick` (tick every 5 s); P1 confirmation (M05) — **chat sample only** in live mode (no transcript, for speed).
- Factory (M06) + gate (M07) + spike lane (M10 `try_post_spike`).
- Routing: S → job `express.render` (queue express, priority 100); A → `clip.render` (normal, 60); B → `clip.render` (normal, 40).
- Express target timings: extract 10 s · hosted transcription 20 s · P2 30 s · facecam (cached) 0–15 s · render ≤ 90 s · gate 15 s · post 20 s. Record `spikes.spike_to_post_s`.
- Express workers are never paused by CPU or spend rules.

## Build
1. `live/recorder.py` — per session: `streamlink --stdout [--http-proxy if platform in downloads.proxy_platforms] --twitch-disable-ads <url> 1080p60,1080p,936p60,720p60,720p,best | ffmpeg -hide_banner -loglevel error -i pipe:0 -c copy -map 0 -f segment -segment_time <live.segment_s> -reset_timestamps 1 -strftime 1 <DATA_DIR>/live/<session_id>/seg_%Y%m%dT%H%M%S.ts` (container TZ=UTC so names are UTC). Reconnect up to `live.max_reconnects` with 10 s backoff; update `reconnects`.
2. `live/buffer.py` — segment index (start time from file name, end = next start); janitor deletes segments older than `live.buffer_s` unless listed in a `.pin` file; `extract(session, start_wall, end_wall) -> path` (concat demuxer + trim; waits up to 70 s for the closing segment).
3. `live/session.py` — one asyncio task group per session: recorder process + chat listener + detector tick loop; learns bot names in the first 10 minutes (store on the session); on spike: persist (`spike_wall` from peak, `window_*_wall`), confirm with P1, pin segments, enqueue the render job with payload `{session_id, spike_id, start_wall, end_wall, spike_floor_wall, pad_start_wall, pad_end_wall, lane}`; after the clip renders, append to `clipped_ranges`.
4. `live/manager.py` (`sixstars live-manager`) — every 15 s: start `assigned` sessions (→ `recording`, `started_at`), stop `released` ones (→ `ended`), detect offline streams (recorder exit + platform says offline) → `ended`; heartbeat with active sessions. On `ended`: schedule `replay.find` job (normal, priority 25) that polls Twitch `videos?user_id=&type=archive&first=1` (match `stream_id`) every 10 min for up to 2 h, or for YouTube uses the same video id once `liveStreamingDetails.actualEndTime` is set → creates/attaches a `videos` row with `live_session_id`, sets session `replay_queued`, enqueues `replay.scan` (M08 handles it, skipping clipped ranges). Delete the buffer directory.
5. `express.render` handler: extract from buffer → factory in express mode (hosted transcription, `factory.preset_express`, LIVE badge, lane spike) → express gate (with hourly fallback) → `try_post_spike` → record latency.
6. CPU guard: if host CPU > 85% for 2 min (read `/proc/stat`), normal workers stop claiming `vod.process` until it drops below 70%.

## Acceptance tests
- Unit: segment index/extract maths; pin/janitor; session state transitions; replay matching by `stream_id`.
- Staging (GTA 5, 3 slots): record 3 streams for 2 hours unattended; buffer stays ≤ 30 min; ≥ 5 confirmed spikes; ≥ 3 S-tier clips posted privately with median spike→post < 4 min (record p50/p90); after the streams end, replay scans run and find the same top spikes (± one bucket) without duplicating the live clips.
- Kill `live-manager` mid-recording → it restarts and resumes sessions (sessions in `recording` are restarted).

## Definition of done
Live lane running in staging; latency numbers in PROGRESS.md. If not done by Wed 05:00 UTC: set `live.max_slots = 0` in production for launch, note it in PROGRESS.md, and continue this milestone after launch.

## Handoff to M15
Include live checks A2–A4 in the acceptance run if this milestone is done.

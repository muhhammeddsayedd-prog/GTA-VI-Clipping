# M05 — AI Layer (prompts, schemas, calls, cost tracking)

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 06:00–07:30 UTC · **Requires:** M02 · **Launch-critical:** yes

## Goal
All Claude-powered decisions as typed, tested functions: spike confirmation, clip writing, fallback moment picking, layout detection, visual checks, permission reading, takedown classification and translation.

## Context you need
- Models are settings: `ai.model_writer` = `claude-sonnet-5-5`, `ai.model_fast` = `claude-haiku-5-5`.
- `clients.anthropic_client.complete_json(...)` (M02) handles the API call, JSON parsing, one repair retry and cost logging.
- Budget: `ai.daily_budget_usd` (25 in production). VOD-lane calls check `budget_ok("vod")`; express calls always run.
- Brand voice: English, global audience, **hype** tone, no clickbait lies, never spoilers in hooks/titles, never slurs.
- Prompt texts and exact JSON schemas are in `docs/15-claude-prompts.md` (P1–P8). Copy them into files; that doc is the single source of truth for wording.

## Build
1. `ai/prompts/` — one Markdown file per prompt: `p1_spike_confirm.md`, `p2_clip_writer.md`, `p3_vod_moment_picker.md`, `p4_layout_detect.md`, `p5_visual_check.md`, `p6_permission_read.md`, `p7_takedown_classify.md`, `p8_translate_lines.md`. Each has a `## System` and `## User template` section; templates use `{placeholders}` filled with `str.format_map` (escape JSON braces).
2. `ai/schemas.py` — Pydantic models matching each JSON schema in docs/15 exactly, with validators:
   - P2: `hook` ≤ 8 words; `title` ≤ 70 chars, no `#`; `caption` ≤ 120 chars; `hashtags` 3–5 items, lowercase, no spaces/`#` (code adds `#`); `score` 1–10; `start_s < end_s`.
   - P1: `confidence` 0–1.
   - P4: boxes non-negative ints inside the frame.
3. `ai/calls.py` — one function per prompt:
   - `confirm_spikes(spikes: list[SpikeContext]) -> list[SpikeVerdict]` (batches up to 10 in replay; single in live; fast model).
   - `write_clip(ctx: ClipContext) -> ClipWriting` (writer model; prompt caching on the system block). `ClipContext` includes: creator handle/platform, stream title, P1 verdict, chat sample, numbered subtitle lines with word timings, window bounds, `spike_floor_s`, min/max length, spoiler policy (now, `gate.spoiler_block_until`), `gate.reject_topics`.
   - `pick_moments(transcript_chunks, n_min, n_max, len_min, len_max) -> list[Moment]`.
   - `detect_layout(frame_jpeg: bytes, w, h) -> Layout`.
   - `visual_check(frames: list[bytes]) -> VisualVerdict`.
   - `read_permission(description, name, titles) -> PermissionVerdict`.
   - `classify_takedown(sender, subject, body) -> TakedownVerdict`.
   - `translate_segments(segments, source_lang) -> list[TranslatedSegment]`.
   Images: resize to ≤ 1280 px long side, JPEG quality 85, base64 image blocks.
4. `ai/costs.py` — `today_spend(provider)`; used by budget checks and the daily report.
5. `spikes/confirm.py` — glue: applies P1 results to `DetectedSpike` per the tier rules: `is_moment=false` → rejected; provisional S needs confidence ≥ `spikes.s_min_confidence` (0.8) else becomes A; A/B need ≥ `spikes.ab_min_confidence` (0.6) else rejected. Updates the `spikes` row (tier, confidence, reason, status confirmed/rejected).
6. Tests: each call with the fake client returning canned JSON (valid, invalid-then-valid, invalid twice → error); validators; tier rules in `spikes/confirm.py`.
7. A golden-sample script `scripts/ai_smoke.py` (live, manual) that runs P1 and P2 on the real spike found in M04 and prints the result.

## Acceptance tests
- Unit tests pass; mypy strict.
- Live smoke: P2 on a real GTA 5 spike returns a valid hook/title/caption/hashtags/keywords/score; P4 on a frame from a facecam stream returns a sensible box (check by drawing it on the frame and saving `layout_check.jpg`).
- Spend for the smoke run appears in `api_usage`.

## Definition of done
Prompts, schemas, calls, confirm glue, tests and smoke script merged; example outputs pasted into PROGRESS.md.

## Handoff to M06
The factory calls `write_clip` (after transcription) and `detect_layout` (when the creator has no cached layout); the gate (M07) calls `visual_check`.

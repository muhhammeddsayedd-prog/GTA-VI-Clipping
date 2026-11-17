# M07 — Quality Gate + YouTube Pre-check

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 11:30–13:30 UTC · **Requires:** M06 · **Launch-critical:** yes

## Goal
Automatic approval that replaces a human reviewer, plus a private YouTube upload check for risky clips. A clip that fails is never posted.

## Context you need
- Two gate profiles (settings `gate.hourly`, `gate.express`):

| Check | hourly | express |
|---|---|---|
| Creator tier | not restricted/blocked; `auto` creators under `gate.auto_creator_daily_cap` (4) clips posted today | `preferred`, or `auto` with music_ratio = 0 |
| Length (s) | 20–60 | 15–45 |
| Music ratio | ≤ 0.10 | 0 |
| Spoiler | reject if `spoiler` and now < `gate.spoiler_block_until`; hook/title never contain spoilers | same |
| Safety | all P2 safety flags false; nothing from `gate.reject_topics` | same |
| Watermarks/logos | P5: no other watermark, no platform logos | same |
| Visual | `blackdetect` ≥ 2 s fails; `freezedetect` (noise 0.003) ≥ 2 s fails; P5 no menu/loading/pause; facecam not empty | same |
| Subtitle sync | ≤ 0.3 s drift | same |
| Score (P2) | ≥ 7 | ≥ 8 |
| Topic duplicate | none approved/posted in 48 h | 6 h |
| Source overlap | no other clip from the same source within ±30 s | same |
| File | 1080×1920, has audio, duration OK, ≤ 100 MB | same |

- Express fallback: if an express clip fails only express-only checks but passes the hourly profile → re-gate as hourly (`lane = hourly`), not rejected.
- Result JSON in `clips.gate_result`: `{"profile":"hourly","passed":bool,"checks":{"<name>":{"ok":bool,"value":…,"limit":…}}}`; first failure → `reject_reason`.
- Music detection: PANNs AudioTagging on 1-second frames; music if "Music" > 0.5 or any of Song/Singing/Rapping/Pop music/Hip hop music > 0.4. `music_ratio = music frames/total`; per-frame flags returned for ducking (M06 uses them).
- **Pre-check** (hourly clips only, `precheck.enabled`, ≤ `precheck.daily_max` per day). A clip is risky if: first clip from an `auto` creator, or music_ratio > 0, or a compilation, or P2 flag `copyrighted_media`. Flow: `precheck_pending` → job `clip.precheck` uploads the MP4 **private** to the owner's YouTube channel with `clients.youtube.upload_private` (unaudited projects can only make private videos — that's what we want) → job `clip.precheck_result` with `run_after = +precheck.wait_s` (1800 s) reads status: fail if `uploadStatus = rejected`, `regionRestriction.blocked` non-empty, or the video is gone → delete the private video → approved or rejected (`precheck_claim`; creator `claims_count += 1`).
- Limits: the public API does not show every Content ID claim; it does show blocks and rejections.

## Build
1. `gate/music.py` (PANNs; load model once per process; CPU).
2. `gate/visual.py` (ffmpeg blackdetect/freezedetect parsing; sample 4 frames; P5 call).
3. `gate/checks.py` — one pure function per check returning `CheckResult(ok, value, limit)`.
4. `gate/profiles.py` — load profiles from settings.
5. `gate/gate.py` — `run_gate(clip_id) -> GateOutcome`; applies express fallback; sets status `approved` / `precheck_pending` / `rejected`; on approve: upload MP4 to Storage (`clips/<env>/<yyyy>/<mm>/<dd>/<clip_id>.mp4`), keep the local `ready/` copy for posting, emit `clip.approved`/`clip.rejected` events.
6. `precheck/youtube_precheck.py` — the two job handlers.
7. Hook the gate into the end of `factory/pipeline.py` (M06).
8. Ranking stub: set `rank_score = score` on approval (M10 replaces it with the full formula).

## Acceptance tests
- Unit: each check with passing/failing inputs; profile selection; express fallback; risky-clip rule.
- Component: craft 5 bad clips from the fixture (2 s black frames, frozen video, music bed, fake watermark text "@othercreator" burned in, a hook containing a story term on the spoiler list) → each rejected with the right reason; one good clip → approved and uploaded to Storage.
- Pre-check on staging test channel: upload/check/delete cycle works end to end (use `precheck.wait_s = 60` in staging for the test, then restore).

## Definition of done
Gate + pre-check merged; test results in PROGRESS.md.

## Handoff
M08/M14 produce clips that flow through this gate automatically. M10 consumes `approved` clips.

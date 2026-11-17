# M06 — Clip Factory (render pipeline)

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 07:30–11:30 UTC · **Requires:** M05 · **Launch-critical:** yes

## Goal
Turn a source window (local video file + window times + spike context) into a finished 1080×1920 MP4 with layout, styled English subtitles, overlays, bleeps and normalised audio — fast enough for the express path.

## Context you need
- Inputs come from the VOD lane (M08: a downloaded section file) or the live lane (M14: extracted from the recording buffer). Both give: `source_path`, `window_start_s`, `window_end_s`, `spike_floor_s` (relative to the file), creator, platform, stream title, P1 verdict, chat sample, lane hint (`spike` | `hourly`), mode (`normal` | `express`).
- Output: a `clips` row (status `rendering` → `gating`), MP4 in `$DATA_DIR/ready/<clip_id>.mp4`. Gate (M07) runs next; upload to Storage happens after the gate passes.
- Design decisions: gameplay on top + facecam below (layout A); blurred background + 4:5 gameplay when no facecam (layout B); English subtitles 2–4 words per line, current word highlighted, keywords in accent colour with a pop; hook for the first 3 s; "● LIVE · JUST HAPPENED" badge for spike-lane clips; credit "🎥 @creator"; watermark `@6starsclips`; progress bar; slow zoom on the last 40%; keyword pop SFX; 1.5 s end card "6 STARS CLIPS · Moments #<series_no> · Follow for more"; profanity bleeped (audio) and masked (`f***`) in subtitles; loudness −14 LUFS.
- Safe zone: no text above y = 220 (`factory.safe_top_px`) or below y = 1500 (1920 − `factory.safe_bottom_px` 420).
- Full spec with exact positions: `docs/09-clip-factory.md` (read it — sections 3–7 are the layout and filter details).
- Settings used: everything under `factory.*`, `vod.whisper_model`, `transcribe.express_provider`, `spikes.min_len_s/max_len_s`.

## Build
1. `factory/transcript.py`: extract 16 kHz mono WAV of `[pad_start, pad_end]`; transcribe (local for normal, hosted for express); if language ≠ en → P8 translation, spreading English words evenly across each segment's time span; return words relative to the file.
2. `factory/lines.py`: group words into subtitle lines (2–4 words, ≤ 22 chars, break on pauses > 0.35 s or punctuation); number them for P2.
3. `factory/writer.py`: build `ClipContext`, call `write_clip`; apply trims (respect `spike_floor_s` and length bounds); store all P2 outputs on the clip row.
4. `factory/profanity.py`: load `assets/profanity.txt`; match words (whole word, simple suffixes); return spans; masked display text.
5. `factory/facecam.py`: get `creator_layouts` row (valid if not expired and same source resolution); else grab the middle frame, call `detect_layout`, store with 7-day expiry. Confidence < 0.6 → treat as no facecam.
6. `factory/subtitles.py`: generate `.ass` (PlayResX 1080, PlayResY 1920; style from settings; one Dialogue per word-state for highlight; `\t` pop on keywords; y = 1150 layout A / 1450 layout B).
7. `factory/overlays.py`: hook box (ASS or drawtext), LIVE badge, credit, watermark, progress bar (`drawbox` with width `iw*t/duration`), end card (pre-rendered PNG per series number via Pillow, or drawtext on a colour source).
8. `factory/audio.py`: bleep spans (`volume=enable='between(t,a,b)':volume=0` + sine mix), music duck spans (from M07's music detector — call it here if available, else skip), keyword SFX (CC0 pop in `assets/sfx/pop.wav`; add `assets/sfx/LICENSES.md`), `loudnorm`.
9. `factory/render.py`: build one `ffmpeg -filter_complex` graph: trim → layout (A or B) → zoom on the last 40% → subtitles (`ass=`) → overlays → concat end card → encode (`libx264 -preset <preset> -crf <crf> -r 30 -pix_fmt yuv420p -movflags +faststart`, AAC 128k 48 kHz). NVENC if `factory.use_nvenc` and available. Measure `render_ms`.
10. `factory/pipeline.py`: `render_clip(input) -> clip_id` orchestrating 1–9, assigning `series_no = nextval('clip_series_seq')` at start, updating the clip row, cleaning scratch files. Job handlers `clip.render` and `express.render` (express: hosted transcription + `factory.preset_express`).
11. Fonts: add Montserrat Black (OFL) to `assets/fonts/` with its license; make ffmpeg/libass find it (`fontsdir=` in the `ass` filter).
12. CLI: `sixstars clip render-file <video> --start 12 --end 48 --creator test --lane hourly` to render without the DB (for design iteration), and `sixstars clip render <spike_id>`.

## Acceptance tests
- Unit: line grouping, profanity matching/masking, ASS generation (snapshot test), filter-graph builder (snapshot test), trim rules.
- Component: render `tests/fixtures/clip_source_30s.mp4` in both layouts; ffprobe confirms 1080×1920, 30 fps, H.264 + AAC, duration = window + end card ±0.1 s; loudness within −14 ± 1 LUFS (`ebur128`).
- Real clip: render the spike from M04/M05 (GTA 5 VOD) as hourly and as express; **save stills at 1 s, 5 s, mid, end** and the MP4; check subtitles stay inside the safe zone and the bleep lands on a test profanity.
- Timing: express render of a 30 s clip ≤ 90 s on the staging server (record actual).

## Definition of done
Factory merged; sample clips and stills attached to the PR; render timings in PROGRESS.md.

## Handoff to M07
The gate receives the clip id, its MP4, transcript, P2 outputs and the music analysis.

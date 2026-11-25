# M20 — Daily 16:9 long-form episode ("TOP N GTA 6 Stream Moments")

Numbering:
- Decision **D122**.
- Migration **0058** (0057 is taken by PR #105, the fixed YouTube thumbnail).
- Code facts were read at `origin/develop` b9d93a6 (2026-11-25, after PR #104).
- Before every push, check every remote branch again for collisions in decision and migration numbers.

Owner request (2026-11-24): "I want to generate 10–15 minute videos to post on the page with the best moments of GTA (reference: https://www.youtube.com/watch?v=dMfN68hBoc8) … 1 video per day … built from our engine, not new cost … use the videos we already render … horizontal 16:9 … very clean, showing all cuts to the end … I need to see a full video before we go and push for production."

Follow `CLAUDE.md`. Build the milestones L0–L8 in order. Each one is tested, committed, logged in `PROGRESS.md` and merged to `develop` before the next starts. The **STOP** points below are hard stops.

Two rules have no exceptions:
- No production setting change without the owner's OK.
- No merge to `main` without the owner's OK.

Background report: https://claude.ai/artifact/XzvUzPd1ACHGPJPbRfpe2Z. Design summary: `docs/31-longform.md`.

## 0. Setup

These are done (2026-11-25):
- The 0056 `schema_migrations` rows were renamed in both environments (owner approved).
- PR #104 is merged.
- The worktree is `../6stars-wt-longform`, branch `longform-l0`.

Owner tasks still open:
1. **Production deploy token.** The production GitHub environment secret `SUPABASE_ACCESS_TOKEN` is invalid. Issue a new one, or production deploys keep failing.
2. **Storage file-size limit.** Raise the global Storage upload limit in both Supabase projects (Dashboard → Storage → Settings) to at least 2 GB. Pro plan defaults allow up to 500 GB; the bucket `episodes` itself is capped in the migration.
3. **YouTube verification.** Confirm that @6starsClips-m3x is phone-verified. Custom thumbnails need it, and so do videos over 15 min (we stay at 14:30 or under).

## 1. Why

| # | Fact | Where |
|---|---|---|
| 1 | About 70 clips a day pass the gate in production, with spike windows of about 45 s. Only the 18 s 9:16 cut is posted. | production `clips`, 17–24 Nov |
| 2 | The raw 16:9 source is deleted after render (VOD windows ≤ 26 h, live cuts at spike end). Only the 9:16 render is kept. | `vod/cleanup.py`, `live/manager.py:331` |
| 3 | A 9:16 render cannot become a clean 16:9 video (vertical crop, stacked cam, burned subtitles). | `factory/layout.py` (every layout crops) |
| 4 | VOD moments can be re-downloaded by range. Live moments map to the archive VOD through `spike → live_session.vod_source_id`. | `vod/sections.py:136`, `vod/store.py:741` |
| 5 | `subtitles.build_ass` and `layout_filters` are fixed at 1080×1920. Audio, encoder, overlay and escape helpers do not depend on the canvas. | `subtitles.py:523`, `layout.py:37`, `audio.py:161`, `presets.py` |
| 6 | Posting parses every lane into the `Lane` enum, so a new lane value crashes `load_accounts`. `posts.clip_id` is NOT NULL. | `posting/store.py:144`, `models.py:10`, `0001_init.sql:293` |
| 7 | Upload-Post takes `video` as a URL (fetched, up to 20 GB) and `thumbnail_url` at upload. | `docs/14` §1 (checked 2026-11-25) |
| 8 | A standard Storage upload is a single POST. Files over 6 MB should use TUS. | `storage.py:133`, Supabase docs |

## 2. Decisions (made; don't revisit)

1. Episodes post on the **main channel**, account `youtube_main`.
2. Every moment is **re-downloaded** and rendered full-frame at 1920×1080 (scale + pad, no crop). We never re-use the 9:16 render.
3. Moments run **15–45 s**. Each is cut from the sentence before the build-up to the first pause after the payoff.
4. The format is a **countdown "TOP N"**: a 5 s cold open of #1, then N → 1, then a 10 s end card. The on-screen `#N` shows for 2 s, the creator credit for 3 s, and English subtitles stay on throughout.
5. Episodes have **their own tables and post record** (`episodes`, `episode_items`). `posts`, the `Lane` enum and the clip lane checks are not touched. The YouTube 24 h count adds episode posts.
6. **Transcription is local** (faster-whisper, setting `longform.whisper_model`). No hosted cost.
7. **Upload by URL**: the episode goes to Storage (TUS), and Upload-Post fetches a 24 h signed URL. The thumbnail goes as `thumbnail_url` in the same call.
8. **Preview before any public post**: a console Episodes page plus a private YouTube upload, with both links on Telegram. Production starts with `longform.publish_mode = private`, and `public` needs the owner's explicit go.
9. Trigger: a scheduler `PeriodicTask` (like `campaign_watch`), plus a CLI command and an n8n-token route for manual runs.

## 3. Settings (migration 0058, idempotent, never overwrites an operator value)

```json
{
  "longform.enabled": false,
  "longform.publish_mode": "off",
  "longform.build_hour_utc": 10,
  "longform.publish_hour_utc": 16,
  "longform.pool_hours": 48,
  "longform.pool_statuses": ["approved", "posted", "expired"],
  "longform.pool_reject_reasons": ["creator_daily_cap", "duplicate_topic", "unreadable_subtitles", "creator_tier_express"],
  "longform.min_score": 7,
  "longform.target_s": 720,
  "longform.min_s": 600,
  "longform.max_s": 870,
  "longform.segment_min_s": 15,
  "longform.segment_max_s": 45,
  "longform.lead_s": 3,
  "longform.tail_s": 2,
  "longform.max_per_creator": 3,
  "longform.max_creator_s": 90,
  "longform.reuse_after_days": 7,
  "longform.fetch_pad_s": 5,
  "longform.whisper_model": "small",
  "longform.render": {"width": 1920, "height": 1080, "fps": 30, "crf": 20, "maxrate": "8M"},
  "longform.priority": 15,
  "longform.keep_days": 7,
  "longform.cold_open_s": 5,
  "longform.end_card_s": 10,
  "longform.account_id": "youtube_main"
}
```

Staging overrides (in `supabase/seed/staging.sql` and the migration's staging guard):
- `enabled` = true
- `publish_mode` = `private`
- `pool_hours` = 72
- `min_score` = 6

Every key goes in `docs/16-settings-reference.md`, in a new section "Long-form episodes".

## 4. Milestones

### L0 — Setup and verification

- Write this file, add D122 to `docs/00-decisions.md`, write the findings into `docs/14-api-integrations.md` (Upload-Post URL upload and thumbnail at upload, Supabase limits, YouTube chapter, length and thumbnail rules), and create the `docs/31-longform.md` skeleton.
- **Measure (staging server)**:
  1. Upload one 500 MB test MP4 to Storage over TUS.
  2. Make a private Upload-Post URL-mode YouTube upload with a `thumbnail_url`.
  3. Record the upload times and the answer shapes in `docs/14`.

  This needs `launch.dry_run` off for one private post, so ask the owner first.
- Done when the docs are merged and the measurements are recorded.

### L1 — Schema and settings (`0058_longform.sql`)

- `episodes` table:
  - `id uuid pk`, `episode_date date unique`;
  - `status`, checked to `planned|fetching|rendering|assembling|ready|publishing|posted_private|posted|failed|skipped|removed`;
  - `n_moments`, `duration_s`, `title`, `description`, `chapters jsonb`, `tags text[]`;
  - `storage_path`, `thumb_path`, `local_path`;
  - `account_id` (FK `platform_accounts`), `privacy`, `upload_request_id`, `platform_post_id`, `post_url`, `published_at`;
  - `error`, `attempts`, `views`, `likes`, `comments`, `created_at`, `updated_at` (`set_updated_at` trigger).
- `episode_items` table:
  - `episode_id` FK, `position`, `clip_id` FK, `creator_id` FK;
  - `fetch_url`, `fetch_start_s`, `fetch_end_s`, `cut_start_s`, `cut_end_s`, `align_offset_s`;
  - `segment_path`;
  - `status`, checked to `planned|fetched|rendered|failed|dropped`;
  - `error`, `updated_at`;
  - unique `(episode_id, position)`, an index on `clip_id` and one on `creator_id`.
- Bucket `episodes`: private, `file_size_limit` 2 GB, mime types `video/mp4` and `image/jpeg`.
- The settings from §3.
- Tests: `tests/integration/test_longform_migration.py` (applies twice, tables exist, settings seeded, no overwrite).
- Done when CI is green and the staging deploy applies 0058.

### L2 — Selector and daily plan (dry run)

New package `src/sixstars/longform/`:
- `config.py` — `LongformConfig.load()`, a typed bundle (pattern: `posting/params.py`).
- `selector.py` — pure functions:
  - `pool_filter`, `rank`, `apply_variety`, `fill_to_target`, `countdown_order`;
  - the inputs are dataclasses, never DB rows.
- `pool.py` — the SQL that loads the candidates:
  - gated statuses plus the allowed reject reasons, `score >= min_score`, created within `pool_hours`;
  - joins spikes (`score_z`, window), creators (blocked or opt-out, P6 `restricted`), the max views per clip from `posts`, and the last `episode_items` use;
  - excludes `removed`, music rejects or flags, `spoiler`, and non-empty `safety_flags`.
- `planner.py` — `plan_episode(day)`:
  - writes `episodes` and `episode_items` in one transaction (dedupe on `episode_date`);
  - emits `episode.planned`;
  - notifies a summary (moments, est. length, creators, top 5 titles), or `episode.skipped` when the total is under `min_s`.
- Triggers:
  - `services/scheduler.py` daily task at `build_hour_utc`;
  - CLI `sixstars longform build --date … [--plan-only]`;
  - `POST /v1/longform/build` (n8n token).
- Tests: `tests/unit/longform/test_selector.py` covers filter, caps, per-creator seconds, topic dedupe, no repeat creators back to back, fill, skip day, countdown order. Add a pool SQL integration test.
- **STOP**: run plan-only on staging for 2 days. The owner reviews the picks.

### L3 — Fetch and 16:9 segment render

- `fetch.py` (job `longform.fetch_item`, queue `normal`, priority `longform.priority`):
  - **VOD:** `download_section(clip.source_url, section_for(start − pad, end + pad, dur), …)` with `ytdlp.access_for` / `proxy.job_accesses`, `max_height` 1080, and `proxy.record_usage`.
  - **Live:** `video_for_live_session(spike.live_session_id)`, then the same padded range on the archive URL.
  - If there is no archive, the download fails, or the video is sub-only, set the item to `dropped`, and the planner back-fills the next ranked moment.
- `align.py` (pure):
  - local `transcribe_window(modes=("local",))` over the fetched range;
  - fuzzy match of the stored `clips.transcript` words gives `align_offset_s`;
  - cut start = sentence start before the build-up (at least `lead_s` before the first stored word);
  - cut end = first pause after the last stored word + `tail_s`;
  - clamp to `segment_min_s`–`segment_max_s`.
- `subs16x9.py`:
  - ASS at PlayRes 1920×1080, captions bottom-centre (about y 980, max 2 lines, about 1500 px);
  - lower-third credit for the first 3 s;
  - reuses `lines.group_lines`, `bleep`, `overlays.dialogue`, `credit_text`, `escape`.
- `segment.py` (job `longform.render_item`):
  - video: `scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30`, then `ass`, then the `#N` drawtext (first 2 s), then `format=yuv420p`;
  - audio: `build_audio_filters(…, end_card_s=0)` with two-pass loudnorm;
  - encode: the `presets` args with `longform.render`;
  - all segments share one format, so they can be joined with concat `-c copy`;
  - re-runs are safe: an item whose `segment_path` exists is skipped.
- Tests:
  - filter-graph and ASS snapshots;
  - an ffmpeg test on `synthetic_source` (1920×1080, 30 fps, AAC 48 k, loudness ±1 LU);
  - alignment tests with synthetic word lists, including a 4 s live drift.

### L4 — Assemble and package

- `assemble.py` (job `longform.assemble`, queued when every item is `rendered` or `dropped`):
  1. cold open;
  2. concat N → 1, with 150 ms audio crossfades at the joins;
  3. end card;
  4. `+faststart`;
  5. TUS upload to `episodes/<env>/<date>/<id>.mp4`.
- `chapters.py` (pure): timestamps from the real segment durations; checks for a first 00:00, at least 3 chapters, each at least 10 s.
- `package.py`: one PL1 call (writer model, with a fake) for the title, description intro, thumbnail text and tags. The description is the intro, then the chapters with creator credits and links, then the footer. The prompt goes in `docs/15` and `ai/prompts/pl1_episode_package.md`.
- `thumb.py`: give `covers/compose` a canvas parameter and add a `thumb_16x9.html` template (1280×720). It uses #1's peak frame, the cam crop (creator layout box) and the PL1 text.
- Status `ready`, then a notify "Episode ready" with the console link, length and moments.

### L5 — Console preview

- API:
  - `GET /v1/admin/episodes`, `GET /v1/admin/episodes/{id}`, `GET /v1/admin/episodes/{id}/media` (signed URLs for the video and thumbnail);
  - `POST /v1/control/longform/rebuild/{id}`.
- Web, `web/app/(console)/episodes`:
  - list and detail pages;
  - a 16:9 player (a `ClipPlayer` variant, contain, aspect 16/9) with chapter rows that seek;
  - a table of the items;
  - a preview of the title, description and thumbnail.
- vitest. Deploy by hand with Vercel.

### L6 — Publish, safety, takedowns

- `publish.py` (job `longform.publish` at `publish_hour_utc`):
  - Refuses unless `enabled`, `publish_mode != off`, `emergency_stop` off and `dry_run` off (`posting/gate.posting_mode`). Staging is always private.
  - Upload-Post in URL mode: YouTube only, `title`, `description`, `tags`, `categoryId=20`, `privacyStatus`, `thumbnail_url`, no `#Shorts`, `request_id`/idempotency `episode:<id>:<account>`.
  - Then `wait_for_upload`, then store `post_url`. Status becomes `posted_private` or `posted`; emit events and notify.
- `posting/caps.py`: count episode posts in the YouTube 24 h total.
- Takedowns:
  - `takedowns.find_match`, `enforce.act_on_creator` and `removal` follow `episode_items.creator_id`;
  - an episode containing that creator is unpublished, set to `removed`, and a notify goes out.
- Guardian:
  - an episode stuck more than 3 h in fetching, rendering or assembling raises an alert;
  - files older than `keep_days` are cleaned up.
- Stats: views, likes and comments per episode.
- Tests: publish refusal matrix, caps, idempotency, takedown match (`FakeUploadPostClient`).

### L7 — Staging acceptance — **STOP: owner watches a full episode**

Build the day's episode on staging, then check:
1. Built in under 3 h; 10:00–14:30 long; 1920×1080 at 30 fps; −14 LUFS ±1; no black or frozen frames.
2. Every moment plays from its build-up to after the payoff; subtitles are in sync (10 spot checks); live moments are aligned.
3. Chapters match the moments.
4. **The owner watches it in the console and as a private YouTube video**. Both links go out on Telegram.
5. Takedown drill: the private episode is removed.
6. `emergency_stop` and `dry_run` both block the upload.

Then build at least 3 daily episodes and write the report `docs/reports/longform-staging-<date>.md`. Stop for the owner's production go.

### L8 — Production (owner go only)

1. `develop` → `main`, then deploy the console by hand.
2. With the owner's OK: `longform.enabled=true`, `publish_mode=private`. The first episode goes up private, and the owner watches it.
3. After the owner's explicit go: `publish_mode=public`.
4. Measure for 7 days.

Rollback: `longform.publish_mode=off`.

## 5. What a correct episode looks like

- Full-screen 1920×1080 stream footage, exactly as it was broadcast. No crop, no blur bars, no zoom punches.
- Hard cuts with soft audio joins. Each moment is 15–45 s and plays to its end.
- `#N` small in the top left for 2 s. A lower-third credit for 3 s. Clean white subtitles with a shadow, at most 2 lines.
- A small, faint 6 Stars mark. Nothing else on screen.
- Cold open, then #N to #1, then the end card. 10:00–14:30 in total.
- The title follows the pattern "TOP 30 Funniest GTA 6 Stream Moments (Nov 25)". Each chapter credits the streamer.

## 6. Reporting

After each milestone, report:
- what was built and the flag names;
- ruff, mypy and pytest results;
- timing, cost and disk;
- blockers;
- a `PROGRESS.md` entry in the usual template.

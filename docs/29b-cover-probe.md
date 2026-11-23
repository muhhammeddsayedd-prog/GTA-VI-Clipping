# 29b — Cover engine C0: re-verification and Upload-Post probe

2026-11-23, staging. Spec: `docs/29-cover-engine.md` + `docs/29a-cover-engine-prompt.md`. Checked against
`origin/develop` at b58de03.

## 1. Probe results (what the live API did)

Three private uploads from staging through Upload-Post profile `default`, on 23 Nov 2026 12:37–12:47 UTC. Script:
`scripts/covers_probe.py`. Clip: `67d5e99b` (21.2 s, layout C, style `cam_top_viral`). Dummy cover: the
clip's frame at 3 s with "COVER PROBE" in yellow, 1080×1920 JPEG, 311 KB, served from a 24 h signed URL in the
`clips` bucket (`clips/covers-probe/staging/<clip>.jpg`). There was no Instagram probe (owner decision, see §3).

| Probe | Sent | Upload answer | Final status (≈ 8 min) |
|---|---|---|---|
| P1 TikTok timestamp | `privacy_level=SELF_ONLY`, `cover_timestamp=3000` | 200 `"Upload queued successfully in the durable worker."` + `job_id` | `completed`, `success: true`. No warning about the cover field. `platform_post_id`/`post_url` null; warning: "TikTok published the video but has not released its public post ID yet…" (normal for TikTok). |
| P2 TikTok custom image | `privacy_level=SELF_ONLY`, `tiktok_cover_image_url=<signed URL>` | 200, queued | First poll: `processing`, attempt 2, `"error": "Recovered by scheduler reaper after lease/heartbeat timeout (attempt 1/5)."` (Upload-Post's worker restarted the job). Second poll: `completed`, `success: true`. **No capability warning:** our account has `cover_image` (see §2). Same "post ID not released yet" warning. |
| P3 YouTube thumbnail at upload | `privacyStatus=private`, `thumbnail_url=<signed URL>` | 200, queued | `completed`, video `X0n4aCpzTVI`, `post_url: "Post uploaded as Private. No public URL available."`, **`thumbnail_set: false, thumbnail_pending: true`** (both polls; the fields appear inside the status `results` entry, which the docs leave unstated). There is no `thumbnail_note` field and no eligibility error. |
| P3b YouTube set-thumbnail | `POST /api/uploadposts/youtube/thumbnail` JSON `{user, video_id: "X0n4aCpzTVI", thumbnail_url}`, 7 min after upload | **HTTP 200** `{"success": true, "video_id", "width": 1080, "height": 1920, "thumbnails": {default, medium, high, standard, maxres, shortsAspectRatio}}` | YouTube's `shortsAspectRatio` thumbnail (1080×1920) **is our probe image** (downloaded and checked). There was **no 403/eligibility error** on a channel that is not in the Partner Program. |

### What this means

- **YouTube is not blocked at the API.** The spec assumed custom Shorts thumbnails need the Partner Program. The
  API accepted our image and YouTube stored it as the Shorts-aspect thumbnail of a private video. What is not
  known yet is whether YouTube **shows** it on Shorts surfaces (search, channel Shorts tab, shelf) for a
  non-YPP channel. Check: the owner opens YouTube Studio → Content → Shorts → `X0n4aCpzTVI` ("Cover probe
  (staging test, private)") and looks at the thumbnail shown there and on the channel's Shorts tab (signed in as
  owner). If it shows, `cover.youtube_custom_enabled` can be tested on staging in C7 instead of waiting for YPP
  (production still needs the owner's yes).
- **`thumbnail_url` at upload stays `thumbnail_pending`.** It did not turn into `thumbnail_set` within 8 minutes, and we
  then overwrote it with P3b, so we can't tell if the pending path would have finished by itself. C7 therefore
  plans the follow-up job (set-thumbnail after processing, back off 2/5/15 min) as the main YouTube path.
- **TikTok:** both cover fields are accepted with no warning. Whether the custom image shows as the grid cover or
  as a visible first frame in playback must be checked in the TikTok app (posts are "Only me" on
  @6starsclipping). That is the C7 10-post test. Upload-Post's lease timeout on P2 may be unrelated to the cover
  (it recovered on retry); watch for it in C7.
- **Instagram:** not probed live. `cover_url` is documented (§2) and verified in docs/14 since M02.

## 2. Upload-Post docs (read 2026-11-23)

Pages: https://docs.upload-post.com/api/upload-video, …/api/upload-status, …/api/youtube-thumbnail,
…/api/user-profiles, …/api/unpublish-post. Also recorded in `docs/14-api-integrations.md` §1.

| Platform | Field | Facts |
|---|---|---|
| Instagram | `cover_url` (string) / `cover_image` (file, JPEG ≤ 8 MB) | `cover_url` wins if both are sent. `thumb_offset` (string, ms; no min/max/default documented). |
| TikTok | `cover_timestamp` (int, ms, default 1000) | Needs capability `cover_timestamp`. |
| TikTok | `tiktok_cover_image_url` / `tiktok_cover_image` | JPG/JPEG/WebP/PNG ≤ 20 MB; wins over `cover_timestamp`; the URL wins over the file. Needs capability `cover_image`. **A missing capability is not an error**: the post publishes and `warnings` names the ignored field; fix = reconnect the account. |
| TikTok | capabilities | Read from `GET /api/uploadposts/users` → `social_accounts.tiktok.capabilities`. **Ours (live, 23 Nov):** `music, location, cover_image, cover_timestamp, draft, video_privacy, photo_privacy, profile_analytics, comments, trend_search`. `GET /api/uploadposts/tiktok/settings?user=default` → `privacy_level_options: PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, SELF_ONLY`. |
| YouTube | `thumbnail` (file, JPG/PNG/GIF/BMP ≤ 50 MB) / `thumbnail_url` | File wins over URL. Shorts still processing → `thumbnail_pending: true` (+ `thumbnail_note` per the docs; not seen). Docs say custom thumbnails need a verified channel. |
| YouTube | `POST /api/uploadposts/youtube/thumbnail` | `user`, `video_id` (the upload's `platform_post_id`), `thumbnail` or `thumbnail_url`. 200 = set; 202 `pending: true` = still processing; 400 bad input; 401 key/token; **403 usually unverified channel**; 404 video not on channel; 429 YouTube daily quota. Error body: `error` + `youtube_status`. |
| All | delete | `POST /api/uploadposts/posts/unpublish` works for YouTube; **not** for Instagram or TikTok. |

## 3. Re-verification of prompt §2 against the code

| # | The prompt assumed | The code / database says | Handling |
|---|---|---|---|
| 1 | Staging = GTA 5 test content | Staging `settings.env.target_game` = `"Grand Theft Auto VI"` (read live, 23 Nov). CLAUDE.md: GTA 5 removed everywhere on 18 Nov. Develop's `supabase/seed/staging.sql` still says GTA V; the fix sits on the unmerged `gta6-hashtags` branch. | Build and test on GTA 6 staging content. |
| 2 | Staging posts go to test accounts | Staging posts through the same profile `default` onto the **production accounts** (D101/D102): TikTok `SELF_ONLY`, YouTube `private`, Instagram can't post privately so staging stays `launch.dry_run = true`. | No Instagram live probe (owner, 23 Nov). The first real IG cover is in C7 with the owner's yes. |
| 3 | Migration `0028_cover_engine.sql` | 0028 is taken (M18). Develop ends at 0036; **0037 is claimed twice** by in-flight branches (`ai-cost-a0` api_usage_detail, local `clipcamp-1` clip_campaigns). | The covers migration takes the next free number at merge time (≥ 0038). |
| 4 | Package `worker/covers/`, `python -m covers.cli` | Package `src/sixstars/`, typer CLI `sixstars`. | `src/sixstars/covers/`, `tests/unit/covers/`, `sixstars covers run|grid`. |
| 5 | Normal lane + one campaign lane per campaign | Develop: `clips.lane` / `posts.lane` ∈ {spike, hourly}; caps in `platform_accounts` (`lanes`, `cap_24h`, `spike_reserve`, gaps) + `posting/caps.py`. Campaign lanes exist only on the **unmerged local `clipcamp-1`** (D117): a nullable `campaign_id`, `lane_quotas.lane_key` ∈ `normal`/`spike`/`campaign:<slug>`, `render_profiles` per look (watermark, logo, disclosure). | `cover.lane_modes` / `cover.brand_mark` keyed by `lane_key` with a `*` default; until clipcamp lands, every clip is `normal` or `spike`. Deadline key `live` = spike lane (express queue), `normal` = hourly. |
| 6 | Renderer keeps clean layers + peak timestamp | One ffmpeg filtergraph (`factory/render.py` `build_filter_complex`; crops in `factory/layout.py`). Scratch dir deleted after render; only `ready/<id>.mp4` is kept. `variants.py` keeps the source span (`source.mkv` + `recipe.json`) only until variants finish. The anchor (`factory/anchor.py`, only when `cut.adaptive` is on) is logged in `scoring_log.sub_scores.anchor_s` in padded-window time. **There is no peak column.** The chat peak is `spikes.spike_offset_s`, and `gate/rescore.py` `peak_in_clip` maps it to clip time. | C3: grab clean frames from the source span with the layout's crop rectangles instead of saving layer videos; persist the clip-time peak on the clip/render (one additive column). |
| 7 | Facecam ≥ 40% possible; captions are karaoke | Layout C (owner's M18 decision, facecam on top) = 672 of 1920 px ≈ 35% (`layout.py` `C_FACECAM_H`). Captions: ASS, chunks of 1–2 words with an active-word colour (`factory/subtitles.py` `chunk_events`), not `\k` karaoke. The probe frame shows the problem: at 3 s the facecam crop shows the streamer's mouth and chest with the eyes out of frame, and the caption reads "THE PHONE". | C1 is a look change on every clip (672 → ≥ 768 px, phrase captions in the cover windows). Owner to confirm before C1. |
| 8 | One cover per clip | M18 renders several looks per clip (`clip_renders`, `posts.render_id`) and story cuts are their own clips (`clips.version`). | `covers` gets `render_id`; `rendered_ts_ms` is per render. |
| 9 | `star_scores` / `clip_features` / `clip_performance` | `post_metrics_snapshots` exists (0033; `learning.snapshots` on in staging only). `star_scores` and `clip_features` are planned (docs/28 L2), not built. There is no `clip_performance`. | Reward from `post_metrics_snapshots` (48 h row). The arm lives on `covers`; add it to `clip_features` when L2 lands. |
| 10 | YouTube traffic-source reward | `clients/youtube_analytics.py` has no traffic-source query, and **no YouTube OAuth refresh token exists**. | That half of the reward waits for the owner's `sixstars youtube auth`. |
| 11 | Upload-Post cover fields | None sent today (`posting/captions.py` `build_fields`, `clients/upload_post.py` `upload_video`). | C7 adds them in `build_fields`. |
| 12 | Cheapest configured model | Anthropic only. `ai.model_fast` = `claude-haiku-5-5` ($0.10 / $0.50 per M tokens). Vision exists (`anthropic_client.image_block`; P4/P5/P9V). Cost via `api_usage` + `ai/costs.py`. | Hook line and QA on Haiku. Rough estimate ≈ $0.05–0.10 per 100 clips for both calls together. No new provider. |
| 13 | Libraries | Only Pillow present. No playwright, mediapipe, OCR, opencv, imagehash or rembg. One Docker image runs every service. | Added in C3/C5/C6 (all free). Proposal: RapidOCR (the PP-OCR models on onnxruntime) instead of the full paddlepaddle install, to keep the image small. |
| 14 | 2 TikTok accounts | One real TikTok account (`tiktok_live` disabled); production `tiktok_main.lanes = {}` since 19 Nov (TikTok not distributing). | TikTok covers matter little until delivery is fixed; they stay late in the rollout. |
| 15 | Live fast lane "posted within 30 min" | `express.render` job on the `express` queue; `posting.spike_max_age_s` = 1800 makes the poster skip older spike clips (`posting/precedence.py`, `SPIKE_TOO_OLD`). It is a cutoff, not a deadline. | Cover deadline 60 s for the spike lane, run beside upload prep. |
| 16 | Spoiler/safety lists to reuse | `ai/calls.py` `spoiler_status` / `enforce_policy` (`gate.spoiler_block_until`, `gate.reject_topics`); `ai/titles.py` `_BANNED`, `spoils_ending`; `factory/bleep.py` profanity list. | The hook-line validator calls these on the cover line. |
| 17 | (not in prompt) | `factory/thumbs.py` already writes `clips.thumb_path` (`clips/thumbs/<env>/…jpg`). | Reuse for fallbacks and the console grid. |

## 4. Clean-up

- The three probe posts stay private: TikTok "Only me" ×2 on @6starsclipping (Upload-Post can't delete TikTok
  posts), YouTube private `X0n4aCpzTVI`. They are not in `posts`, so caps and stats ignore them. Delete the
  YouTube one after the owner's Studio check (`POST /api/uploadposts/posts/unpublish`).
- The cover object `clips/covers-probe/staging/67d5e99b-….jpg` stays in the staging `clips` bucket. The
  signed URL expires on 24 Nov 12:37 UTC.
- No `settings` or `scoring_config` row was read for writing or changed. The only DB writes were the
  Supabase Storage upload.

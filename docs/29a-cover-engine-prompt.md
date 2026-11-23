# Master prompt: Cover Engine (thumbnails and covers), staging first

> The owner's build prompt of 2026-11-23, kept verbatim (only markdown table and code formatting restored).
> It wins over `docs/29-cover-engine.md` where they disagree. Deviations found against the code are recorded in
> `docs/29b-cover-probe.md` and `development-plan/PROGRESS.md`.

You are working on our GTA 6 clips pipeline, 6 Stars Clips: n8n, Supabase, the Python worker in this repo and the admin console. Build the Cover Engine: a new stage that gives every clip a high-quality, subtle, catchy thumbnail or cover on YouTube Shorts, Instagram Reels and TikTok.

The research and design are in the doc "6 Stars Clips — Thumbnail & Cover Layer: Research and Plan": https://claude.ai/artifact/XJMneCSdEGyyAA5XYvAicV. Copy its content into docs/29-cover-engine.md as your first commit, together with this prompt as docs/29a-cover-engine-prompt.md. Treat them as the spec. Where this prompt and the doc disagree, this prompt wins; where either disagrees with the code, ask me.

Staging first. Build and test everything on staging (GTA 5 test content). Production only after the production gate at the end, with every new flag off, switched on one step at a time.

## 1. The one idea to keep in mind

Every cover is real stream footage plus one line of type, set by code, checked before it posts.

We are not building an image generator. Competitor Shorts with millions of views (IShowSpeed, Kai Cenat, xQc clips) all use a real frame: the streamer's face large at the peak of the reaction, plus 1 to 5 words. AI-generated or AI-retouched covers look cheap, can break campaign rules ("no AI videos" on the Clix pool), raise likeness issues with real streamers, and push us toward YouTube's "templated, mass-produced" monetization problem. So:

- No generative pixels anywhere in a cover. No text-to-image, no inpainting or outpainting, no face swap, no face restoration (GFPGAN, CodeFormer and similar make skin look plastic).
- The only AI in this layer: a text model that writes the 1 to 5 word line, and a vision model that grades the finished cover. Everything visual is deterministic code.

## 2. Context you must re-verify before building

Read the code and the database and confirm each point. Report anything that's different before M1.

- Posting goes through Upload-Post. Its upload endpoint already supports covers (verify against https://docs.upload-post.com/api/upload-video):
  - Instagram: cover_url (public URL, wins over cover_image), cover_image (JPEG file ≤ 8 MB), thumb_offset (ms).
  - TikTok: cover_timestamp (ms, default 1000), tiktok_cover_image_url / tiktok_cover_image (custom image, wins over the timestamp, JPG/PNG/WebP ≤ 20 MB, requires a cover_image capability on the account: find out what that means for our account).
  - YouTube: thumbnail / thumbnail_url (≤ 50 MB). For Shorts still processing the response returns thumbnail_pending: true instead of thumbnail_set. There is also POST /api/uploadposts/youtube/thumbnail to set or replace a thumbnail later.
- Zernio stays for ads only. Don't route organic posts through it.
- YouTube custom Shorts thumbnails exist since 24 Jul 2026, but only for YouTube Partner Program channels (verified channel needed; ours is verified since 21 Nov). Our channel is not in the Partner Program yet, so YouTube auto-picks a frame from the video. There is no API to choose that frame for a Short. There is no A/B testing for Shorts.
- Where covers show: a Shorts thumbnail never shows in the swipe feed; it shows in search, the channel's Shorts tab, the home Shorts shelf and subscriptions. TikTok and Instagram covers show in the profile grid (cropped to the centre 3:4), in search and in the Reels tab. An Instagram cover can't be changed after posting.
- Renderer: find where the 9:16 render is built (facecam crop, gameplay crop, captions, watermark). The engine needs the clean layers (facecam and gameplay before captions are burned in) and the clip's peak timestamp (the anchor from the scoring overhaul). If the renderer doesn't keep these, M2 adds that.
- Lanes: the normal lane plus one campaign lane per clipping campaign/creator, each with its own rendering settings, all sharing each account's posting limit. Find where lane settings live.
- Config and logging: scoring_config (with version history) and scoring_log from migration 0026. Reuse them. If the Learning Loop work has landed, post_metrics_snapshots, star_scores and clip_features exist: the cover arm becomes a feature there. If not, use clip_performance.
- Live fast lane: strong live spikes target "posted within 30 minutes". The cover must never slow that lane down.
- Our current covers (audited 23 Nov on @6starsClips-m3x, 64 Shorts): YouTube auto-picks dark night gameplay frames with a small or missing face and captions cut mid-word ("SUSPECT'S", "[WARNING] BHP", "ST RIGHT"), and the caption styles are mixed. That is the baseline to beat.

## 3. Decisions already made

- Approach: real frames + code-composited type. No generative imagery (section 1).
- Order of value: (1) make the video's own frames cover-ready, because that is all YouTube uses today; (2) Instagram custom covers, which work today; (3) TikTok, timestamp first, custom image after testing; (4) YouTube custom thumbnails, built now but off until we're in the Partner Program.
- A cover never blocks a post. If the cover isn't ready in time, or fails QA 3 times, the clip posts with the platform's default (or the best plain frame with no text).
- Brand: a small, fixed "VI" neon mark (from our Vice City identity) in the same spot on every normal-lane cover. Never big, never a full synthwave treatment. Campaign lanes can turn it off.
- English only, global audience, no regional styling.
- Models: the hook line and the QA check use the cheapest model already configured in our stack that passes the evals in M4 and M6. Don't add a new provider without asking me (we are actively cutting LLM cost).

## 4. Hard rules (never loosened by learning or by you)

- No generated, inpainted, swapped or face-restored pixels in any cover. Mild global colour/contrast and unsharp-mask sharpening only.
- The line on a cover must describe something that is said or happens in the clip. No invented events, no fake quotes.
- No story spoilers or ending reveals. Reuse the clip pipeline's spoiler and safety checks and word lists, and add the cover line to what they check.
- At most one emoji. No red arrows, circles, stacked boxes or "SPOILER WARNING" banners.
- Never reuse the same layout + colour + line style for more than 3 covers in a row on one account.
- A cover failure never delays or blocks a post (deadline in section 7, stage 6).
- The existing safety rules still apply: snapshots before changes, additive migrations only, every behaviour behind a flag, every decision versioned (config_version on each row), a changelog entry in docs/CHANGELOG-scoring.md for each change, secrets never in the repo.

## 5. Ask me first (only these)

- Any new paid service or model provider.
- Turning on covers (anything beyond frame_only) for a campaign lane: I'll check the campaign brief first.
- Turning on TikTok custom cover images (tiktok_cover_image_url) in production.
- Any change to the renderer that changes clip length, cut points or audio (M1 changes layout and captions only).
- Expected cost above 1.5× the current daily AI spend.

For everything else: act, then report.

## 6. The cover design spec

### 6.1 Canvas and safe zones

- Master canvas 1080 × 1920, sRGB JPEG, quality 90, under 2 MB.
- Safe band: everything that matters (face, line, brand mark) sits inside x 60–960, y 400–1520. That keeps it inside YouTube's central area (~900 × 1160) and inside the centre 3:4 crop TikTok and Instagram use in the profile grid (~1013 × 1350, y ~285–1635).
- Export a 3:4 preview (centre crop 1080 × 1440) for every cover, for QA and for the dashboard grid.

### 6.2 Three layout families

| Layout | When | Geometry (inside the 1080 × 1920 canvas) |
| --- | --- | --- |
| Split (default) | Reaction to something on screen | Facecam panel y 380–1000 (face centred, face height ≥ 35% of the panel). Gameplay panel y 1000–1540 showing the moment. The line sits across the seam, centred at y ≈ 1000. Areas outside y 380–1540 are filled with a blurred, darkened continuation of the nearest panel (blur radius ≥ 40 px, brightness −35%), not black bars. |
| Face-led | Pure reactions, talking/quotable moments, screams, laughter | Face crop fills the canvas, face centre at y ≈ 800, face height 40–55% of the safe band. Line in the lower third, centred at y ≈ 1300. |
| Moment-led | Spectacle: crashes, chases, ragdolls, physics | Gameplay fills the canvas. The streamer's face as a cut-out inset (rembg / BiRefNet) at bottom-left, 300–380 px tall, with a 6 px white outline. Line centred at y ≈ 560. |

Layout selection: the scorer proposes the layout that fits the frame (face quality vs gameplay interest); the bandit (stage 7) can override within allowed layouts.

### 6.3 The line

- 1 to 5 words, CAPITALS, max 2 lines, max 14 characters per line. Prefer 2 to 4 words.
- Two styles, both logged:
  - Quote: the loudest, most quotable thing said at the peak, trimmed (e.g. SIX STARS?!, OH MY GOD I DID IT).
  - Label: names what happened (e.g. NIKO IS IN GTA 6, FIRST CAR STOLEN, 11 HOURS GONE).
- It complements the post title, never repeats it (no more than 2 shared content words).
- It must make sense to someone who has never seen this streamer.
- Banned: clickbait claims not in the clip, "INSANE", "CRAZY", "YOU WON'T BELIEVE", "SPOILER", ending/story reveals, slurs, profanity beyond what the platform shows in captions (mask as F*CK style), more than one ?!.

### 6.4 Type

- Fonts (bundle them in assets/covers/fonts/, all SIL Open Font License): Anton (main), Montserrat Black (alternate), Inter SemiBold (TikTok native box).
- Main style: white #FFFFFF, one key word in yellow #FFD400, black outline 12–16 px (scaled to font size), soft shadow (0 6 px 18 px rgba(0,0,0,0.45)).
- Auto-fit: largest size where the line fits 900 px wide, cap height 110–170 px; never below 96 px font size.
- A faint dark gradient (black at 0 → 40% opacity) behind the text area only, so it reads on bright frames.
- TikTok native style (TikTok only, a bandit arm): sentence case, black text in a white rounded box (radius 24 px, padding 28 × 40 px), Inter SemiBold 56–64 px, max 2 lines. This is how most TikTok GTA clip covers look; it reads as a normal post, not an ad.

### 6.5 Finish

- Global: contrast +8–12%, saturation +8–12%, no filters or LUT looks.
- Face area: unsharp mask (radius 1.2, amount 0.6). If the facecam crop is under 400 px tall, an optional 2× upscale with Real-ESRGAN general model (realesr-general-x4v3 or similar), never a face model. Behind cover.upscale_enabled.
- Brand mark: assets/covers/brand/vi-mark.png, 72 px tall, at x 84, y 412 (top-left of the safe band), 90% opacity. On TikTok native style, bottom-right of the box instead.

### 6.6 Platform outputs per clip

| Platform | What we send | Notes |
| --- | --- | --- |
| Instagram | cover_url → the composed JPEG, public URL from Supabase Storage bucket covers-public | Can't change after posting: QA must pass before upload, otherwise send thumb_offset of the best rendered frame |
| TikTok | cover_timestamp = the best rendered frame (full caption phrase + visible face, scored in stage 2). Custom image via tiktok_cover_image_url only when cover.tiktok_image_enabled | Custom image off until tested (M7) |
| YouTube | thumbnail_url at upload when cover.youtube_custom_enabled; if thumbnail_pending, retry with the set-thumbnail endpoint after processing | Off until we're in the Partner Program. Until then the M1 video changes are what matter |

## 7. The engine, stage by stage

New package worker/covers/ (adjust to the repo's layout):

```
covers/
  __init__.py
  config.py        # reads cover.* keys from scoring_config, returns a versioned dataclass
  miner.py         # stage 1
  scorer.py        # stage 2
  hookline.py      # stage 3
  compose/         # stage 4: templates/*.html, render.py (Playwright), assets loader
  qa.py            # stage 5
  deliver.py       # stage 6: builds Upload-Post params, YouTube follow-up
  learn.py         # stage 7: bandit + weekly report section
  cli.py           # `python -m covers.cli run --clip <id> [--dry-run]`, `grid --account <x>`
tests/covers/      # unit tests + golden-set tests
```

**Stage 0: cover-ready video (renderer change, M1).**

- Split layout in the video: facecam panel at least 40% of the frame height whenever a face is detected.
- During the first 2 s and ±1 s around the peak, show the caption as a complete phrase (one line of up to ~6 words), never word-by-word karaoke and never a cut word. Karaoke can continue outside those windows.
- No dark frame in the first 1.5 s: if the gameplay is very dark (mean luma < 50/255), lift it +15% brightness in that window only.

Why: YouTube picks our Shorts thumbnails from the video today, and TikTok's cover_timestamp points at a rendered frame.

**Stage 1: mine frames.** From the clean layers (facecam and gameplay separately), extract frames with ffmpeg: 4 fps from peak −2 s to peak +2 s, 1 fps across the rest of the clip. Also extract the matching rendered frames (for TikTok's timestamp and the YouTube auto-pick check). Keep them in a temp dir; persist only the top 5 per clip to Storage (covers-work/<clip_id>/), delete the rest.

**Stage 2: score frames.** Per facecam frame:

- MediaPipe Face Landmarker with blendshapes: face_found, face_area (share of the panel), eyes_open (1 − eyeBlink L/R), expression = max of (jawOpen, eyeWide avg, browInnerUp, mouthSmile avg) with a bonus when two or more are high.
- sharpness = variance of the Laplacian on the face box (normalised per stream: percentile within this clip).
- exposure = mean luma of the face box (ideal 90–190) and % clipped pixels.
- Hard rejects: no face, eyes closed (eyes_open < 0.35), face box cut by the frame edge, motion blur (sharpness bottom 20%).

Per gameplay frame: detail (edge density), brightness (penalise mean luma < 60), change (difference from 0.5 s earlier, so we catch the moment itself).

frame_score = 0.35·expression + 0.20·face_area + 0.15·sharpness + 0.10·exposure + 0.20·gameplay (weights in config). The scorer also returns the proposed layout: face-led if gameplay < 0.3 or the clip is tagged talking/quotable; moment-led if gameplay > 0.7 and face_area < 0.25; else split.

For rendered frames: rendered_score = frame_score + 0.3 if OCR finds a complete caption phrase (no cut word) − 0.5 if the caption is cut. The best one is TikTok's cover_timestamp.

**Stage 3: hook line.** One call returning JSON. Input: transcript ±5 s around the peak, the title, streamer name, event tags (first time, police chase, wasted, streamer-in-game, countdown), the clip's taste-profile tags, and the account's last 20 cover lines. Use this prompt (keep it in covers/prompts/hookline.md, versioned):

```
You write the text line for a short-video cover. The cover shows a real streamer's face reacting
to a GTA 6 moment. Your line is the only text on it.

Rules:
- 1 to 5 words, CAPITALS, max 2 lines of max 14 characters.
- It must describe something said or shown in this clip (use the transcript). Never invent.
- It must add something the title doesn't say; share at most 2 content words with the title.
- Understandable by someone who doesn't know this streamer.
- No story spoilers, no ending reveals, no "INSANE/CRAZY/YOU WON'T BELIEVE", no slurs.
  Mask profanity like F*CK. At most one "?!" and no emoji (the template adds one if allowed).
- Different from these recent lines: {recent_lines}

Write 3 candidates:
- 2 in "quote" style: the loudest, most quotable thing said near the peak, trimmed.
- 1 in "label" style: names what happened.
Mark one key word per line for yellow.

Return JSON only:
{"candidates":[{"text":"...","style":"quote|label","key_word":"...","source":"exact transcript words it is based on"}]}

Title: {title}
Streamer: {streamer}
Event tags: {tags}
Transcript around the peak: {transcript}
```

Validate the JSON in code (length, characters per line, banned words, overlap with the title, source must appear in the transcript, fuzzy match ≥ 0.8). Drop failing candidates. If none survive: no line; the cover becomes the plain best frame (still useful on TikTok/Instagram).

**Stage 4: compose.** HTML/CSS templates (compose/templates/split.html, face_led.html, moment_led.html, tiktok_native.html) rendered to PNG with Playwright (headless Chromium, one browser kept warm per worker). Inputs: the chosen frames as local file URLs, the line, the key word, flags (brand mark, emoji). Use CSS -webkit-text-stroke + paint-order: stroke fill for the outline, and a JS fit-text loop for auto-sizing. Render the top 1 to 3 variants requested by the bandit. Target: under 3 s per variant on the worker. Then convert to JPEG and make the 3:4 preview.

**Stage 5: QA gate.** Rule checks first (cheap), then one vision check.

Rule checks:

- OCR (PaddleOCR) on the cover downscaled to 15% (≈ 162 px wide, the grid size): must read the line back with ≥ 0.85 similarity.
- Face detected in the safe band, face area within the layout's range.
- Exposure: mean luma of the safe band 70–200; < 2% clipped.
- Diversity: perceptual hash (imagehash pHash) Hamming distance ≥ 12 from each of the account's last 9 covers; layout+colour+style not used 3 times in a row.
- Text: banned words and spoiler list again, on the final line.

Vision check (one call, the 3:4 preview + the 1080 × 1920 cover + the transcript snippet + the title), prompt in covers/prompts/qa.md:

```
You are reviewing a cover image for a short video before it is posted. Answer in JSON only.
Score each from 0 to 2:
1. readable: the text can be read at small size.
2. faithful: the text and image match the transcript; nothing implied that doesn't happen.
3. natural: looks like a real stream frame with clean text; no artifacts, no odd crops, no
   warped faces, not cluttered.
4. clear: one idea; a stranger understands what the clip is about.
5. safe: no spoiler, no graphic violence against civilians, nothing sexual, no slurs.
Return {"scores":{...},"total":0-10,"hard_fail":true|false,"reason":"one sentence"}.
hard_fail = true if faithful = 0 or safe = 0.
```

Pass = all rule checks + vision total ≥ 7 (config cover.qa_pass_score) + no hard_fail. On fail: try the next-best frame (max 3 attempts), then fall back to the plain best frame without text (it must still pass the rule checks except OCR), then to platform default.

**Stage 6: deliver.** Build the per-platform Upload-Post params from 6.6. Covers run in parallel with upload preparation; deadline cover.deadline_s (default 60 s for the live fast lane, 300 s for normal posts). Missed deadline → post without a custom cover, log status = 'skipped_deadline'. YouTube: when thumbnail_pending, queue a follow-up job that calls the set-thumbnail endpoint once processing is done (back off 2, 5, 15 min). On 403/permission errors, set cover.youtube_custom_enabled = false and send one Telegram alert. On rate-limit errors (429), back off and retry for up to 24 h.

**Stage 7: learn.** Thompson sampling per platform over arms = layout × line_style (plus tiktok_native on TikTok and no_line as a control arm at 10%). Keep ≥ 20% exploration. Reward = views_vs_account_median at 48 h, binarised at > 1.0 (use the Learning Loop's snapshots if present), plus, on YouTube, the share of views from non-feed traffic sources (search, channel, browse, subscriptions) via the YouTube Analytics API. Update weekly. Bandit can only choose among layouts allowed by the scorer for that clip.

## 8. Data model (additive migration 0028_cover_engine.sql; adjust the number and the referenced table names to what exists)

```sql
create table covers (
  id uuid primary key default gen_random_uuid(),
  clip_id uuid not null references clips(id),
  post_id uuid references posts(id),            -- filled when posted
  lane text not null,
  platform text not null check (platform in ('youtube','instagram','tiktok')),
  arm text not null,                             -- e.g. 'split|quote'
  layout text,                                   -- split | face_led | moment_led | tiktok_native | plain
  line_text text,
  line_style text,                               -- quote | label | none
  key_word text,
  frame_ts_ms int,                               -- clean-layer frame used
  rendered_ts_ms int,                            -- rendered frame (TikTok timestamp / YT check)
  scores jsonb not null default '{}',            -- face, expression, sharpness, exposure, gameplay, ocr, vision, phash_min_dist
  phash text,
  storage_path text,
  public_url text,
  status text not null default 'draft',          -- draft | passed | failed | applied | apply_failed | skipped_deadline | fallback_plain | fallback_default
  attempt int not null default 1,
  error text,
  config_version int not null,
  created_at timestamptz not null default now(),
  applied_at timestamptz
);
create index on covers (platform, created_at desc);
create index on covers (clip_id);
```

Config keys (in scoring_config, all with descriptions, versioned):

| Key | Default (staging) | Meaning |
| --- | --- | --- |
| cover.enabled | true | Master switch |
| cover.platforms | ["instagram","tiktok","youtube"] | Platforms the engine runs for |
| cover.youtube_custom_enabled | false | Send thumbnail_url to YouTube |
| cover.tiktok_image_enabled | false | Use tiktok_cover_image_url instead of the timestamp |
| cover.lane_modes | {"normal":"full","*":"frame_only"} | Per lane: full / frame_only / off |
| cover.brand_mark | {"normal":true,"*":false} | Per lane |
| cover.weights | as in stage 2 | Frame score weights |
| cover.qa_pass_score | 7 | Vision pass threshold |
| cover.deadline_s | {"live":60,"normal":300} | Per lane type |
| cover.upscale_enabled | false | Real-ESRGAN on small facecams |
| cover.exploration | 0.2 | Bandit exploration share |
| cover.video_ready | false | Stage 0 renderer changes |

frame_only = best real frame, no line, no mark (and for TikTok the best cover_timestamp). Safe for campaign lanes until I approve full.

## 9. Milestones (each self-contained, tested, committed; progress notes per milestone)

**C0, verify and probe.**
- Re-verify section 2 and report.
- On staging: post 1 test clip per platform via Upload-Post with a dummy cover (IG cover_url, TikTok cover_timestamp, YouTube thumbnail_url). Record what each returns, including thumbnail_pending, any capability errors on tiktok_cover_image_url, and the YouTube eligibility error text. Write it to docs/29b-cover-probe.md.

**C1, cover-ready video (stage 0) behind cover.video_ready.**
- Done when 20 staging clips each show a complete caption phrase and a face ≥ 35% of the facecam panel at the peak and in the first 2 s, verified by OCR + face detection on the rendered frames, and clip length/cuts/audio are unchanged.

**C2, migration, config, Storage buckets** (covers-work private, covers-public public-read for IG cover_url only).

**C3, miner + scorer (stages 1–2)**, including saving clean layers from the renderer if missing.
- Done when, on 30 staging clips, a blind pairwise check (you show me the top frame vs a random frame from the peak window as a contact sheet in docs/reports/covers-c3.md) favours the top frame in ≥ 25 of 30.

**C4, hook line (stage 3).**
- Eval set: 50 clips (staging + the moments in reference/clips.txt whose transcripts we have). Done when ≥ 45 have at least one valid candidate after validation, and none invent content (check source against the transcript).
- Report the model used and cost per 100 clips.

**C5, compositor (stage 4).**
- All 4 templates, fonts bundled, auto-fit, brand mark, blurred fill, 3:4 preview.
- Done when 30 clips render in all allowed layouts under 3 s per variant, and a contact sheet of the 3:4 previews is in docs/reports/covers-c5.md for my review.

**C6, QA gate (stage 5).**
- Build a test set of planted bad covers: wrong text, no face, dark frame, near-duplicate, spoiler line, text too long, cut-off text. Done when the gate rejects all planted bad ones and passes ≥ 80% of the good ones from C5.
- Report the vision model used and cost per 100 covers.

**C7, delivery (stage 6)** with the deadline, fallbacks, YouTube follow-up job, per-lane modes, Telegram alerts.
- Instagram first, then TikTok timestamp, then YouTube behind its flag.
- TikTok custom image test on staging: 10 posts with tiktok_cover_image_url, check whether the image appears as a visible first frame in playback; report with screen recordings.

**C8, learning + console.**
- Bandit (stage 7) and the cover arm as a feature in clip_features (or clip_performance).
- Console Covers tab: per account, the last 30 covers as the platform grid shows them (3:4 for TikTok/IG, 9:16 for YouTube), with status, arm and QA score on hover; a filter for fallbacks and failures.
- Weekly report section in docs/reports/<week>.md: QA pass rate and top fail reasons, fallback rate, deadline misses, arm results with sample sizes, 5 best and 5 worst covers.

**T, staging test plan.**
- Unit tests for every pure function (fit-text, validation, rules, bandit update).
- 48 h end to end on staging with all three platforms, posting at the normal cadence.
- Fault drills: Playwright crash, OCR model missing, vision model timeout, Storage down, Upload-Post 5xx, YouTube 403 → each must post the clip without a cover and alert once.
- Results in docs/CHANGELOG-scoring.md.

## 10. Production gate (all must be true; then tell me and wait)

- All C-milestones done, tests and drills green.
- I've reviewed the C5 and C6 contact sheets and said yes.
- Production backups taken (affected tables + n8n workflows exported to backups/n8n/<date>/).
- cover.lane_modes for every campaign lane is frame_only or off.

Then switch on in this order, one step at a time, each watched for at least 24 h and logged with its rollback flag:

1. cover.video_ready (stage 0, helps YouTube immediately)
2. cover.enabled with cover.platforms = ["instagram"]
3. add "tiktok" (timestamp only)
4. bandit live (cover.exploration 0.2)
5. cover.tiktok_image_enabled only after the C7 test and my yes
6. cover.youtube_custom_enabled only once the channel is in the Partner Program, with my yes

Automatic rollback triggers (flip the last flag back yourself and tell me): post failures rise above the previous rate; more than 5% of posts miss their slot because of the cover stage; QA pass rate below 50% for 6 h; AI spend for covers above 2× the C4 + C6 estimate.

## 11. Brainstorm backlog (don't build now; note ideas you'd add)

- Cover ↔ hook consistency: show the cover line as the on-video caption in the first 1.5 s, so the click promise and the opening match (good for retention, bad-for-bait).
- Per-streamer memory: store each streamer's facecam framing and best-performing arm; start their clips from that arm.
- Series look for big moments: a consistent treatment for "FIRST ___ IN GTA 6" clips so the grid reads as a series on the channel page.
- Emotion map: map the dominant blendshape (scream, laugh, shock, cry) to a line style or key-word colour, then let the bandit test it.
- Ranking/compilation clips: a numbered-list template when we start posting multi-moment Shorts.
- Gameplay-only lane: a cover style for spectacle clips with no streamer (moment-led without inset).
- Grid-aware selection: choose among passing variants the one that best contrasts with the last 2 covers in the account grid (colour histogram distance).
- Animated previews: not controllable via API today; watch for platform changes.
- Long-form later: if we post compilations to YouTube long-form, the same engine at 1280 × 720 with Test & Compare (3 variants).

## 12. How to report

After each milestone, a short message:

- what was built, with flag names;
- test results and the contact sheet link where relevant;
- cost per 100 clips for any model call;
- anything blocked;
- what you need from me.

Full detail goes in the changelog and the progress notes.

## Start now

1. Copy the spec doc and this prompt into docs/29-cover-engine.md and docs/29a-cover-engine-prompt.md.
2. Re-verify section 2 and run the C0 probe on staging.
3. Report to me before starting C1.

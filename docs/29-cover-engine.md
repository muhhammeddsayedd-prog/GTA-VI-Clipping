# 6 Stars Clips — Thumbnail & Cover Layer: Research and Plan

Nov 23, 2026 · @Muhammed

> Copied on 2026-11-23 from the Claude Doc <https://claude.ai/artifact/XJMneCSdEGyyAA5XYvAicV> (rev 8). This is the
> cover engine's spec, together with the build prompt in `docs/29a-cover-engine-prompt.md`. **Where the two
> disagree, 29a wins** (for example 29a's milestone list C0–C8, its 4 fps peak window and 3 s compose target
> replace the M1–M7 list, 2 fps window and 5 s target below). The verification against the code is in
> `docs/29b-cover-probe.md`.

## Summary

Build the cover layer as a code-composited system on real stream frames, not an image generator, and fix the video's own frames first, because YouTube will keep choosing our Shorts thumbnails for us until we join the Partner Program.

- **Where covers count.** A Shorts thumbnail never shows in the swipe feed, where most views come from. It shows in search, the channel's Shorts tab, the home shelf and subscriptions. On TikTok and Instagram the cover is what people see in the profile grid (cropped to 3:4), in search and in the Reels tab.
- **What we can set today.** Instagram: a custom image through `cover_url`. TikTok: only a frame timestamp (Zernio can also add a 1-frame image at the start). YouTube: custom Shorts thumbnails exist since 24 Jul 2026, through the API too, but only for Partner Program channels. Our channel (6 subscribers, 64 Shorts) is not in it, so YouTube picks a frame itself.
- **What competitors do.** The Shorts that get millions of views use real frames: the streamer's face at the peak of the reaction, large, plus one 2 to 5 word line in heavy white or yellow type. On TikTok the line sits in a plain white caption box. None of the top ones use generated images.
- **Where we are.** Our auto-picked frames are often dark night gameplay, with a small or missing face and captions cut mid-word ("SUSPECT'S", "\[WARNING\] BHP"). That reads as broken in a grid.
- **The plan.** Seven milestones: fix in-video cover frames, then frame mining and scoring, hook text, template compositing, a quality gate, per-platform delivery, and a learning loop. Faces are never generated or edited by AI. It runs mostly on open-source tools on our worker, plus one cheap text call and one cheap vision check per clip.

## Where covers actually matter

Instagram is the only platform where we can set a fully custom cover today. YouTube needs Partner Program status, and TikTok only takes a frame.

| Platform | Where the cover shows | What we can set by API | Size and safe zone | Limits |
| --- | --- | --- | --- | --- |
| YouTube Shorts | Search, channel Shorts tab, home Shorts shelf, subscriptions. **Not** the swipe feed ([vidIQ](https://vidiq.com/blog/post/youtube-shorts-custom-thumbnails/)) | `thumbnails.set` now covers Shorts. Call it only after processing has succeeded ([API docs](https://developers.google.com/youtube/data/reference/rest/v3/thumbnails/set)). Zernio's YouTube post takes a `thumbnail` URL ([Zernio](https://docs.zernio.com/platforms/youtube)) | 1080 x 1920. Keep face and text in a central \~900 x 1160 area | Custom Shorts thumbnails started with Partner Program channels on 24 Jul 2026 ([YouTube blog](https://blog.youtube/news-and-events/making-thumbnails-easier-on-youtube/)). Needs a verified channel (ours since 21 Nov). No A/B testing for Shorts ([PPC Land](https://ppc.land/youtube-ends-2-year-wait-for-shorts-thumbnails-but-blocks-a-b-testing/)). Channels have a thumbnail upload rate limit |
| Instagram Reels | Profile grid (3:4 crop), Reels tab, Explore grid | `cover_url` (public image; wins over `thumb_offset`) ([Meta docs](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media)). Zernio: `instagramThumbnail` / `reelCover` | 1080 x 1920. The grid shows the centre 3:4 (about 1013 x 1350) ([Planoly](https://planoly.com/blog/guide-to-instagrams-new-vertical-grid)) | Cover can't be changed after posting |
| TikTok | Profile grid (3:4 crop), search results | Only `video_cover_timestamp_ms`, a frame from the video ([TikTok docs](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post)). Zernio offers `video_cover_image_url`, which stitches the image in as the first frame | 1080 x 1920, centre 3:4 | A stitched frame is real video content; check it doesn't flash before the hook |

Two consequences for us:

1. **One master, three crops.** Design every cover at 1080 x 1920, with the face and text inside the centre 3:4 band. That one image then works in all three grids.
2. **Until we're in the Partner Program, the video is the thumbnail on YouTube.** YouTube picks a frame itself. So the frames near the hook must already look like a good cover: big face, one complete readable line. The same frames are what we point TikTok's timestamp at.

Open question: whether the lower Partner Program tier (fan funding only) unlocks custom Shorts thumbnails. Check in YouTube Studio when we qualify.

## Competitor teardown

Every high-view GTA VI streamer Short I looked at uses a real stream frame. It shows the streamer's face large and at peak emotion, with one short line of text. The heavily designed covers are the minority and usually the loudest.

Method: on 23 Nov 2026 I looked at YouTube search results (Shorts filter) for "gta 6 streamer clips", "xqc gta 6", "ishowspeed gta 6" and "kai cenat gta 6", TikTok video search for "kai cenat gta 6", and our own channel's Shorts tab. View counts are as shown that day. Channel size drives views more than covers do, so treat these as patterns, not proof.

| Example (views) | Where | What the cover shows | Pattern |
| --- | --- | --- | --- |
| IShowSpeed playing GTA 6 for the first time (4.9M) | YT search | Split screen: Speed's face on top, gameplay below. Caption "BRO GIVE ME THAT", a small "GTA 6" tag | Frame + quoted line |
| IShowSpeed's first GTA 6 stream will be chaos (2.9M) | YT search | Big neon "VI" logo over the face, "1 YEAR LATE" | Frame + game logo |
| Streamers discover GTA 6 bugs (1.5M) | YT search | Face on top, ragdoll in water below, caption "\*FALLS\*" | Frame + action word |
| Speed's first backflip in GTA 6 (326K) | YT search | Screaming face, green "OH MY GOD I DID IT" | Frame + quoted line |
| Kai Cenat finds Jimmy De Santa (312K) | Kai Cenat Live, official (16.9M subs) | Face on top, character below, caption "AS F\*CK" | Frame + burned-in caption |
| Jason lost 11 hours of progress (260K) | YT search | Face on top, save screen below, yellow "PROGRESS" | Frame + one word |
| Speed reacts to the GTA 6 ending (40K) | YT search | Crying face close-up, huge "THE GTA 6 ENDING" and one emoji | Designed, face-led. Also a story spoiler |
| xQc sees Niko Bellic in GTA 6 (2.9K) | YT search | xQc's face behind huge 3-line "NIKO IS IN GTA 6" | Designed, clean |
| xQc reacts to Niko Bellic reveal (14K) | YT search | Red boxes "OMG HE'S BACK!!?? NO WAY!!", emoji, "SPOILER WARNING" | Designed, loud. The clickbait end |
| Kai Cenat clips (likes hidden) | TikTok search | Nearly all: face on top, gameplay below, a plain white rounded caption box in sentence case ("Kai's reaction to GTA 6 in daylight") | Native TikTok caption |
| Our channel (2 to 524 views) | YouTube @6starsClips-m3x | Auto-picked frames: often dark night gameplay, small or no face, captions cut mid-word ("SUSPECT'S", "\[WARNING\] BHP", "ST RIGHT"), mixed caption styles | No cover intent |

What the winners share:

- **A face carries the click.** The streamer's face takes 35 to 50% of the frame, at the peak of the reaction (mouth open, eyes wide, crying, screaming).
- **One idea in 1 to 5 words.** It is either the line they shouted ("OH MY GOD I DID IT") or a label for what happened ("NIKO IS IN GTA 6"). It adds what the face can't say; it doesn't repeat the title.
- **Readable at thumb size.** Heavy sans in white or yellow with a black outline, or black text in a white box. Two lines at most.
- **Context in the frame.** The gameplay half shows the thing being reacted to, so the cover makes sense without the title.
- **Native beats designed on TikTok.** The TikTok covers look like ordinary posts, not ads.

What to avoid, seen in the weaker ones: stacked red boxes, several emojis, "SPOILER WARNING" banners, and story-ending spoilers (which our hard rules already ban).

## The formula: catchy, never sloppy

A cover looks "subtle but catchy" when every pixel is real footage and the only added layer is one well-set line of type. It looks like AI slop when the system invents pixels, piles on effects, or makes every cover look the same.

Why this matters beyond taste:

- Viewers have learned to spot generated images: over-saturated light, odd textures, slightly wrong expressions. Covers without a clear idea feel interchangeable, and people scroll past ([Uppbeat](https://uppbeat.io/blog/creator-questions/are-ai-thumbnails-putting-viewers-off-your-videos)).
- YouTube does not monetize content that "looks like it's made with a template with little to no variation" ([YouTube help](https://support.google.com/youtube/answer/1311392)). Clip channels are allowed, but our covers should vary with the moment, not be one stamp.
- Some paid campaigns ban AI outright (Clix's pool says "no AI videos"). A generated or retouched face could break a campaign's rules.
- A generated face of a real streamer is a likeness problem and could be judged misleading.

### The cover recipe

1. **Frame:** a real frame within about 1.5 s of the clip's peak, chosen for the face's expression and sharpness.
2. **Layout:** one of three families, picked by what the frame offers:
    - **Split:** face on top (about 45%), the gameplay moment below. Our default, as it matches our 9:16 render.
    - **Face-led:** close crop on the face, the line across the lower third. For pure reactions and talking clips.
    - **Moment-led:** gameplay fills the frame, the face as a cut-out in a corner. For crashes, chases and spectacle.
3. **Line:** 1 to 5 words in capitals, at most 2 lines and 14 characters per line. Either the quote ("SIX STARS?!") or the label ("FIRST CAR STOLEN"). It complements the title and never repeats it.
4. **Type:** one heavy sans (for example Anton or Montserrat Black), white with yellow for one key word, a thick black outline. On TikTok, a plain white box with black sentence-case text instead.
5. **Finish:** a small contrast and saturation lift (about +10%), mild sharpening of the face area, and a faint dark gradient behind the text. Nothing else.
6. **Brand:** a small, fixed "VI" neon mark in the same corner on every cover. Recognition over time without shouting. Campaign lanes can switch it off.

### Hard "no" list

- No generated or AI-retouched faces, no face-restoration models (they make skin look plastic).
- No invented objects, events or text the clip doesn't contain.
- No more than one emoji, no red arrows or circles, no stacked boxes.
- No story spoilers or ending reveals, matching our clip rules.
- No identical layout and colour for more than 3 covers in a row in the same grid.

## System design: the cover engine

The cover engine is a new stage on the Python worker. It runs after a clip renders and before it posts, and a missing or failed cover never blocks a post: the clip goes out with the platform's default frame.

*(The doc's diagram, "cover engine · 7 stages, 1 gate, 1 learning loop", in text:)* rendered clip → 0 cover-ready video → 1 mine frames → 2 score frames → 3 hook line → 4 compose → 5 QA gate (fail: next-best frame, max 3 tries, then the plain best frame) → 6 deliver per platform → posted; 7 learn reads the results and steers stages 3–4.

A failed check sends the clip back to the next-best frame (at most 3 tries), then falls back to the best plain frame with no text.

| Stage | What it does | Tools |
| --- | --- | --- |
| 0. Cover-ready video (in the renderer) | Facecam at least 40% of the height in the split layout. During the first 2 s and at the peak, show a full caption phrase, never a half word. This is what YouTube auto-picks from today | Existing renderer |
| 1. Mine frames | Take frames from the **clean layers** (facecam crop and gameplay crop before captions are burned in): 2 fps from 3 s before to 3 s after the peak, 1 fps for the rest | ffmpeg |
| 2. Score frames | Face: size, eyes open, expression intensity from blendshapes (jaw open, eyes wide, brows up). Image: sharpness (Laplacian variance), exposure (penalise dark night frames), motion blur. Gameplay half: detail and brightness. Weights live in config | OpenCV, MediaPipe Face Landmarker |
| 3. Write hook line | Input: transcript around the peak, title, streamer, event tags ("first time", police chase), last 20 lines used on that account. Output: 3 candidates in JSON, quote style and label style, under the rules in the formula | Cheapest text model that passes the eval; an open model is fine |
| 4. Compose | Render each of the 3 layout families with the top frame and top line, at 1080 x 1920, as HTML/CSS templates turned into PNG. Face cut-out only for the moment-led layout. Optional 2x upscale of a low-res facecam with a general upscaler, never a face-restoration model | Playwright (Chromium), rembg, Real-ESRGAN (optional) |
| 5. QA gate | Rule checks first: OCR reads the line back exactly at 15% scale, face present, exposure in range, perceptual hash differs from the account's last 9 covers, banned words. Then one vision check: matches the clip, not misleading, no spoiler, looks natural. Score 1 to 10, pass at 7 or more | PaddleOCR, imagehash, a cheap vision model |
| 6. Deliver | YouTube: `thumbnails.set` after processing succeeds, only while eligible. Instagram: `cover_url` from a public Supabase Storage URL. TikTok: `video_cover_timestamp_ms` at the best rendered frame. Campaign lanes use their own template settings (for example no brand mark) | YouTube Data API, Graph API or Zernio |
| 7. Learn | Log each cover's layout, line style and scores with the clip's results. A bandit shifts the share of each layout and line style per platform | Supabase, a nightly job |

### Data model (additive)

- `covers`: `id`, `clip_id`, `lane`, `platform`, `layout`, `line_text`, `line_style`, `frame_ts_ms`, `scores` (jsonb: face, expression, sharpness, exposure, ocr, vision), `phash`, `storage_path`, `status` (draft, passed, failed, applied, apply\_failed), `error`, `config_version`, `applied_at`.
- Config keys in the existing `scoring_config` table under a `cover.` prefix: on/off per platform and per lane, layout weights, pass score, brand mark on or off, `cover.youtube_custom_enabled`.

### Cost and load

Stages 1, 2, 4 and the rule checks run locally on the worker with open-source tools, so the main cost is CPU time, a few seconds per clip. The paid parts are one short text call and one image check per clip. On a cheap model that should be small next to the current clip-scoring spend; confirm with the provider's price sheet before switching it on.

## Measurement and learning loop

A cover's effect is hard to isolate, because most Shorts, Reels and TikTok views come from the feed, where no cover shows. So we measure it with a randomised split and patience, not by reading single clips.

- **No native A/B test for Shorts.** YouTube's Test & Compare is for long-form only, and it ranks by watch time share rather than clicks ([YouTube help](https://support.google.com/youtube/answer/13861714)). We run our own split.
- **Randomise the arms.** For each clip, the engine picks the layout family and line style with a bandit (Thompson sampling) per platform. Keep at least 20% exploration so no arm dies early. Log the arm on the `covers` row.
- **What we compare.** Per arm, per platform: `views_vs_account_median` at 48 h (already planned in `clip_performance`). On YouTube also the share of views from non-feed sources (search, channel page, browse, subscriptions), taken from the Analytics API traffic-source report. That share is where a thumbnail can actually move numbers.
- **Volume check.** At 10 YouTube posts a day plus hourly TikTok and Instagram posts, we get a few hundred covers a month per platform. That's enough to spot big differences between 3 layouts within a month, not small ones. Don't add more arms than that.
- **Human review.** Add a "Covers" tab to the dashboard showing the last 30 covers per account as the platform grid shows them (3:4 crops for TikTok and Instagram). Look at it weekly; anything that looks samey or sloppy becomes a new rule in the QA gate.
- **Weekly report.** Add a covers section to `docs/reports/<week>.md`: QA pass rate and top fail reasons, arm results with sample sizes, and the 5 best and worst covers.

Open question: whether YouTube's Reporting API gives thumbnail impressions and click-through for Shorts. If it does, use that as the main YouTube metric instead of the traffic-source share.

## Milestones for Claude Code

Build in this order. Each milestone ships behind a `cover.*` flag on staging first, follows the existing safety rules (snapshots, additive migrations, changelog), and can be turned off without a redeploy.

1. **M1: Cover-ready video.** Bigger facecam in the split layout, a full caption phrase in the first 2 s and at the peak, no half-word captions. Done when 20 staging clips, checked as frames, show a readable phrase and a visible face at the peak. This is the only thing that helps YouTube before we join the Partner Program.
2. **M2: Frame miner and scorer.** Save the clean facecam and gameplay layers from the renderer, extract frames, score them, store the top 5 per clip. Done when the top frame beats a random frame in a blind check of 30 clips.
3. **M3: Hook line writer.** Prompt, JSON output, diversity against the last 20 lines, banned-word list. Done when 50 generated lines pass the formula's rules with no manual fixes on 45 or more.
4. **M4: Compositor.** HTML/CSS templates for split, face-led and moment-led, plus the TikTok white-box style; fonts bundled; 1080 x 1920 output with the 3:4 safe band. Done when the 3 layouts render for 30 clips in under 5 s each.
5. **M5: QA gate.** OCR, face, exposure and hash checks, then the vision check with its rubric and the fallback chain. Done when the gate catches every planted bad cover in a test set (wrong text, no face, dark frame, duplicate).
6. **M6: Delivery.** Instagram `cover_url` first (it works today), then the TikTok timestamp, then YouTube `thumbnails.set` behind `cover.youtube_custom_enabled`. On a 403, turn that flag off and alert on Telegram; on a 429 rate limit, retry later. Per-lane template settings for the campaign lanes.
7. **M7: Learning loop and dashboard tab.** Bandit assignment, the Covers grid tab, the weekly report section.

## Risks and open questions

| Risk | What we do |
| --- | --- |
| Covers start to look like one template | The diversity check against the last 9 covers, 3 layout families, varied line styles, and the weekly grid review |
| The line overpromises (clickbait) | The vision check compares the line with the transcript; a line must describe something that happens in the clip |
| A paid campaign rejects edited or AI material | Per-lane setting to use the plain best frame with no text or mark; read each campaign's brief before enabling covers for it |
| TikTok's stitched cover frame flashes before the hook | Off by default; test on 10 posts and watch the first-second retention before using it |
| The extra stage slows the live fast lane | The cover runs in parallel with upload prep; if it isn't ready within 60 s, post with the default frame |

Open questions:

- Do we post TikTok and Instagram through Zernio or the platform APIs directly? It decides which cover fields M6 uses.
- When will the YouTube channel reach the Partner Program, and does the lower tier unlock custom Shorts thumbnails?
- Do any current campaign briefs (TOMO, Ruckus, Lacy, Clix) say anything about thumbnails or covers?

## Sources

- [YouTube blog: custom thumbnails for Shorts](https://blog.youtube/news-and-events/making-thumbnails-easier-on-youtube/)
- [YouTube Data API: thumbnails.set](https://developers.google.com/youtube/data/reference/rest/v3/thumbnails/set)
- [PPC Land: Shorts thumbnails, no A/B testing](https://ppc.land/youtube-ends-2-year-wait-for-shorts-thumbnails-but-blocks-a-b-testing/)
- [vidIQ: where Shorts thumbnails appear, safe zone](https://vidiq.com/blog/post/youtube-shorts-custom-thumbnails/)
- [Meta: IG User Media (cover\_url, thumb\_offset)](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media)
- [TikTok: Content Posting API, Direct Post](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post)
- [Zernio: YouTube publishing](https://docs.zernio.com/platforms/youtube)
- [YouTube help: Test & compare thumbnails](https://support.google.com/youtube/answer/13861714)
- [YouTube help: monetization and inauthentic content](https://support.google.com/youtube/answer/1311392)
- [Uppbeat: are AI thumbnails putting viewers off?](https://uppbeat.io/blog/creator-questions/are-ai-thumbnails-putting-viewers-off-your-videos)
- [Planoly: Instagram's vertical grid](https://planoly.com/blog/guide-to-instagrams-new-vertical-grid)
- Competitor examples: YouTube search (Shorts filter) for "gta 6 streamer clips", "xqc gta 6", "ishowspeed gta 6", "kai cenat gta 6"; TikTok search for "kai cenat gta 6"; our channel's Shorts tab. All viewed 23 Nov 2026.

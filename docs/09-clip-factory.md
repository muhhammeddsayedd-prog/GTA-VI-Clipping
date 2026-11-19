# 09 — Clip Factory

Turns a source window into a finished 1080×1920 MP4. Jobs `clip.render` (normal) and `express.render` (express). Same code, different presets.

## 1. Pipeline

```
source window (+context pad)
  → transcribe (local faster-whisper for normal, hosted API for express) → words with timings
  → translate to English if needed (P8)
  → Claude clip writer (P2): trim, hook, title, caption, hashtags, keywords, topic, spoiler/safety, score
  → music detection (PANNs) on the final window
  → profanity detection on words
  → facecam/gameplay boxes (cached per creator; P4 vision if missing)
  → build subtitles (.ass) + overlays
  → ffmpeg render
  → loudness normalise
  → quality gate (docs/10)
  → upload to Storage, update clip row
```

## 2. Transcript

- Normal: faster-whisper `medium` int8 on CPU, `word_timestamps=True`, `vad_filter=True`, language auto-detect.
- Express: hosted Whisper (Groq first, OpenAI fallback) with word timestamps; send 16 kHz mono audio extracted with ffmpeg.
- Output format (stored in `clips.transcript`):
```json
{"language":"en","words":[{"w":"no","s":12.34,"e":12.51},{"w":"way","s":12.51,"e":12.80}]}
```
Times are relative to the **window start** (context pad included; the writer's trim decides the final in/out points).

- Non-English: P8 translates segment by segment; English words are spread evenly over each segment's time span.

## 3. Layouts (1080×1920)

**A — facecam found** (`creator_layouts.facecam_box` not null):
| Region | Placement |
|---|---|
| Gameplay | Crop `gameplay_box` (default full frame minus facecam area if it's an overlay) to aspect 1080:1150, centre-weighted; scale to 1080×1150 at y = 0 |
| Facecam | Crop `facecam_box`, crop-to-fill 1080×770, at y = 1150 |
| Seam | 6 px accent-colour line at y = 1147 |

**C — facecam found, facecam on top** (decision D112; the layout of every design style since migration 0027):
| Region | Placement |
|---|---|
| Facecam | Crop `facecam_box`, crop-to-fill 1080×672, at y = 0 |
| Gameplay | Crop like layout A to aspect 1080:1248, at y = 672 |
| Seam | 6 px line at y = 669; the progress bar runs along it |
| Text | Captions centred on y = 1400; the title bar's bottom 20 px below the seam; payoff emoji at y = 1000 |

The style (`factory.styles` → `rules.facecam_top`) decides between A and C; detection only decides whether there is a facecam.

**H — news card** (style `headline_card`, `rules.frame`): black 1080×1920; the brand header and headline drawn from y = 300; the source cropped to 4:3 (moved sideways to keep the facecam) and scaled to 1080×810 at y = 630; credit bottom at y = 1480.

**P — POV crop** (style `pov_fullframe`): the source cropped to 3:4 away from the facecam and scaled to 1080×1440 at y = 240, black bars above and below; the POV line centred on y = 900; credit bottom at y = 1480.

**B — no facecam**:
| Region | Placement |
|---|---|
| Background | Full source scaled to cover 1080×1920, `boxblur=30:3`, darkened 30% |
| Gameplay | Crop source centre to 4:5, scale to 1080×1350 at y = 285 |

### Facecam detection (P4)
Grab one frame at the window middle; send to the writer model with vision; it returns facecam box (or null) and gameplay box in source pixels. Cache in `creator_layouts` for 7 days; re-detect if confidence < 0.6 or when the source resolution changes.

## 4. Subtitles (.ass via libass)

- Style file `assets/subtitles/style.ass.tmpl` generated from settings: font `factory.font` (Montserrat Black), size 78, white primary, black outline 6, shadow 3 (soft, 50% alpha), bold.
- Lines of 2–4 words, max ~22 characters, broken at pauses > 0.35 s or punctuation.
- Current word highlighted in `factory.accent_color` using per-word `\c` overrides with `\k`-style timing (one Dialogue event per word-state is simplest and most robust).
- Keywords (from P2, 1–2 per line): accent colour + pop: `\t(0,80,\fscx118\fscy118)\t(80,160,\fscx100\fscy100)`.
- Position: Layout A centre-x, y = 1150 (the seam); Layout B y = 1450. Never above `safe_top_px` (220) or below `1920 − safe_bottom_px` (1500).
- Profanity masked: first letter + asterisks (`f***`).

## 5. Overlays

| Overlay | Spec |
|---|---|
| Hook | First `factory.hook_seconds` (3 s): white rounded box, black text, max 2 lines, y = 260–420, pop-in 150 ms |
| LIVE badge | Spike-lane clips only: red pill "● LIVE · JUST HAPPENED", top-left at (40, 240), whole clip |
| Credit | "🎥 @creator" small (34 px), white 85%, bottom of gameplay area, whole clip |
| Watermark | `@6starsclips` 30 px, 60% opacity, top-right (1040 right-aligned, 240) after the hook ends |
| Progress bar | 8 px accent bar at y = 1500 growing left→right over the clip |
| Punchline zoom | Last 40% of the clip: slow zoom 100→108% on the gameplay region (`zoompan` or `scale`+`crop` with `t`) |
| Keyword SFX | Short CC0 "pop" at each keyword time, −12 dB |
| End card | `factory.end_card_s` (1.5 s): dark card, "6 STARS CLIPS", "Moments #<series_no>", "Follow for more" |

## 6. Audio

1. Bleep: for each profanity word, `volume=0` over `[s−0.05, e+0.05]` and mix a 1 kHz sine at −20 dB for the same span.
2. Music: segments flagged as music (hourly clips under the limit) get `factory.music_duck_db` (−18 dB).
3. Keyword SFX mixed in.
4. `loudnorm=I=-14:TP=-1.5:LRA=11` two-pass for normal, single-pass for express.

## 7. Encode

- `libx264 -preset <preset> -crf <crf> -profile:v high -pix_fmt yuv420p -r 30 -movflags +faststart`, AAC 128 kb/s 48 kHz.
- Express: `factory.preset_express`; if `factory.use_nvenc` and GPU present: `h264_nvenc -preset p4 -cq 23`.
- Output ≤ 60 s, typically 8–25 MB.
- Record `render_ms`.

## 8. Music detection

`panns-inference` AudioTagging on 1-second frames of the final window; a frame is music if the "Music" class probability > 0.5 or any of "Song", "Singing", "Rapping", "Pop music", "Hip hop music" > 0.4. `music_ratio = music frames / total`. Store per-frame flags to drive ducking.

## 9. Files

- Scratch: `$DATA_DIR/render/<clip_id>/` (deleted after upload).
- Storage key: `clips/<env>/<yyyy>/<mm>/<dd>/<clip_id>.mp4`.
- Console thumbnail: right after the render (before the gate) one JPEG frame of the finished clip is uploaded to `clips/thumbs/<env>/<yyyy>/<mm>/<dd>/<clip_id>.jpg` and saved in `clips.thumb_path` (`factory/thumbs.py`, settings `console.thumb_*`). A failure is recorded as `clip.thumb_failed` and never fails the render (docs/26, Console media).
- Keep a local copy in `$DATA_DIR/ready/<clip_id>.mp4` until posted (posting uploads the local file to Upload-Post).

## 10. Editing style (`factory.style`)

Sections 4 to 6 describe the classic look. With the setting `factory.style` (migration 0021) the factory edits like the viral references of docs/27: captions of one or two words in Anton capitals that pop in on the seam, the hook as a title bar for the whole clip, hard punch-ins to a full-frame facecam on the loudest moments (an extra `ebur128` pass over the window audio places them), a step zoom on the gameplay every 2.5 s, whoosh/impact on each punch-in and an emoji with a bass hit at the clip writer's `payoff_s`, a compressor before loudnorm, and no end card. Layout A keeps gameplay on top. Every decision is logged as `clip_edit_effects`. Without the setting the classic look is unchanged.

## 11. Design styles (development-plan/M18)

Since migration 0027 the look comes from a catalogue of styles (`factory.styles`, docs/27 section 5): each is a set of the section 10 rules plus `facecam_top`, `captions` and `title_whole_clip`. Before the render the style director (`factory/director.py`) gives each enabled platform account a style; the clip is rendered in the reference account's style (`clips.style_id`, `clips.layout_kind`, `clips.style_plan`) and recorded in `clip_renders`. When other accounts got other styles, the clip's span of the source is copied to `$DATA_DIR/variants/<clip_id>/` (the source itself can be deleted as soon as the render ends) and the job `clip.variants` renders those styles after the gate approved the clip, to `$DATA_DIR/ready/<clip_id>.<style>.mp4`, checks each file (frame size, length, black and frozen picture in the style's gameplay band, docs/10) and uploads it. The publisher sends each account the ready render of its style, otherwise the first render (`posts.render_id`). `sixstars clip render-file --style <id>` renders a local file in one style (`--title-line`, `--headline`, `--question`, `--pov` give it the writer's texts).

Phase B: the style is picked after the clip writer and the layout detection (`produce(choose_style=...)`), because a style may need a facecam or one of the writer's texts (`moment_type`, `title_line`, `headline`, `question`, `pov_line`, stored in `clips.moment_type` and `clips.style_texts`). The texts are drawn with Pillow (`factory/cards.py`) like the title bar.

## 12. Story cuts (development-plan/M18 Phase C, decision D113)

Besides the short clip, the clip writer may propose a longer cut of the same moment (`story_start_s`, `story_end_s`; 60-150 s, `factory.story`) when the window holds a real story. To give it room, hourly normal-mode renders of VOD and live sources get a wider source window: the VOD section and the live cut start `pad_before_s` (90) earlier and end `pad_after_s` (30) later, and the writer reads all of it. Express renders never get one.

After the short clip is rendered, the same job renders the story (`pipeline.render_story`) as its own `clips` row (`version = 'story'`, `parent_clip_id` the short clip, lane hourly), from the same source, transcript, writer reply and layout, snapped like the short cut (`writer.final_window` with the story's limits). Its style is planned for the moment type `story` (favouring `cam_top_wordcap` and `cam_top_title`). It is recorded in `clip_renders`, gets its own variants, and a `clip.gate` job. A story that cannot be made is rejected on its own; the short clip is never affected. One story per short clip (unique index).

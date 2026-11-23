# M19 — Render v2 (no zoom, gameplay-first layout, verified facecam)

Owner's master prompt of 2026-11-23, copied as given. Numbering notes for this repo:

- The decision is **D119** (D117 = AI cost plan and D118 = paid clipping campaigns were taken first).
- Migration numbers: 0038-0040 landed on develop meanwhile (AI cost A5a/A5b, titles v2) and 0041/0042 are
  held by the in-flight `ai-cost-b1` branch, so R0's migration is **0043_render_v2_no_zoom.sql**; later
  milestones take the next free number when they land. R1's settings migration is **0054** (0053 is held
  by the campaign-posting work).
- The code facts below were read at main 874d640; this work branches from develop (73347db and later).
  Differences are recorded in `PROGRESS.md`.

---

Claude Code master prompt: Render v2 (no zoom, gameplay-first layout, verified facecam)

You are the main coding agent for 6 Stars Clips. Read CLAUDE.md first and follow it: uv and Python 3.12, ruff and mypy --strict must pass, tests with pytest, no hard-coded thresholds (every number below is a setting), migrations in supabase/migrations/, progress notes, develop → staging, main → production.

This job rebuilds how a clip is framed and drawn. It is split into milestones R0 to R7. Do them in order. Each one must be tested and committed, with progress notes, before the next starts. Stop and report to me at the two checkpoints marked STOP. Do not change production settings or merge to main without my OK.

## 0. Setup

Branch render-v2 off the latest develop. Pull first: the code facts below were read at main commit 874d640 (21 Nov 2026). If something has moved, follow the code and note the difference.
Copy this prompt to development-plan/M19-render-v2.md. Add decision D117 (or the next free number after D116) to docs/00-decisions.md: "Render v2: static gameplay-first split (G) and no-cam frame (G0); zoom, punch-ins and the H/P/M/A frames removed; facecam verified by a face detector".
The full audit is in the doc "Render & layout audit: 6 Stars Clips vs competitors" (23 Nov 2026). The summary in section 1 is enough to work from.

## 1. Why (audit findings you are fixing)

We sampled 9 of our published Shorts frame by frame and compared them with the top GTA VI streamer-clip Shorts this week (5.3M, 1.3M, 359K and 116K views).

| # | Problem | Where in the code |
|---|---|---|
| 1 | Every clip zooms: the gameplay grows 100%→108% over the last 40% | `RenderSpec.zoom: bool = True` (factory/render.py:80) → `layout.scale_filters(zoom=True)`, `window.ZOOM_MAX = 1.08`, `zoom_start_s` |
| 2 | cam_top_viral(_follow) steps the zoom 100%↔108% every 2.5 s | `ZoomCut`, `zoomcut_every_s` / `zoomcut_scale` in factory.styles |
| 3 | Up to 4 full-screen 1.25× face punch-ins: a 9:16 slice of a landscape cam box (about 136 px wide) scaled about 10×. 38 of 150 published clips have them | `PunchIns`, `_punch_filters`, `effects.select_punchins`, `pipeline._punch_spans`, `punchin_*` rules, `cut_sfx` |
| 4 | Faces cut: the cam panel forces 1.61:1 (1080×672) with no idea where the face is, so 4:3 and square cams lose forehead or chin | `fit_crop(layout.facecam_box, g.width, face_h, …)` in `layout_filters` |
| 5 | Faces soft: cam boxes upscaled 3.0× on average, up to 4.7×, with the default scaler | `scale_filters` uses plain `scale` |
| 6 | Wrong facecam: Joe Bartolozzi's panel shows a gameplay wall, stableronaldo's a chin and game UI. 13 published clips used confidence < 0.7 | facecam.py: one frame → P4 → cached per creator for 7 days; `MIN_CONFIDENCE = 0.6` (hard-coded, and 0.60 passes); no face check |
| 7 | Gameplay too tight: C shows 49% of the source width (45% when zoomed); the streamer's chat and alerts sit inside our crop | `C_FACECAM_H = 672`; `gameplay_crop` avoids only the facecam box |
| 8 | Gameplay too small: the H card is 42% of the frame with 58% black; M and P letterbox with black bars | `LayoutCard`, `LayoutMontage`, `LayoutPov`, `_framed_filters`, `_montage_filters` |
| 9 | Clutter on the face: the LIVE · JUST HAPPENED pill (also on replays), the watermark at y=240 inside the cam panel, the title pill across the seam, payoff emoji, pink progress bar, captions, follow line, all at once | overlays.py (`LIVE_BADGE_TEXT`, `badge_events`, `BADGE_Y`, `WATERMARK_Y`, `progress_filters`), pipeline.py (`live_badge = job.lane == "spike"`, title bar, `payoff_fx`) |
| 10 | 10 styles under a bandit with 1–14 YouTube posts each, so none can be judged | factory.styles, factory.style_director |

What every winning competitor does: cam on top at 35–50% of the height, gameplay below at 50–65%, gameplay never zooms or moves, the whole face with headroom, one caption on the seam, and almost nothing else on screen.

## 2. Decisions (made; don't revisit)

- All zoom is removed. Delete the code, don't just set it to 0. That covers the tail zoom, zoom cuts, punch-ins, their sound effects (whoosh/impact on punch-ins) and any zoom inside the cam panel. After R0 there is no code path that changes scale over time.
- Two frames only:
  - **G** (gameplay-first split), used when a facecam is verified: cam panel 1080×720 at y=0, gameplay panel 1080×1200 at y=720.
  - **G0** (no verified cam): gameplay 4:5, 1080×1350 at y=285, over the existing blurred and darkened background of layout B.
- Retire layouts A, C (replaced by G), B (replaced by G0), H, P and M. Retire the styles cam_top_viral, cam_top_viral_follow, hype_montage, headline_card and pov_fullframe. cam_top_title(_follow), cam_top_wordcap(_follow) and cam_top_question become text treatments on G/G0. The style director may vary only the text (word captions vs a full line vs a question line, and the follow line), never the frame or any motion.
- One text element on screen at a time, plus the small brand mark and the creator credit. The LIVE pill, payoff emoji, progress bar and seam line are gone.
- The facecam is trusted only after a face detector confirms it. If the cam can't be verified, the clip renders as G0, never with a wrong panel.
- The cam is 37.5% of the frame. This supersedes the cover-engine prompt's rule that the facecam panel must be "at least 40% of the frame height". Its face-size rules still apply.

## 3. Settings (new keys, all in settings, documented in docs/16-settings-reference.md)

Add them in one migration (R1). Read them through the existing config loaders (factory/config.py, gate settings), never as constants.

```json
"factory.layout_v2": {
  "enabled": false,
  "cam_h": 720,
  "g0_game_h": 1350,
  "g0_game_y": 285,
  "max_slide": 0.25,
  "seam_y": 720,
  "safe_right_x": 920,
  "safe_bottom_y": 1540,
  "cam_face_center_y": 0.45,
  "cam_face_h_min": 0.40,
  "cam_face_h_max": 0.60,
  "cam_fill_blur": 30,
  "cam_fill_darken": 0.30,
  "cam_unsharp": 0.5,
  "scaler": "lanczos"
},
"factory.text_v2": {
  "font": "Anton",
  "size_min": 110,
  "size_max": 130,
  "outline_px": 9,
  "color": "#FFFFFF",
  "key_color": "#FFD400",
  "max_words": 3,
  "hook_s": 2.5,
  "mark_text": "6 Stars Clips",
  "mark_size": 34,
  "mark_opacity": 0.7,
  "mark_x_right": 1040,
  "mark_y": 740,
  "credit_size": 30,
  "credit_x": 40,
  "credit_y": 740
},
"facecam.detect": {
  "min_confidence": 0.75,
  "check_frames": 5,
  "min_face_hits": 3,
  "min_face_share": 0.25,
  "detector_score": 0.6,
  "cache_scope": "source",
  "creator_prior_days": 7
},
"gate.render_v2": {
  "face_frames": 8,
  "face_hit_share": 0.8,
  "face_min_panel_share": 0.30,
  "min_game_share_g": 0.60,
  "min_game_share_g0": 0.70,
  "max_black_band": 0.05
}
```

`factory.layout_v2.enabled` is the master switch: false keeps today's renderer until R7. On staging, set it to true from R2 on.

## 4. Milestones

### R0: remove zoom and punch-ins (can ship on its own)

In factory/layout.py:
- Delete `ZoomCut`, `PunchIns`, `_punch_filters` and the zoom branch of `scale_filters`. `scale_filters` keeps only the plain scale and takes the scaler from settings (Lanczos).
- Remove the `zoom`, `zoom_cut` and `punch_ins` parameters from `layout_filters`, `_framed_filters` and `_montage_filters`.

In factory/window.py: delete `ZOOM_MAX`, `ZOOM_TAIL_FRACTION` and `zoom_start_s` (check other users first).
In factory/render.py: delete `RenderSpec.zoom`, `zoom_cut` and `punch_ins` and their wiring.
In factory/pipeline.py: delete `_punch_spans` and its use, the `sfx_spans` fallback for punch-ins, and the `ZoomCut(...)` / `PunchIns(...)` construction (lines ~620–680).
In factory/effects.py: delete `select_punchins` and the punch-in cues in the SFX timing. Keep the B-roll cut SFX only if montage is still alive at this point; it goes in R1.
In factory/style.py: delete the rules `punchin_max`, `punchin_zoom`, `punchin_len_s`, `punchin_min_gap_s`, `punchin_db`, `zoomcut_every_s`, `zoomcut_scale` and `cut_sfx`, plus the `punchins` and `zoomcuts` properties and `VIRAL_STYLE`'s zoom keys. `StyleRules.from_setting` must ignore these keys if old settings still carry them (log once, don't fail).

Migration 0037_render_v2_no_zoom.sql (or the next free number): strip those keys from factory.style and from every entry in factory.styles, and set `enabled: false` on cam_top_viral and cam_top_viral_follow. Keep scoring_config/settings history as the repo does for settings changes.

Tests:
- Remove or rewrite the zoom and punch-in tests in tests/unit/factory/test_factory_style_effects.py, test_factory_render.py, test_factory_layout_audio.py, test_factory_params_window.py and test_design_styles*.py.
- Add tests/unit/factory/test_no_zoom.py: for every style in the catalogue and every layout kind, the built filter graph contains no `eval=frame`, no `zoompan`, no time-dependent scale/crop expression (no `t` in a scale/crop argument) and no overlay with `enable=between(…` for a close-up.
- Add a real-ffmpeg check in test_factory_render_ffmpeg.py: render a 6 s synthetic clip and assert that frames at 0.5 s, 3 s and 5.5 s show the same framing (a static test pattern stays pixel-aligned within 1 px).

Done when ruff, mypy and the full test suite pass, and 5 staging renders show identical framing at the start, middle and end. Commit, progress note, and tell me R0 is ready: I will decide whether R0 alone goes to production immediately.

### R1: geometry of G and G0, retire the other frames

In factory/layout.py:
- Add `LayoutKind` values "G" and "G0" and the dataclasses `LayoutG(facecam_box: Box, face_box: Box | None, overlay_boxes: tuple[Box, ...])` and `LayoutG0(overlay_boxes: tuple[Box, ...])`.
- Extend `Geometry` from factory.layout_v2 (`cam_h`, `g_game_h = height - cam_h`, `g0_game_h`, `g0_game_y`, scaled to the output canvas like today).
- Add `gameplay_band("G")` and `gameplay_band("G0")` for the gate.
- Keep the old classes only as long as `factory.layout_v2.enabled = false` needs them. Delete them in R7.

Gameplay crop (pure function `gameplay_crop_v2`):
- Take the largest crop of aspect 1080:g_game_h (G) or 4:5 (G0) at full source height, starting centred.
- If it overlaps the facecam box or any overlay box, slide it horizontally to the nearest position that overlaps none, moving at most `max_slide` × source width from the centre.
- If no position is clear, take the one with the least overlapping area within the slide limit.
- Even-pixel offsets. One crop for the whole clip.
- Unit tests: cam left, cam right, cam bottom-right overlapping the centre, chat box on the right, nothing clear.

G filter graph:
- [cam panel 1080×cam_h] vstack [gameplay 1080×g_game_h], both scaled with Lanczos.
- The cam panel comes from R2's framing; until R2 lands, use today's `fit_crop` of the cam box.
- Remove the seam line and the progress bar for G and G0.

G0 filter graph: today's layout B background chain (downscale, boxblur, darken, upscale), with the 4:5 gameplay overlaid at g0_game_y.

`pipeline.framed_layout`: when `factory.layout_v2.enabled`, return `LayoutG` if the detection gives a verified cam (from R3; until then, any `LayoutA`), otherwise `LayoutG0`. Ignore `style.frame` and `style.facecam_top`.

`variants.recipe_json` / `layout_from_recipe`: store kind, facecam_box, face_box and overlay_boxes. Old recipes without them load as G (cam box present) or G0.

Styles: a migration disables hype_montage, headline_card and pov_fullframe and removes their affinities from factory.style_director. Merging the text treatments happens in R4.

Console (web/): wherever layout kinds or style names are shown (ClipBits.tsx and friends), add G and G0 and stop showing retired styles as choices.

Done when unit tests cover the geometry and crops, and staging renders of 3 cam clips and 2 no-cam clips match the spec sizes (check with ffprobe and the frame-level checks in R5).

### R2: face-aware cam framing

Detector: OpenCV YuNet (`cv2.FaceDetectorYN`) through opencv-python-headless. Bundle the ONNX model in assets/models/ with its licence, and record the version in docs/14. Prefer it over MediaPipe: smaller, CPU-only, and fits our Docker image. Check the current OpenCV docs before coding, as CLAUDE.md requires, and note the version used. Wrap it in factory/faces.py with a fake for tests. No network.

Framing (pure function `cam_window(face_box, cam_box, panel_w, panel_h, rules) -> CamWindow`):
- Use the median face box over `check_frames` frames sampled across the clip, inside the cam box.
- Build a window of aspect panel_w:panel_h (3:2 at defaults). Its height is set so the face height falls between `cam_face_h_min` and `cam_face_h_max` of the panel, as close to the middle as the cam box allows. Centre the face horizontally, with the face centre at `cam_face_center_y` of the panel height.
- Where the window extends past the cam box, those pixels come from a blurred fill: the cam crop scaled to cover the panel, boxblur `cam_fill_blur`, darkened by `cam_fill_darken`. The sharp cam crop is overlaid on top.
- Never take pixels outside the cam box from the source. That is how gameplay or overlays leak into the panel today.
- Never cut the face. If the face can't fit at `cam_face_h_min`, shrink the window until it fits and accept a smaller face.
- Return the source crop box, the scale, and where the sharp crop sits inside the panel.

Filter: crop → Lanczos scale → `unsharp=5:5:{cam_unsharp}` on the cam only → overlay on the blurred fill → the panel.

No face found at render time (detector misses in all sampled frames): fall back to G0, log `cam_no_face`, write an events row.

Unit tests for `cam_window` with real box numbers from production creator_layouts (see section 6): Jerma 248×246 at (0,651); MissMikkaa 518×608 at (1402,472); Kai Cenat 393×222 at (0,432); a 16:9 cam; a face near the box edge; a tiny face. Assert that the face box is fully inside the panel in every case and that its height share is within the limits.

Done when the unit tests pass and staging renders of Jerma-, MissMikkaa- and Kai-style sources show the whole face with headroom (check with R5's face check).

### R3: facecam detection v2

P4 prompt and schema:
- Extend ai/prompts/p4_layout_detect.md and `ai.schemas.Layout` with `overlays: list[PixelBox]`: chat boxes, alert and sub-goal widgets, follower/sub bars, platform banners and the streamer's other on-screen panels, but not the game HUD.
- Bump the prompt version the way the repo does it, and keep `layout_schema` bounds for the new list.
- Ask for the cam box to cover the whole webcam picture (its frame edges), not just the face.

Detection (facecam.py):
- Grab `check_frames` frames spread over the clip window. Send the middle frame to P4 (one call, as today).
- Run the face detector on all frames, inside the returned cam box.
- Accept the cam when P4 confidence ≥ `min_confidence` and a face with score ≥ `detector_score` is found in ≥ `min_face_hits` frames and the median face height is ≥ `min_face_share` of the cam box height.
- Otherwise run the face detector on the whole frame once. If one face sits in a consistent corner region in ≥ `min_face_hits` frames, propose a cam box around it, padded to the nearest strong edges (a simple edge/variance search is enough). Accept it if the face check passes, and log `cam_box_from_face`.
- Otherwise use G0.
- Delete the hard-coded `MIN_CONFIDENCE`, `FULL_FRAME_SHARE` and `MIN_FACECAM_SHARE`; read them from facecam.detect (add the last two as keys too).

Cache:
- Key by source (`clips.source_id` for VODs and Twitch clips, the live session id for live), with columns `source_key`, `face_box`, `overlay_boxes` and `face_check`.
- Keep a per-creator row as a prior: try the creator's last accepted box first, and accept it without a P4 call when the face check passes on this clip's frames. That keeps P4 cost flat.
- A failed face check on any clip invalidates that source's row.

Migration (0038_render_v2_detection.sql):
- Add `source_key text`, `face_box jsonb`, `overlay_boxes jsonb not null default '[]'` and `face_check jsonb` to creator_layouts.
- Change the unique key to allow one row per (creator, source_key) plus the creator prior. Pick the smallest schema change and explain it in the progress note.
- Then delete all existing rows (363 today, 52 of them below 0.7 confidence). They were made without a face check. Additive except for that cleanup; follow CLAUDE.md for migrations.

Events: `facecam_accepted` / `facecam_rejected` (with the reason: low_confidence, no_face, face_too_small) / `cam_box_from_face`, carrying clip_id and creator_id.

Latency: the spike lane targets "posted within 30 minutes". Face checks must add < 3 s per clip on the server CPU. Measure and report it.

Done when:
- Unit tests with the fake detector cover accept, reject, the face fallback and the cache prior.
- On staging, re-detection of the creators of section 6 gives: Joe Bartolozzi → G0, or G with a real face. Never the gameplay wall. stableronaldo → G0, or a box around his actual face. Jerma, Kai and Elajjaz → G with the face inside.

### R4: text and overlays

One text system on the seam (factory.text_v2):
- Hook line from 0 to `hook_s`, centred on `seam_y`: the writer's hook, a whole phrase, max 2 lines, no cut word.
- Word captions after that, also centred on `seam_y`: 1 to `max_words` words, white, `outline_px` black outline, the active or key word in `key_color`, sized between `size_min` and `size_max` to fit 960 px. Captions never split a word.
- G0: hook at the top (y 160–280), captions centred at y≈1490.
- Never draw text at x > `safe_right_x` or y > `safe_bottom_y`.

Treatments replace the old text styles:
- `wordcap`: hook, then word captions.
- `line`: hook, then one full caption line at a time (from cam_top_title).
- `question`: the writer's question as the hook, then word captions (from cam_top_question).
- `*_follow`: the existing follow line on the seam for the last `follow_overlay_s`.
- In factory.styles, these become the only enabled styles, all with frame G/G0, and keep their current weights. The director and bandit keep working, now over text treatments only.

Remove for v2:
- The LIVE pill: delete `LIVE_BADGE_TEXT` and `badge_events` wiring for v2; `live_badge` is always false.
- The title pill / title bar for the whole clip.
- The payoff emoji and its bass hit (`payoff_fx`).
- The progress bar and seam track.
- The hook box in the top band.

Keep:
- Brand mark: `mark_text` right-aligned at `mark_x_right`, `mark_y`, `mark_size`, `mark_opacity`, inside the gameplay panel's top-right, never on the face. Off where the lane's rendering says no watermark (campaign lanes, if lane render settings exist in the code by now; otherwise make it a per-lane setting `factory.text_v2.mark_lanes`, default `{"*": true}`).
- Creator credit: 🎥 @handle at `credit_x`, `credit_y`, `credit_size`, top-left of the gameplay panel. It matters for clipping campaigns.
- Profanity masking and bleeps: unchanged.

ASS/subtitles (subtitles.py, overlays.py, cards.py): add the G/G0 positions and delete the C/A/H/P/M positions in R7.

Done when unit tests cover the text positions, the safe zone and the one-element-at-a-time rule (assert from the ASS events: no two text events overlap in time, except the mark and the credit). Staging renders look like section 5.

### R5: QA gate for v2

In gate/visual.py and gate/checks.py, settings gate.render_v2:

Face check on the rendered cam panel (G only):
- Sample `face_frames` frames across the clip and run the face detector inside the cam panel.
- Fail if a face is found in fewer than `face_hit_share` of the frames, if the face box touches the panel edge (within 4 px), or if the median face height is below `face_min_panel_share` of the panel.

Geometry: use ffprobe size, and the black-band detection on rows. Gameplay must be ≥ `min_game_share_g` (G) or ≥ `min_game_share_g0` (G0) of the frame. No black band taller than `max_black_band`.
Gameplay band: use `layout.gameplay_band("G" | "G0")` for the black/freeze detection.
No motion: the R0 test guards the code. Also log a warning if a G/G0 render was produced with any old zoom key present in its style rules.
On a fail: re-render the clip once as G0 (log `gate_fallback_g0`). If G0 also fails, use the normal gate reject path.

Tests: planted bad renders. Make each with ffmpeg in the test: a panel with no face, a face cut at the top edge, a tiny face, gameplay at 42% with black bars, a black band of 10%. Every one is rejected. Good renders from R2 pass.

### R6: golden set and contact sheet. STOP after this.

Pick 20 clips. Start with these published Shorts and find each clip id with `select clip_id from posts where post_url like '%<video id>%'`:
- Kai Cenat H1vZZSXUYSU
- MissMikkaa -jzScf0YxZU and 48cQoHwvtqc
- Elajjaz GYIEuvb8kHA
- Jerma985 aBFaHvYmtM8
- Vargskelethor qqNRnZuein8
- stableronaldo nGEXB61T2XI
- Joe Bartolozzi G-9RmlmgHTM and 3LKAGgU_pJk
- Ludwig FEcD1bBPbDI

Add 10 approved clips from the current queue, with as many different creators as possible.
Re-render all 20 on staging with v2. Use their stored source copies; if a source copy is gone, skip it and say so.
Write docs/reports/render-v2.md with a contact sheet per clip: old render vs new render, 4 frames each (10%, 35%, 60%, 85%), plus layout kind, face check numbers, crop slide in px, render time old vs new, and the gate verdict. Commit the images under docs/reports/render-v2/.
Report to me and STOP. I review the sheet before R7.

### R7: production switch and queue re-render (after my OK)

Merge to develop, then deploy to staging. After the staging acceptance passes, merge to main (ask me first, per CLAUDE.md).
Set `factory.layout_v2.enabled = true` in production.

Re-render the queue:
- Every clip that is approved/queued and not yet posted on a given account gets new clip_renders with v2, for the styles in its style_plan.
- Mark the old renders `status = 'superseded'`, and add that status where statuses are checked.
- `variants.render_for_post` must never return a superseded render.
- Keep the queue order and posting slots. Run the re-render through the jobs queue at a priority below fresh spike clips, so the live fast lane is never delayed.
- If a clip's source copy is missing, don't post the old render: hold the clip, list these clips in the report, and I decide.

Delete the dead code:
- Layout classes A, B, C, H, P and M, with their geometry constants, filters and tests.
- The retired styles' entries in factory.styles (a migration that removes them) and their affinities.
- Any remaining badge/progress/seam/emoji code.

Update the docs: docs/09-clip-factory.md (layouts), docs/27-style-reference.md (now: G/G0 + text treatments; mark the old styles retired with the date), docs/16-settings-reference.md and docs/10-quality-gate.md.

Measure for 7 days in the weekly report (or a one-off query in the progress note):
- Median views at 48 h per platform, v2 vs the last 7 days before v2.
- YouTube viewed-vs-swiped-away, where we have it.
- Gate fallback rate (G → G0) and facecam_rejected reasons.

## 5. What a correct v2 frame looks like (check against this)

- 1080×1920.
- G: the face is on top, whole, centred, with headroom and sharp enough; the gameplay is below at 62.5% of the frame, sliding clear of the streamer's cam and chat; both are perfectly still for the whole clip.
- Text: one line on the seam at a time, plus the small mark top-right and the credit top-left of the gameplay. No text in the bottom 380 px or the right 160 px.
- G0: the gameplay large in the middle (70% of the frame) over a blurred copy of itself, the hook on top for 2.5 s, captions low.
- Never: zoom, jump cuts to a face, a panel without a face, a face cut by an edge, black bars in G, the LIVE pill, emoji stickers, a progress bar, two captions at once.

## 6. Reference data (production creator_layouts, 23 Nov 2026)

Use these as test fixtures. Source frames are 1920×1080 unless noted.

| Creator | Cam box (x, y, w, h) | Conf | Truth |
|---|---|---|---|
| Jerma985 | 0, 651, 248, 246 | 0.93 | Real cam, square: today loses 37% of its height |
| MissMikkaa | 1402, 472, 518, 608 | 0.60 | Real cam, tall: today the face is cut above the nose |
| KaiCenat | 0, 432, 393, 222 | 0.93 | Real cam |
| Elajjaz | 0, 440, 342, 314 | 0.90 | Real cam |
| xQc | 0, 0, 368, 291 | 0.93 | Real cam |
| Ludwig | 0, 384, 346, 244 | 0.93 | Real cam |
| Joe_Bartolozzi (1280×720) | 958, 93, 322, 184 | 0.95 | Wrong: gameplay wall, so it must be rejected |
| stableronaldo | 72, 735, 423, 345 | 0.60 | Wrong: chin and game UI, so it must be rejected or re-found |
| LIRIK, Vargskelethor | none | 0.80 / 0.95 | No cam, so G0 |

## 7. Reporting

After each milestone, send a short message with:
- what was built, with flag and setting names;
- test results;
- render time per clip, before and after;
- the P4 / face-check cost and latency per clip (R3);
- anything blocked, and what you need from me.

Full detail goes in development-plan/PROGRESS.md.

## Start now

Do section 0. Do R0 completely and report to me.

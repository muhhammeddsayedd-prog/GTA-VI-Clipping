# M18 — Multiple design styles, chosen per platform

**Goal:** every clip is rendered in several design styles: one style per platform account, and a short and a story version of the moment when it has a story. A style director picks each style automatically. Today one global look (`factory.style`, docs/27) goes to every platform.

**Status:** Phase A built 2026-11-19 (see PROGRESS.md); phases B-E open.

**Depends on:** M06 (clip factory), M07 (gate), M10 (posting), M17 (console, for the style views). Staging first. Nothing reaches `main` before the staging checks of each phase pass.

## Owner decisions (2026-11-19)

These rows move to `docs/00-decisions.md` as D112–D115 when the first phase is merged.

| # | Decision | Notes |
|---|---|---|
| D112 | **The facecam goes on top** in every style that shows one, at about 35% of the height, with gameplay below. This replaces "gameplay top, facecam bottom" (docs/27 §2, "owner's choice"). | 6 of the 7 references that show a facecam put it on top (§1). |
| D113 | **Every version is posted.** A moment gets a short cut, plus a story cut when the window holds a real arc. Both are posted. | Spacing rules are in Scope 4. |
| D114 | **One style per platform.** Each platform account gets its own render of each version, in a style picked for that account. | This also closes the D111 gap: each render carries that account's handle. |
| D115 | **`headline_card` uses our brand header:** the "6 Stars Clips" name and avatar, with no verified tick and no imitation of X/Twitter's interface. | |

## 1. Reference study (owner links, 2026-11-19)

Ten Instagram reels were downloaded through Apify (`apify/instagram-scraper`) and checked with contact sheets, ffmpeg scene detection (cuts at scene > 0.3) and `ebur128`.

| Ref | Plays | Length | Frame | Text | Cuts |
|---|---|---|---|---|---|
| R02 bluethunder `Df5Q8WUC_g-` | 161k | 95 s | Facecam top ≈35%, gameplay ≈65% | White pill title with emoji on the seam, whole clip | 2 |
| R07 bluethunder `Df4hsU0Cp7R` | 462k | 157 s | Same as R02 | Pill title plus small sub-pill "(No story spoilers)" | 8 |
| R03 akaformula `Df4xUX_xUim` | 386k | 11 s | Facecam top ≈35% | White title with thin outline and emoji at the top of the gameplay, whole clip | 0 |
| R04 akaformula `Df4jQBbREZj` | 1.3M | 14 s | Same as R03 | Same as R03 | 1 |
| R05 ghostofcaldera `Df4qH3jAiHv` | 657k | 25 s | Facecam top 35%, gameplay ≈53%, blurred filler strip at the bottom | Red condensed question title with black outline, inside the gameplay | 0 |
| R06 LeopardStealth `Df4183UALi6` | 7.1M | 121 s | Facecam top ≈36%, Twitch chat overlay kept | One word at a time, white with the current word in yellow, on the facecam's bottom edge; creator socials end card | 2 |
| R09 @rap `Df41UsLtO3j` | 6.9M | 33 s | Black card: publisher header, 2-line headline, landscape stream crop in the middle | Headline only | 0 |
| R08 kingziah `Df4pKefxA-Z` | 1.4M | 39 s | Full gameplay, portrait crop with black bars | Centred "POV …" with emoji | 1 |
| R10 fxyplay `Dcod99JqRXy` | 1.6M | 25 s | 3:4 letterbox, gameplay B-roll on top, face below, gradient seam | Two-tier kinetic text ("YOU CAN" / big glowing "BACKFLIP") | 3, plus montage |
| R01 svx `Df4-nHLB3jU` | 2k | 60 s | Raw 16:9 letterbox | None | 0 (anti-reference) |

**Takeaways:**
- The facecam goes on top.
- A title that stays for the whole clip is common (6 of 10).
- Calm, continuous takes perform as well as fast cutting.
- Length falls into three groups: micro (10–15 s), standard (25–40 s) and story (90–160 s).

## 2. Audit of the current system (what blocks multiple styles)

1. **One look for everything.** `factory.style` is a single global `StyleRules`. Layout A or B is chosen only by whether a facecam was found.
2. **Positions are fixed in code.** The geometry is module constants in `factory/layout.py` (`A_GAMEPLAY_H`, `CAPTION_Y_*`, `HOOK_*`, `WATERMARK_*`, `PROGRESS_*`).
3. **The gate's band depends on those constants.** `gate/visual.py` computes its frozen/black band from them (`BAND_TOP`/`BAND_BOTTOM`). With another layout the band can fall on our own static graphics and fail good clips.
4. **The visual check assumes our layout.** P5 says the webcam is "in the lower part". A brand header or card can be flagged as `platform_logos` or `other_watermark`.
5. **The style isn't recorded.** There is one file per clip (`clips.local_path`), and `posts` has no link to a render.
6. **Length is capped at 60 s** (`gate.hourly.max_len`, `spikes.max_len_s`).
7. **The docs disagree about the hook.** docs/09 §10 and the `factory.style` description in migration 0021 say the title bar lasts "the whole clip". Commit `52135a5` and docs/27 say it lasts `factory.hook_seconds`.

## 3. Style catalogue

A style is a named preset built from shared parts: layout template, title pack, caption pack, pacing, length profile and audio pack. Only curated presets exist, so the system can't produce random combinations.

| Style | From | Layout | Text | Pacing | Lengths | Hard requirements |
|---|---|---|---|---|---|---|
| `cam_top_viral` | docs/27, flipped per D112 | Facecam top 672 px, gameplay 1248 px | 2-word Anton captions on the seam; title bar for `hook_seconds` | Punch-ins, zoom cuts, sound effects, compression | Short | Facecam |
| `cam_top_title` | R02, R03, R04, R07 | Same | Persistent title (variant `pill` on the seam, or `outline` at the top of the gameplay) with emoji, optional sub-pill; no speech captions | Calm | Short, story | Facecam; source ≥ 1080p (the gameplay crop is narrow) |
| `cam_top_wordcap` | R06 | Same | One word at a time, white with the active word in the accent colour, on the facecam's bottom edge; optional creator credit end card | Calm | Story (also short) | Facecam; words/s ≥ setting |
| `cam_top_question` | R05 | Facecam top, gameplay, blurred bottom strip | Big condensed question title inside the gameplay | Calm | Short | Facecam; `moment_type = discovery` and a question title from P2 |
| `headline_card` | R09 | Black card: brand header (D115), 2-line headline, landscape stream crop as the streamer had it | Headline only | Calm | Short | None (any facecam, ≥ 720p) |
| `pov_fullframe` | R08 | Full gameplay, portrait crop, black bars | Centred POV line with emoji | Calm | Short | None; replaces layout B |
| `hype_montage` | R10 | 3:4 letterbox, gradient seam, B-roll on top | Two-tier kinetic words | Cut about every 3 s | Short | VOD lane only; other moments of the same video as B-roll |

Every style keeps the creator credit and our watermark. The watermark is now the posting account's handle (D114).

## 4. Scope

### Phase A: foundation and the flip (no new styles yet)

1. **Style templates.**
   - `factory/styles/`: a `StyleSpec` (layout template + `StyleRules` + text packs + length profiles + gate band rectangle + P5 description) and a registry.
   - The geometry moves out of module constants into the templates. Positions stay on the 1080×1920 reference canvas.
2. **`cam_top_viral`** is the current look with the facecam on top (D112). Layout B clips use `pov_fullframe` (Phase B) and keep the blurred layout until then.
3. **Per-render gate data.**
   - The frozen/black band comes from the chosen style's gameplay rectangle.
   - P5 receives a style description listing our own elements.
   - Content checks (score, music, dedupe, safety, P9V) run once per clip version. Technical checks and P5 run once per render.
4. **Data.** Migration `00xx_design_styles.sql`:
   - `clip_renders` (id, clip_id, version `short|story`, account_id, platform, style_id, style_version, trim_start_s, trim_end_s, local_path, storage_path, thumb_path, render_ms, gate_status, gate_reasons, created_at).
   - `posts.render_id`.
   - Settings `factory.styles` and `factory.style_director` (below).
   - `clips.local_path` stays as the reference render, for compatibility.
5. **Rendering per account.**
   - One `clip.render` job per (version, account). Transcript, layout detection, loudness envelope and audio cleanup are computed once per version and reused.
   - Spike clips render the live TikTok account first, so posting is not delayed; the other accounts follow.
   - Each render burns in its account's handle.
6. **Style director v1** (`factory/director.py`, pure logic):
   - Filter by hard requirements, lane, render budget and enabled styles.
   - Apply per-account weights.
   - Within one clip, prefer styles not already given to another account.
   - Don't repeat the last style on the same account.
   - Seeded random pick on (clip_id, version, account).
   - Writes `clip.style_chosen` with the candidates, the rejected ones and the reason.
7. **Docs.** docs/09 §3–5 and §10, docs/27 §2 (flip), docs/10 (band, per-render checks), docs/16 (new settings); fix the hook drift (audit 7).

### Phase B: the cheap static styles

8. **Templates** for `cam_top_title`, `headline_card`, `pov_fullframe` and `cam_top_question`. Header card, pills and outline titles are drawn with Pillow like `factory/titlebar.py` (colour emoji).
9. **P2 adds:**
   - `moment_type` (`reaction | discovery | action | story | npc_funny | fail`).
   - Text variants: `headline` (≤ 90 chars), `title_line` (≤ 40 chars with emoji), `question` (≤ 40 chars, or null), `pov_line` (≤ 40 chars, or null).
   - All in the same call; schema limits in `ai/schemas.py`; docs/15 updated.
10. **The director adds content fit.**
    - discovery → `headline_card` or `cam_top_question`
    - reaction → `cam_top_viral`
    - action with no speech → `pov_fullframe`
    - story → `cam_top_title` or `cam_top_wordcap`

    The affinity table is part of `factory.style_director`.

### Phase C: story versions and word captions

11. **P2 returns a `short` trim** (payoff-centred, `factory.length.short`), **and a `story` trim** (`factory.length.story`) only when the window holds an arc.
    - Wider windows on the VOD and Twitch-clip lanes are needed for story cuts (window maths settings).
    - Spikes post the short version at once. Their story version goes to the hourly lane.
12. **Gate profiles per version.** `gate.hourly.max_len` becomes per version, and the duplicate check exempts sibling versions of the same moment.
13. **Posting.**
    - A story version takes an hourly slot.
    - Two versions of the same moment on the same account are at least `posting.sibling_gap_h` apart.
    - A platform whose upload limit is below the story length skips that version. **Check each platform's current maximum length (TikTok, Instagram Reels, YouTube Shorts) in the Upload-Post docs before coding**, and record the limits in docs/14.
14. **Add the `cam_top_wordcap` template.**

### Phase D: learning from performance

15. **Score.** For each published render: views at 48 h divided by the rolling median of that account (`posts.views`, `stats_updated_at`); for YouTube, also `average_view_percentage`.
16. **Bandit.** Thompson sampling per (account, style) replaces the fixed weights when `factory.style_director.mode = bandit`. It keeps an exploration floor (default 10%) and a minimum sample size before a style can be starved. Console: a per-style performance table.

### Phase E: `hype_montage`

17. **B-roll and effects.** B-roll is picked from other high-scoring moments of the same video, cut about every 3 s; two-tier kinetic captions; gradient seam. Never official game logos.

## 5. Settings (new; every number is a setting)

| Key | Default | Meaning |
|---|---|---|
| `factory.styles` | `{"cam_top_viral": {"enabled": true, "weight": 1, "lanes": ["spike","hourly"], "accounts": "*", "params": {…}}, …}` | Per style: on/off, base weight, lanes, accounts (or `*`), parameter overrides (the old `factory.style` keys move into `params`). |
| `factory.style_director` | `{"mode": "weighted", "no_repeat_n": 1, "diverse_across_accounts": true, "explore_floor": 0.1, "min_samples": 20, "affinity": {…}, "min_words_per_s_wordcap": 1.2}` | How the director picks. |
| `factory.length` | `{"short": {"min": 10, "max": 40}, "story": {"min": 60, "max": 150}}` | Length profiles. Story max is capped per platform by the verified limits. |
| `factory.render_budget` | `{"max_renders_per_hour": …}` | Measured on the server in Phase A, then set. |
| `posting.sibling_gap_h` | `12` | Minimum gap between versions of the same moment on one account. |

## 6. Risks

- **Render cost.** Up to 4 accounts × 2 versions = 8 renders per moment, against 1 today; story renders run up to 150 s.
  - Measure on the Hetzner server in Phase A before raising volume.
  - Per-version shared preprocessing.
  - `factory.render_budget` lowers the version count before it delays the spike lane.
- **Repetition on one account.** Posting two versions of the same moment can look duplicated to a platform. Mitigated by `posting.sibling_gap_h` and by giving each version a different style.
- **Learning confounders.** The creator, the moment score and the time of day all affect views alongside the style. Normalise by the account baseline; compare styles only within an account.
- **Rights.** No official GTA VI logos and no imitation of another platform's interface. Chat overlays come from the creator's own stream and stay inside the facecam crop.

## 7. Tests (pure, no network)

- **Director:** eligibility, weights, no-repeat, cross-account diversity, deterministic seed, affinity, budget fallback.
- **Geometry:** every template's regions are even-sized, inside the canvas and respect the safe zones.
- **Gate band:** derived per style; never overlaps that style's static graphics.
- **P2 schema:** limits on the new text fields.
- **Story trim:** sits inside the window and contains the short trim's payoff.
- **Posting:** sibling gap and the per-platform length skip.

## 8. Definition of done (per phase)

- `ruff`, `mypy --strict` and `pytest` pass. Migrations apply on a fresh database and on staging.
- **Staging (A):** every account gets its own render with its own handle. The facecam is on top. The gate passes good clips at the same rate as before the flip, checked against the last 20 staging clips.
- **Staging (B, C):** each enabled style is rendered at least 3 times and checked by eye from the console thumbnails. Story versions post to private test accounts and respect the sibling gap.
- **(D):** the bandit keeps every enabled style at or above the floor in a 7-day staging simulation on recorded metrics.
- `PROGRESS.md` updated after each phase.

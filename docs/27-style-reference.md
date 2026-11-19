# 27 — Style reference: what viral GTA clips do, and how the factory copies it

The owner supplied three short clips that each reached millions of views (2026-11-18). This document records what they do frame by frame and what the factory does about it. The setting is `factory.style` (docs/16); the code is `factory/style.py`, `factory/effects.py`, `lines.chunk_line`, `subtitles.chunk_events` and the punch-in branch in `layout.py`.

## 1. The references

| | V1 "GTA 5 & 6 marathon" (streamer talking) | V2 "Kai Cenat meets Ice Spice in GTA RP" | V3 "Trolling players as a fake NPC" |
|---|---|---|---|
| Length | 18.4 s | 18.2 s | 20.0 s |
| Frame | facecam top half, gameplay/B-roll bottom | facecam top ~37%, gameplay bottom | full-screen gameplay, no facecam |
| Captions | **one word** at a time, ALL CAPS, heavy condensed font, hot magenta, thick black outline + hard drop shadow, on the seam | 1–2 words, ALL CAPS, white; magenta for the second speaker; repeats as `WAIT X2`, swears masked `SH*T` | none (no speech) |
| Hook | none on screen: the first spoken words are the hook ("GTA SIX…") | none on screen: starts mid-shout ("WAIT", "HOLD ON") | TikTok-style white bar with black text and emoji, **kept for the whole clip** |
| Pacing | B-roll or graphic change every 1–3 s (trailer shots, logo cards, "40 HOURS" text) | **hard cut to a full-screen, zoomed facecam** for 1–1.5 s on each scream (2.3 s, 7.0 s, 13.5 s, 15–18 s) | jump cuts, dead time removed |
| End | no outro | ends mid-energy (loops) | payoff: explosion + 💀 emoji + sting, no outro |
| Sound | chime on graphic reveals (2.4 s, 7.5 s), broadband impact on a hard cut (12.8 s) | raw stream audio; the punch-ins land on the screams | quiet game audio, harmonic meme sting on the payoff (18.2 s) |
| Loudness | very dense: momentary ≈ −10 LUFS throughout | dense with scream peaks | quiet until the payoff |

Common rules: interesting from frame one, a visual change every 1–3 s, big text with few words, sound on every cut and on the payoff, end on the payoff. Short-form research says the same (vendor guides, unverified): scroll decision in ~0.8 s, 7–20 s clips perform best, cut or zoom every 2–3 s, no intros or fades.

## 2. What the factory does (`factory.style`, migration 0021)

| Element | Rule | Setting |
|---|---|---|
| Captions | Each subtitle line is split into chunks of at most 2 words / 14 characters (also at punctuation, pauses ≥ 0.25 s and repeated words). Anton, size 150, capitals, white, outline 7% of size, hard shadow. Every chunk pops in 80%→110%→100% in 120 ms; a keyword chunk pops to 125% and its keyword words are in the accent colour. A chunk said again straight away shows once as `X2`/`X3`. Profanity masked as before. | `caption_*` |
| Accent | Magenta `#FF2BD6`. | `factory.accent_color` |
| Hook | TikTok-style title bar for the first `factory.hook_seconds` (3 s), then gone so the content has the frame; look copied from V3: TikTok Sans at weight 420, size 64 (down to 44 to fit), black on one tight white rectangle (padding 14/8 px, radius 6, no shadow), wrapped fill-first in at most 2 lines inside 700 px, colour emoji inline, top of the safe zone (below the LIVE badge on spike clips). Drawn with Pillow (`factory/titlebar.py`) because libass has no colour emoji, then overlaid by ffmpeg; without the title font the ASS hook box is used. The watermark sits just below it from t=0. | `hook_mode` |
| Punch-ins | From the momentary loudness of the window (`ebur128`, 100 ms), up to 4 spans of 1.2 s where the audio is ≥ 5 dB over the clip median, 2.5 s apart, not in the first 0.5 s or last 0.3 s, ≥ 1 s from the payoff. During a span the frame hard-cuts to the facecam (layout A) or the source centre (layout B), filling 1080x1920 at 125%; seam and progress bar are hidden. | `punchin_*` |
| Zoom cuts | The gameplay alternates 100% / 108% every 2.5 s (replaces the slow tail zoom). | `zoomcut_*` |
| Payoff | The clip writer (P2) names `payoff_s` and `payoff_emoji`. At that moment a 230 px emoji pops in centred on the gameplay for 1.5 s and a bass hit plays. | `payoff_fx` |
| Sound effects | Whoosh 0.3 s before each punch-in, impact on its first frame, boom on the payoff, keyword pops except within 0.4 s of another effect; all at −10 dB. Files are generated (CC0, `assets/sfx/LICENSES.md`). | `cut_sfx`, `sfx_gain_db` |
| Loudness | Compressor (−20 dB threshold, 4:1, 5/80 ms, +2 dB) before the two-pass loudnorm; target −12 LUFS, true peak −1.5 dB. | `compress`, `factory.loudness_lufs` |
| End card | Off: the clip ends on the payoff and loops. | `factory.end_card_s` = 0 |
| Layout | Unchanged: gameplay on top, facecam below, captions on the seam (owner's choice). Superseded by D112: see section 5. | — |

Without `factory.style` (or with an invalid value) the factory renders the classic look of docs/09, so a database without migration 0021 behaves exactly as before.

## 3. Verified on a real render (2026-11-18)

30 s window of a GTA RP stream with a facecam, `clip render-file` offline: layout A, one punch-in at 12.15–13.35 s on the loudest reaction (+5.8 dB over the median), whoosh 11.85 s, impact 12.15 s, payoff 💀 and boom at 26.15 s, four keyword pops, no end card. Output −12.0 LUFS integrated, true peak −1.3 dBFS, LRA 3.2 LU; render 22 s on the dev machine.

## 4. Not done (ideas for later)

- V1's generated explainer graphics (logo cards, "40 HOURS" counters) and V2's face stickers of the other person: they need image sourcing and carry rights risk.
- Speaker-coloured captions (V2's magenta for the second speaker): needs diarisation.
- Meme stings (V3): only royalty-free sounds may be used; the current boom is generated.

## 5. Design styles (development-plan/M18, decisions D112-D115)

The owner's second set of references (ten Instagram reels, development-plan/M18 section 1) showed that the facecam goes on top and that calm, title-only clips perform as well as the fast edit above. Since migration 0027 the factory has a catalogue of styles (`factory.styles`) instead of one look, and picks one per platform account (`factory/director.py`, `factory.style_director`).

| Style | Layout | Text | Pacing |
|---|---|---|---|
| `cam_top_viral` | C: facecam 1080x672 on top, gameplay 1080x1248 below, seam and progress bar between them | the section 2 captions in the lower gameplay (y 1400), title bar on the seam for `factory.hook_seconds` | punch-ins, zoom cuts, sound effects, compressor |
| `cam_top_title` | C | no spoken captions; the writer's `title_line` (else the hook) plus the payoff emoji as a title bar on the seam for the whole clip (references R02-R04, R07) | calm: no punch-ins, no zoom cuts, no sound effects |
| `cam_top_question` | C | the writer's `question` in big red capitals (Anton) with a black outline at the top of the gameplay, whole clip (reference R05) | calm |
| `headline_card` | H: black frame, brand header (star avatar, `factory.watermark` name) and the writer's `headline` from y 300, the stream as a 4:3 crop 1080x810 at y 630 that keeps the streamer's facecam, credit under it; no watermark (reference R09) | headline only | calm |
| `cam_top_wordcap` | C | one or two heavy words (Montserrat Black capitals) centred on y 580, the bottom edge of the facecam, the spoken word in yellow `#FFD400`; no title (reference R06) | calm; favoured for story cuts |
| `pov_fullframe` | P: the gameplay alone as a 3:4 crop 1080x1440 at y 240 between black bars, away from the facecam | the writer's `pov_line` centred on y 900, white with a black outline (reference R08) | calm |

Without a facecam both styles use layout B. The director gives each account a style the catalogue allows for the lane, not the one of the account's last clip, and different from the other accounts of the same clip where possible; the pick is seeded with the clip id. The first render is made in the style of the reference account; each other style of the plan is rendered by the job `clip.variants` once the gate has approved the clip (`factory/variants.py`), checked like the gate checks a file, and uploaded to Storage as `<clip key>.<style>.mp4`. The console's clip page lists the renders and plays each one.

A Phase B style needs the writer's text it shows (`requires`), so a clip without a question never gets `cam_top_question`. The clip writer also names the kind of moment (`moment_type`), and `factory.style_director.affinity` multiplies each style's weight for it (a discovery suits the news card and the question, a reaction the viral edit, an action the POV crop). Phase C adds `cam_top_wordcap` and story cuts: a 60-150 s cut of the same moment, posted as its own clip (docs/09 section 12).

## 6. Learning which style works (development-plan/M18 Phase D)

With `factory.style_director.mode` = `bandit` (migration 0030) each account learns from its own posts. A post is scored once it is `min_age_h` (48) hours old, for `window_d` (14) days: `log(1 + views)` minus the median of the account's scored posts, so 0 is an ordinary post for that account and +0.7 about twice its usual views (`factory/performance.py`). The views come from the stats collector (`posts.views`, refreshed every two hours for a week).

Per account and clip (`director.bandit_pick`):
1. with probability `explore_floor` (0.1) the weighted pick of section 5;
2. else, while an allowed style has fewer than `min_samples` (20) scored posts on the account, the weighted pick among those;
3. else Thompson sampling: each style's mean score is drawn from its posterior (the sample mean shrunk towards 0 by `prior_n` pseudo-posts, spread the account's pooled standard deviation), plus `affinity_weight` (0.3) times the log of its moment-type affinity, and the highest wins, without the no-repeat and diversity steps.

Until 20 posts of a style are scored, nothing changes. A seven-day simulation (three accounts, three styles worth +0.7, 0 and -0.5 log views) moves to the best style on day 3 and keeps it at 80-90 % from day 4, every style keeping a share (`tests/unit/factory/test_style_bandit.py`). In production that is about five days per account (the posts must age two days first). Staging posts are dry-run or private and have no views, so staging stays in the learning step. The console's Insights → Design styles page shows, per account and style, the posts of the last week, the scored posts, their average views and the views against the account's usual.

## 7. The hype montage (development-plan/M18 Phase E, reference R10)

`hype_montage` (migration 0031): layout M (docs/09 section 3), two-tier captions on the seam (a chunk with a keyword shows the keyword big, italic, white over an accent-coloured glow, the rest of the chunk small above it), a whoosh and an impact on each B-roll cut, the payoff emoji and bass hit, no title.

The B-roll comes from the footage around the moment that the source file already holds (the padded window, widened for story cuts), never from the clip itself (`factory/montage.py`): the picture change is measured twice a second over the padded window; the busiest stretches outside the clip that are not stills (menus, loading and black screens) are laid over the gameplay panel every other `montage_every_s` (3) seconds from the second slot on, never within 1.5 s of the payoff or in the last second, at most six. A clip with less than 9 s of footage around it does not get the style (`requires: ["facecam", "broll"]`); it is hourly only. When the montage is one account's variant, the source copy for `clip.variants` holds the whole padded window. Official game logos are never used.

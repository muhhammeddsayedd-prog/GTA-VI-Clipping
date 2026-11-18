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
| Layout | Unchanged: gameplay on top, facecam below, captions on the seam (owner's choice). | — |

Without `factory.style` (or with an invalid value) the factory renders the classic look of docs/09, so a database without migration 0021 behaves exactly as before.

## 3. Verified on a real render (2026-11-18)

30 s window of a GTA RP stream with a facecam, `clip render-file` offline: layout A, one punch-in at 12.15–13.35 s on the loudest reaction (+5.8 dB over the median), whoosh 11.85 s, impact 12.15 s, payoff 💀 and boom at 26.15 s, four keyword pops, no end card. Output −12.0 LUFS integrated, true peak −1.3 dBFS, LRA 3.2 LU; render 22 s on the dev machine.

## 4. Not done (ideas for later)

- V1's generated explainer graphics (logo cards, "40 HOURS" counters) and V2's face stickers of the other person: they need image sourcing and carry rights risk.
- Speaker-coloured captions (V2's magenta for the second speaker): needs diarisation.
- Meme stings (V3): only royalty-free sounds may be used; the current boom is generated.

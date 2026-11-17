# 15 — Claude Prompt Library

Prompts live in `src/sixstars/ai/prompts/<id>.md`; schemas in `src/sixstars/ai/schemas.py` (Pydantic). Every call: validate JSON → on failure retry once with the error → on second failure the step fails (job retry rules apply). Log cost to `api_usage` with `operation = <prompt id>`.

Common system preamble (all prompts):
> You work for 6 Stars Clips, an English-language short-form channel that posts the best GTA 6 moments from Twitch and YouTube streamers to a global audience. You answer only with JSON that matches the schema. Never invent facts that are not in the input.

---

## P1 — spike_confirm (model: `ai.model_fast`)

**Input:** creator handle, stream title, platform, spike stats (score_z, msgs, uniq, top tokens), 40 chat messages around the peak with relative times, optional transcript of the window.

**Task:** Decide whether chat is reacting to a real, clip-worthy on-stream moment (funny, shocking, skilful, chaotic, emotional, a big reveal) versus noise (raid, giveaway, sub train, chat game, streamer reading donations, technical problem, chat arguing, spam, a "first!" wave, the stream starting/ending).

```json
{
  "is_moment": true,
  "confidence": 0.0,
  "moment_type": "funny|fail|skill|chaos|shock|reveal|emotional|other",
  "one_line": "what happened, max 15 words, no spoilers",
  "noise_reason": null
}
```
Batch form (replay mode): input is a list of up to 10 spikes; output `{"results":[{"spike_id":"…", …}]}`.

---

## P2 — clip_writer (model: `ai.model_writer`)

**Input:** creator handle + platform, stream title, moment_type and one_line from P1 (if any), chat sample, English word-level transcript of the window plus context pad (times relative to the window file start), proposed in/out points, min/max length, the spoiler policy (with current date and `gate.spoiler_block_until`), rejected topics list, profanity list hint.

**Task:** Produce everything needed to render and post the clip.

Rules given in the prompt:
- Trim: you may move `start_s` earlier (setup began sooner) or `end_s` later (payoff came after) within the window file; never start later than `spike_floor_s`; length between min and max.
- Hook: max 8 words, hype tone, curiosity without clickbait lies, no spoilers, no slurs, English. Examples: "He did NOT expect that 😭", "Worst getaway driver in Leonida?".
- Title (YouTube): max 70 characters, no spoilers, no hashtags.
- Caption: one punchy line, max 120 characters.
- Hashtags: 3–5 moment-specific (lowercase words, no spaces), not including the base tags.
- Commentary: optional single on-screen line (max 12 words) adding context; null if not needed.
- Topic: a short slug describing the moment (e.g. `police-chase-boat-crash`) used to avoid duplicates.
- Keywords: for each subtitle line index, 0–2 words to highlight (money, names, "no way", key verbs).
- Spoiler: true if the clip shows or discusses GTA 6 story events, endings, character deaths or mission reveals.
- Safety: flag real-world politics, gambling, sponsored segments, real-world violence, sexual content, personal information (addresses, phone numbers, faces of non-consenting bystanders on IRL streams), hate speech, possible copyrighted media playing (TV, film, music).
- Score 1–10 with sub-scores: hook strength, clarity without context, payoff, rewatchability.

```json
{
  "start_s": 0.0,
  "end_s": 0.0,
  "hook": "",
  "title": "",
  "caption": "",
  "hashtags": ["", ""],
  "commentary": null,
  "topic": "",
  "keywords": [{"line": 0, "words": [""]}],
  "spoiler": false,
  "safety": {"politics": false, "gambling": false, "sponsored": false, "real_violence": false,
             "sexual": false, "personal_info": false, "hate": false, "copyrighted_media": false},
  "score": 0,
  "score_parts": {"hook": 0, "clarity": 0, "payoff": 0, "rewatch": 0},
  "reject": false,
  "reject_reason": null
}
```
Note: subtitle line grouping is done in code before the call; the prompt receives numbered lines.

---

## P3 — vod_moment_picker (model: `ai.model_writer`) — fallback for videos with no chat

**Input:** video title, creator, full transcript in ~30-second chunks with timestamps (long videos: process in 30-minute slices and merge), min/max moments, length limits.

**Task:** Pick the 3–8 strongest standalone moments (same definition as P1).

```json
{"moments": [{"start_s": 0.0, "end_s": 0.0, "moment_type": "funny", "one_line": "", "confidence": 0.0}]}
```

---

## P4 — layout_detect (model: `ai.model_writer`, vision)

**Input:** one 1280-wide JPEG frame from the stream, its original width/height.

**Task:** Find the streamer's facecam overlay (a rectangle showing a real person/webcam) and the main gameplay area. Ignore chat boxes, alerts, logos.

```json
{"facecam": {"x": 0, "y": 0, "w": 0, "h": 0} , "gameplay": {"x": 0, "y": 0, "w": 0, "h": 0}, "confidence": 0.0}
```
`facecam` is null if there is no webcam. Coordinates in original pixels.

---

## P5 — visual_check (model: `ai.model_fast`, vision)

**Input:** 4 frames from the rendered clip (10%, 35%, 60%, 85%).

```json
{
  "other_watermark": false,
  "platform_logos": false,
  "menu_or_loading": false,
  "pause_or_brb_screen": false,
  "empty_facecam": false,
  "readable_subtitles": true,
  "notes": ""
}
```

---

## P6 — permission_read (model: `ai.model_fast`)

**Input:** channel description, channel name, recent stream titles.

```json
{"tier": "preferred|auto|restricted", "evidence": "quote max 20 words or null"}
```
`preferred`: clearly allows or invites clipping (mentions clip programs, "clip my streams", "clippers welcome", paid clip campaigns). `restricted`: forbids reuploads, clips, reposting or compilations. Otherwise `auto`.

---

## P7 — takedown_classify (model: `ai.model_fast`)

**Input:** email from, subject, body text (first 4,000 chars).

```json
{
  "is_takedown": false,
  "kind": "copyright|privacy|trademark|other|not_takedown",
  "creator_handles": [],
  "urls": [],
  "platforms": [],
  "summary": ""
}
```

---

## P8 — translate_lines (model: `ai.model_writer`)

**Input:** source-language transcript segments with start/end times.

```json
{"segments": [{"start": 0.0, "end": 0.0, "en": ""}]}
```
Keep slang energy; translate profanity faithfully (it will be bleeped); keep names unchanged.

---

## P9V — visual_score (model: `ai.visual_score_model`: `"writer"` or `"fast"`, vision)

Added by `visual-scoring` (migration 0016). File: `src/sixstars/ai/prompts/p9_visual_score.md`; call: `ai.calls.score_clip_visual`; usage operation: `p9_visual_score`. It runs in the gate (`gate/rescore.py`), after the render and before the decision, when `ai.visual_score` is true, and its score replaces the clip writer's in `clips.score`.

**Why:** P2 scores from the transcript and chat sample before anything is rendered. In game clips the payoff is on screen, so P2 scored every staging clip 4 to 6, including the ones worth posting.

**Input:** up to 8 JPEG frames of the rendered clip (at most 768 px on the long side) in time order, then text: clip length, hook, commentary line, the time of each frame, the subtitle lines in clip time, one line of chat numbers (messages and chatters in the peak window, share counted as laughter or shock, spike tier, where the peak falls in the clip) and the chat messages from 15 s before to 25 s after the peak (at most 30).

**System prompt (summary; the file is the source):**
- the shared preamble and the shared rubric of P2 and P9 (`_score_rubric.md`), unchanged: four parts, five bands, the rules tying `score` to the parts;
- "judge the payoff from the frames and the chat reaction as well as the words" in place of P2's "when a clip sits between two numbers, give the lower one" and "do not assume a visual payoff that nothing in them points to";
- what is ours and never scored: hook box, subtitles, watermark, creator credit, progress bar, LIVE badge, series counter, commentary line, blurred bars, webcam panel;
- how to read stills (a change between two frames is the event; nothing is invented when the picture never changes) and chat (evidence of what the audience saw, not a score);
- `score` follows from the parts: `payoff` and `clarity` both 7 or more means 7 at least; either at 6 or less means 6 or less;
- menus, inventory screens, stream overlays and watched cutscenes have a `payoff` of 5 or less; people standing and talking with no event and no punchline score 5 or less.

```json
{
  "seen": "one or two sentences: what happens, and where the payoff is",
  "score_parts": {"hook": 1, "clarity": 1, "payoff": 1, "rewatch": 1},
  "score": 1
}
```

Code-side backstop (`calls.bounded_score`): the stored score is never more than one above `payoff`, never more than two above `clarity`, and is in the 7-8 band only with both at 7 or more (9-10: both at 8 or more). Code only ever lowers the model's number. On the hourly lane the clip is asked `ai.visual_score_samples` times and the median is stored; express clips are asked once.

**Failure:** the clip keeps the writer's score (event `clip.visual_score_failed`); a used-up daily budget on the VOD lane defers the gate instead, as for P5.

---

## Cost control
- P1 batches in replay mode.
- P2 uses prompt caching for its long rules block.
- P4 is cached per creator for 7 days.
- Daily budget `ai.daily_budget_usd`; when exceeded, VOD-lane calls stop until midnight UTC; express calls continue.
- P9V (visual score): one request per express clip, `ai.visual_score_samples` per hourly clip; about $0.011 per request with `claude-sonnet-5-5` and 8 frames (measured, see `development-plan/progress-notes/visual-scoring.md`). Not asked for a clip that would be rejected even with a perfect score.

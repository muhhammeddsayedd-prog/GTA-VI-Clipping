## Hardening: AI layer and clip factory

**Date:** 2026-11-17 UTC · **Branch:** `hardening-ai-factory` (from `develop` at `1db9836`) · not pushed, not merged

Four defects found when M05 to M08 ran on real footage. Everything measured below was run on this desktop against the real models (`claude-sonnet-5-5`, `claude-haiku-5-5`), with settings patched in the process and usage rows written to the test database. Inputs, outputs and the request ledger are outside git in `C:\Users\iway\6stars-hardening-out\` (`runs\*.json`, `runs\ledger.jsonl`, `frames\`, `transcripts\`, `renders\`, `music\`).

### 1. Malformed JSON from the models

**Cause.** `complete_json` wrote the Pydantic JSON Schema into the system prompt and parsed free text. On frames with a problem the fast model copied the outline of the schema around its answer (`{"description": ..., "properties": {...}}`); the writer model sometimes sent a second JSON value after the first ("Extra data").

**Fix (`clients/anthropic_client.py`).** The schema now travels as the structured output format: `output_config = {"effort": ..., "format": {"type": "json_schema", "schema": ...}}`. Checked in the Anthropic docs on 2026-11-17 (`build-with-claude/structured-outputs`): both models support it; forced tool use (`tool_choice` `any` / `tool`) returns a 400 on `claude-sonnet-5-5`, so the output format is the mechanism, not a forced tool call. The API's schema dialect has no numeric, length or array-size limits and wants `additionalProperties: false` on every object, so the request carries `anthropic.transform_schema(model)` (SDK 2.9) and the reply is still validated with the full Pydantic model. The per-call subclasses of `ai/schemas.py` give the same schema as their base, so the API compiles one grammar per prompt (cached 24 h on their side). The system prompt is no longer changed by the client, so the cached prefix is the prompt file alone.

Kept: the public signature of `complete_json`, token and cost accounting, `cache_control` on the system block, images before the text, `AnthropicRefusal`, the budget guard (`calls._guard`), the doubling of `max_tokens` for a cut-off reply (once).

Also:
- Tolerant parser kept for safety: `first_json` takes the first complete JSON value (a second value or closing prose is ignored); `_parse` unwraps a `properties` envelope when the wrapper itself is not valid for the schema.
- A reply that fails the Pydantic validators (hook word count, trim rules) is sent back with the reason up to `REPAIR_ATTEMPTS = 2` times (was once). The word-limit error now lists the words as counted (`hook has 9 words (Cuffed | in | a | ...)`): the model was rewording at the same length because it counted differently.
- `invalid_model_output` logs `reason` (300 characters), `attempt`, `schema` and `reply_chars`; `api_call_failed` logs the API's message. The error names the schema title (`ClipWriting`), not `Bound`.
- If the API answers 400 to a request with the format, the client logs `structured_output_rejected` and falls back, for the rest of that call, to the old way (schema in the system prompt, free-text parsing). This covers a model set in `ai.model_*` that does not support the feature.
- `calls.pick_moments`: a part whose reply is unusable or declined is logged (`moment_slice_failed`) and skipped; the error is raised only when every part failed. API failures (HTTP 5xx) still propagate.

**Measured (invalid-output rate).**

| Run | Asks | First reply unusable | Unusable after the retries |
|---|---|---|---|
| P5 on the two bad frame sets, before | 10 | 6 | 2 |
| P5 on the two bad frame sets, after | 30 | 0 | 0 |
| P2 on five real transcripts, before | 25 | 10 | 4 |
| P2 on five real transcripts, after (final code and prompt) | 25 | 1 | 0 |

The bad frame sets are made from the real render `C:\Users\iway\6stars-m06-out\clips\ginge_normal.mp4`: one with a large `@otherchannel` and a "TikTok" label burned in, one zoomed so the subtitles run off the left edge (`frames\bad_watermark.mp4`, `frames\bad_cutsubs.mp4`, four frames each at 10, 35, 60 and 85%). Before, the four P2 failures were: hook over 8 words twice, "Extra data" once, start after the spike floor once. After, the one first reply that failed was a 9-word hook, repaired on the first retry. An earlier after-run (25 asks, before the word list was added to the error) had one clip fail on the hook after both retries; that is what the word list fixed.

P5 verdicts on the bad sets after the fix: watermark set flagged `other_watermark` and `platform_logos` 15 of 15; cut-subtitle set flagged `unreadable_subtitles` 14 of 15 (one run judged them readable: a judgement miss, not a shape problem); the clean frames 2 of 2 clean.

Every reply schema was also sent once to the API as an output format with a trivial request (`schemacheck.py`, fast model): all eleven were accepted (P1 single and batch, P2, P3, P4, P5, P6, P7, P8, P9, P10). P3, P4, P6, P7 and P8 were not otherwise run live in this work; P1, P2, P9 and P10 also passed their tests in `tests/live/test_ai_prompts.py`.

Not changed: `gate/visual.py` still asks P5 up to `P5_ATTEMPTS` times around the client; with the client's two repairs that is now more requests in the worst case than before. It can go back to one attempt once this has run in staging.

### 2. Clips that start mid-sentence

**Cause, as found.** Two things, not one. (a) Whisper `small` wrote no punctuation for some windows, so the writer could not see sentences. (b) The writer is told to cut on subtitle line boundaries, and lines are 2 to 4 words grouped by length: in an unpunctuated transcript a sentence usually begins in the middle of a line, so no line boundary is a sentence start.

**Fix.**
- `factory/pauses.py` (pure): `find_breaks` (a gap between words of at least `pause_s`, or a sentence-ending mark, is a phrase boundary), `snap_start` (back to the nearest boundary within `start_back_s` that keeps the clip within the maximum; else forward within `start_forward_s`, never past the spike floor, never under the minimum), `snap_end` (forward within `end_forward_s`, inside the file and the maximum; else back within `end_back_s`, never under the minimum), `snap_to_pauses`. A sentence end is preferred to a nearer bare pause. `writer.final_window` runs it between the trim rules and the existing move off the words.
- Real Whisper output has almost no gaps between words even where people pause: the pause is inside a stretched word (measured: `doj_call` had 1 gap of 0.4 s or more in 90 s, with words lasting up to 2.8 s). `transcript.speech_silences` runs the Silero voice activity detector that ships with faster-whisper (0.3 to 0.5 s for a 90 s window, no new dependency) and `pauses.tighten_words` pulls stretched words back onto the speech; the same window then has 7 pause boundaries. Subtitle lines also break at these pauses now.
- Pauses were not enough. The first re-render of the `ketchup` window (pauses only) still opened on "or attempted murder by the way": four people talk over each other for 90 s, the detector finds no silence at all, and the nearest gap was 4.06 s before the cut. So for a transcript in which fewer than `punctuate_below` of the words end a sentence, the fast model is asked where the sentences end (new prompt P10 `p10_sentence_ends`: numbered words in, word numbers out, the words are never rewritten) and `pauses.mark_sentence_ends` writes a full stop after those words, as Whisper does when it punctuates. Lines, the writer and the snap then all see sentences. Cost measured: $0.0002 and $0.0009 for the two windows. If the call cannot run (budget, no key, bad reply) the words are used as they are.
- P2 trim rules: open on the line where a sentence begins, read for meaning when there is no punctuation, prefer a line after a pause.

All numbers come from the setting `factory.cut_snap`; a database without it (before migration 0011) runs with the rule off. `sixstars clip render-file` without `--ai` never calls P10.

Tests: `tests/unit/factory/test_factory_pauses.py` (41 tests): no gaps, gap exactly at the threshold (0.39 no, 0.40 yes), back range, forward range, spike floor, minimum and maximum length for both ends, sentence end preferred to a nearer pause at both ends, 400 random transcripts for the invariants, `tighten_words` table, P10 wiring. `tests/integration/test_factory_pipeline.py`: the acceptance render's writer start fell on "dollars." (the end of "No way, fifty thousand dollars."); the expected start is now the start of that sentence.

**Re-rendered (Whisper `small`, real P2, P10, layout B), both windows from the upload `lmXea-Fxdfg` that M08 listed as starting mid-sentence.** Sources: `C:\Users\iway\6stars-m08-out\data\vod\06a461a8-...\sections\`.

| Window | M08 clip opened on | Now opens on | Now closes on | Cut (window time) |
|---|---|---|---|---|
| `cc2bf9b6` (call to the Department of Justice) | "the map to go" | "why am I doing / this to myself this / is torture." | "who you who you're. / gonna put the rest / on huh." | writer 10.40 to 64.20, final 10.40 to 64.50 (already on sentence boundaries) |
| `18825db2` (ketchup) | "the crime to get in a car accident" | "oh i'm on her / it can either be / a salt vandalism or / attempted murder." | "listen i think y / 'all are just trying / to get this man / arrested for nothing." | writer 14.24 to 57.80 (opened on "a salt vandalism or", closed on "listen i think y"), snapped to 12.62 to 59.80 |

Stills looked at: `C:\Users\iway\6stars-hardening-out\renders\doj_call_0.3s.jpg`, `doj_call_1.0s.jpg`, `ketchup_0.3s.jpg`, `ketchup_1.0s.jpg` (sheet: `_first_second_sheet.jpg`); the first subtitle on screen is "why am I doing" and "oh i'm on her" / "it can either be". Clips: `renders\doj_call.mp4`, `renders\ketchup.mp4`; summaries next to them as `.json`. The pauses-only renders that showed the gap are in `renders\pause_only\`.

Limits: P10's sentence ends are good but not perfect (it wrote "I need. a vehicle" once), and the words are Whisper's ("a salt" for "assault"). A clip can still open on a false boundary; it can no longer open on none.

### 3. Music false positives

Migration 0011 sets `gate.music_min_run_frames` from 1 to 2 (only where it is still 1). Reasoning, recorded in the migration and in docs/16: the zero-music limits (`gate.express.max_music_ratio`, `gate.strict_max_music_ratio`) protect against copyrighted music long enough to be matched by a platform; one second is below that; the detector flags one isolated second of speech over a game sound in a large share of renders; the cost is that one real second of music could pass. No ratio limit was raised. No gate code changed.

**Measured** with the real PANNs detector on the 16 real renders (`music\musiccheck.py`, results in `music\music_results.json`; the clip without its end card):

| | Renders | Ratio counting every frame | Ratio with run length 2 |
|---|---|---|---|
| No frame flagged | 12 | 0 | 0 |
| One isolated second flagged (`ginge_express`, `08e8af2d`, `8251cacc`) | 3 | 0.036, 0.029, 0.022 | 0 |
| A run of three seconds plus two single ones (`665f7fe5`, the truck hit) | 1 | 0.139 | 0.083 |

So 3 of 16 real clips that the express and strict limits rejected for one second now pass, and the one clip with a real run is still over the zero limits. The two re-renders above: ratio 0.

**Music bed.** A 10-second section of a CC0 netlabel track (Maelstrom, "I Dreamt for a Brighter Sky", archive.org item `gt536Maelstrom-ThePassage`; kept in `music\dl\`, outside git) mixed under the speech of the `ginge` source window (31 s): 10 of 31 seconds flagged in one run at mix volume 0.4 and 1.0 (ratio 0.32 with run length 2), 6 of 31 at volume 0.15 (0.19; runs of 4 and 2). The same window without the bed: 0. All far above the limits of 0 (express, strict) and 0.1 (hourly).

`tests/integration/test_gate_run.py`: the seeded value is asserted as 2 and the isolated-frame test sets 1 explicitly for its first half.

### 4. Unstable scores

**Sampling controls.** Checked against the current API notes: both models reject `temperature`, `top_p` and `top_k` (anything but the default is a 400, and every value is on API version `2026-10-21`); thinking cannot be switched off on `claude-sonnet-5-5`. `output_config.effort` is the only control, and it is not a sampling control. So variance cannot be turned down with a parameter.

**What the variance is.** Five fixed real transcripts (the M05 fixture window and four windows of `lmXea-Fxdfg` transcribed once with `small`), P2 five times each:

| Transcript | Before (old score text) | After (rubric), final run |
|---|---|---|
| fixture robbery | 6 6 6 6 6 | 5 5 5 5 5 |
| ketchup | 6 6 6 6 6 | 5 5 5 5 5 |
| DoJ call | 6 6 6 (two asks failed on JSON) | 5 6 5 5 6 |
| cop car | 6 6 6 (two asks failed on JSON) | 5 5 5 5 5 |
| charges | 5 6 5 6 6 | 6 6 7 6 5 |
| Spread | range 0 to 1, mean range 0.2 | range 0 to 2, mean range 0.6 |

Two findings. First, on a fixed transcript the writer's score was already within one point before; the "7 then 5 or 6" seen in M08 came from re-transcribing the window (a different transcript and a different cut each time), not from scoring the same input twice. Second, **the rubric did not reduce the spread** on this set, and it moved the level down by about half a point (mean 5.9 to 5.3). One transcript ("charges") now crosses the gate's 7 in one run of five. The rubric is in the prompt as asked (four parts with a description per band, five overall bands each with an example, rules tying the score to the parts; file `ai/prompts/_score_rubric.md`, shared by P2 and P9), and `ClipWriting` declares `score_parts` before `score` so the parts are written first. Whether to keep its "when between two numbers, give the lower one" line is a call for whoever tunes the yield: it is the likely reason for the lower level and I did not have the budget to measure the prompt without it.

**Stabiliser (implemented, off by default).** `ai.score_samples` (default `1`). With `3`, `write_clip` asks the fast model to score the finished cut three times with the same rubric (new prompt P9 `p9_clip_score`: the hook and the subtitle lines of the cut in clip time) and stores the median of each part and of the score; a scorer failure keeps the writer's score. Measured on the same five cuts, 15 single P9 scores each, grouped into five medians of three:

| Cut (writer's score) | Single P9 scores | Medians of three |
|---|---|---|
| fixture robbery (5) | range 2 (5 to 7), sd 0.57 | 6 6 6 6 6 |
| ketchup (6) | range 1, sd 0.25 | 5 5 5 5 5 |
| DoJ call (5) | range 2, sd 0.37 | 5 5 5 5 5 |
| cop car (5) | range 2, sd 0.37 | 5 5 5 5 5 |
| charges (6) | range 1 (6 or 7, 9 to 6), sd 0.49 | 6 7 6 6 6 |

Median of three: range 0 on four cuts, 1 on the fifth. A clip the model splits 60/40 between 6 and 7 stays a coin toss whatever is done; that is what a borderline clip is. Cost: 75 requests cost $0.0267, so three samples add about **$0.001 per clip** (P2 itself is about $0.018). It is off by default because it changes which model decides the number the gate reads; it should be switched on in staging and compared with a human's view of the clips before production. P9 gave 0 unusable replies in 75.

### Settings (migration `supabase/migrations/0011_quality.sql`, rows in `docs/16-settings-reference.md`)

| Key | Value | |
|---|---|---|
| `factory.cut_snap` | `{"pause_s": 0.4, "start_back_s": 4.0, "start_forward_s": 2.0, "end_forward_s": 3.0, "end_back_s": 2.0, "vad": true, "punctuate_below": 0.02}` | new |
| `ai.score_samples` | `1` | new |
| `gate.music_min_run_frames` | `2` | was `1` (changed only where it is still `1`) |

**The migration is applied to the test database only. It must be pushed to both Supabase projects**; until then the snap rule is off, scores are the writer's, and the music run length stays 1.

### Deviations and things the next person must know

- Scope: besides the files named in the task I edited `tests/integration/test_factory_pipeline.py` (one expected start, see section 2) and `tests/integration/test_gate_run.py` (the seeded music value). `docs/15-claude-prompts.md` is not updated: it still says "retry once" and does not list P9 and P10.
- New prompts P9 and P10 are in `ai/prompt_files.PROMPT_IDS`, `calls.MAX_TOKENS` and `calls.EFFORT`. Both use `ai.model_fast` and the budget lane of the render.
- `clips.transcript` and the subtitles of a window that Whisper left unpunctuated now carry the full stops P10 added.
- The express path (hosted transcription) also runs the voice activity pass (0.3 to 0.5 s for a 90 s window on this desktop) and, for an unpunctuated transcript, P10 (one fast-model request; its latency was not measured separately). Set `punctuate_below` to `0` if the express lane cannot afford that request.
- The 0.4 s pause and the ranges are first values from five windows, not tuned.

### Commands

| Command | Result |
|---|---|
| `uv run ruff check src tests` and `uv run ruff format --check src tests` | pass |
| `uv run mypy --strict src` | pass (153 files) |
| `uv run pytest tests/unit` | 2673 passed, 6 skipped |
| `uv run pytest tests/integration/test_factory_pipeline.py tests/integration/test_ai_layer.py` (test database `v1` through the tunnel) | 15 passed |
| `RUN_LIVE_TESTS=1 uv run pytest tests/live/test_ai_prompts.py -k "p9 or p10 or p2_writes or p1_single"` | 4 passed |
| `uv run pytest tests/integration/test_gate_run.py tests/integration/test_settings.py` (same database) | 65 passed |
| The rest of `tests/integration` | not run here (over 30 minutes through the tunnel): CI |

### Live spend

About **$1.46** with Anthropic for this work. $1.43 is counted request by request in `runs\ledger.jsonl` (241 requests: P2 measurements $1.28, P5 $0.04, P9 $0.03, the four re-renders $0.08, P10 and the schema check under $0.01). The last four live tests (`tests/live/test_ai_prompts.py -k "p9 or p10 or p2_writes or p1_single"`, 4 passed) wrote their usage rows to the test database, which an integration run then truncated; from the per-request costs above they cost about $0.03. No upload, no post, nothing written to a Supabase project.

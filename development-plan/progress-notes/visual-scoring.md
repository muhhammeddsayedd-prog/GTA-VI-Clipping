## Visual scoring: the gate scores the rendered clip from its frames

**Date:** 2026-11-17 UTC · **Branch:** `visual-scoring` (from `develop` at `1440a14`) · not pushed, not merged

### The problem

Staging rendered 21 clips from Twitch GTA 5 streams and approved none: every clip scored 4 to 6 against `gate.hourly.min_score` = 7. The clip writer (P2) scores from the transcript and the chat sample, before anything is rendered, and is told "do not assume a visual payoff that nothing in them points to" and "when a clip sits between two numbers, give the lower one". In game clips the payoff is on screen. Clip `9887c252` is a streamer checking she has a parachute, jumping off a crane without one and getting the "mission failed" screen; its transcript is "I'm guessing I already have a parachute, right? Yeah, I do. Ooh, fuck." and it scored 6.

### What changed

After the render and before the gate decides, the clip is scored again by a model that sees it (new prompt **P9V**, `ai/prompts/p9_visual_score.md`, call `ai.calls.score_clip_visual`, usage operation `p9_visual_score`).

- **Input.** 8 frames of the rendered clip (432x768 after shrinking to 768 px), the hook, the commentary line, the subtitle lines in clip time, and the chat reaction: messages and chatters in the peak window, the share counted as laughter or shock (`spikes.reaction_count / msgs_in_bucket`), the spike tier, where the peak falls in the clip, and up to 30 chat messages from 15 s before to 25 s after the peak.
- **Rubric.** The shared `_score_rubric.md` is included unchanged (four parts, five bands with their examples, the rules tying the score to the parts). P2 and P9 are not edited. P9V drops the two transcript-only instructions and says "judge the payoff from the frames and the chat reaction as well as the words". It names our overlay (hook box, subtitles, watermark, credit, progress bar, LIVE badge, counter, commentary, blurred bars, webcam panel) as not content. It adds three limits: menus, inventory screens, stream overlays and watched cutscenes have a payoff of 5 or less; people standing and talking with no event and no punchline score 5 or less; clarity is about what a stranger sees. And one consistency rule: payoff and clarity both 7 or more means a score of 7 at least, either at 6 or less means 6 or less.
- **Code backstop** (`calls.bounded_score`): never more than one above payoff, never more than two above clarity, in the 7-8 band only with both at 7 or more, in the 9-10 band only with both at 8 or more. Code only lowers the model's number.
- **Frames** (`gate/visual.score_frame_times`): always the four P5 positions (10, 35, 60, 85%), plus up to four more tried in this order: 3 s before the chat peak, at the peak, 6 s before, 3 s after (replay spikes, whose offset on the video clock is known; skipped when outside the clip or within 1 s of a frame already chosen), then 97.5, 72.5, 47.5 and 22.5% of the clip. One extraction serves both calls: P5 takes its four out of the eight (`visual.p5_frames`).
- **Where it runs** (`gate/gate.run_gate`, `gate/rescore.py`): after the file measurements. The clip is first judged as if its score were 10; if it would be rejected anyway (black frames, music, tier, duplicate, safety flag, ...) nothing is asked. Otherwise the score is asked, stored, the row reloaded, and the gate goes on as before (the existing "rejected without P5" shortcut then applies to the visual score).
- **Stored.** `clips.score` = the visual score. `clips.score_parts` = the visual parts at the top (same four keys as before), `"source": "visual"`, `"writer": {"score", "hook", "clarity", "payoff", "rewatch"}`, `"visual": {"score", four parts, "seen", "model", "frames", "samples"}`. Event `clip.scored` carries the same document. A clip that already has `score_parts.visual` is not scored again (a gate retry after a failed P5 does not pay twice).
- **Failure.** Any failure of the frame grab or the call (API error, invalid reply, a wrong setting) keeps the writer's score, logs `gate_visual_score_failed` and writes the event `clip.visual_score_failed`; the gate then judges the writer's score, so nothing is approved by default. The one exception: `BudgetExceededError` on the VOD lane is raised, and the gate defers the clip to the next budget day exactly as it does for P5 (falling back there would reject good clips for the rest of the day).
- **Budget.** VOD-lane clips are budget-guarded, express clips always run. Hourly clips are asked `ai.visual_score_samples` times (median of each part and of the score, lower middle); express clips once.
- **No gate threshold changed.** `gate.hourly.min_score` is 7, `gate.express.min_score` is 8.

### Settings (migration `supabase/migrations/0016_visual_score.sql`, rows in `docs/16`)

| Key | Value | |
|---|---|---|
| `ai.visual_score` | `true` | off: the writer's score is judged, as before. A database without the key (before 0016) is off |
| `ai.visual_score_model` | `"writer"` | `"writer"` or `"fast"` |
| `ai.visual_score_frames` | `8` | 4 to 8 |
| `ai.visual_score_samples` | `3` | hourly lane; express is always 1 |

**The migration is applied to the test database only. It must be pushed to both Supabase projects**; until then the visual score is off.

### Measurement

15 of the 21 clips were rendered (the other six were rejected by the writer before rendering: three for a safety flag, two as "no moment", one for no speech). The 15 files were copied from the staging server read-only, the rows read with SELECT. Everything below ran on this desktop against the real models with the final code path (`score_clip_visual`), usage rows to the test database, request ledger outside git in `C:\Users\iway\6stars-visual-out\` (`ledger.jsonl`, `results\*.jsonl`, `sheets\*.jpg`).

"Mine" is my own verdict from looking at eight frames of each clip (and a denser sheet for two): would I post it. "Old" is the writer's score. New scores are three asks of the final prompt with 8 frames (`results\s5.jsonl` for the writer model; `h4` and `h5` for the fast model, nine asks).

| Clip | What it is | Old | Writer model: median (asks) | Fast model: median (range of 9) | Mine | Other gate checks it failed |
|---|---|---|---|---|---|---|
| `9887c252` | checks for a parachute, jumps off a crane, falls, "mission failed" | 6 | **7** (7 7 7) | 7 (7-7) | post | black frames and frozen picture 3.2 s (the death screen fades to black) |
| `1b838240` | chaos mod throws the car hundreds of metres over the city, he survives | 5 | **7** (6 7 7) | 7 (6-8) | post | music over the limit |
| `6ef30c38` | air strike destroys the mission truck at once, "MISSION FAILED", restart, more chaos | 5 | **7** (7 7 5) | 7 (6-7) | post, borderline | none |
| `3193c835` | RP crowd outside a casino breaks into a brawl, explosion, bodies and fire, streamer laughs | 5 | 5 (5 5 5) | 6 (6-7) | post, borderline | none |
| `62c1ef5f` | "this magnet is useless", then the helicopter magnet lifts a car and she gapes for ten seconds | 5 | 5 (5 5 5) | 6 (6-7) | borderline, leaning no | none |
| `b6df2f6c` | looting cash in an inventory screen while shouting "we gotta run" | 5 | 4 (5 4 4) | 6 (5-6) | don't post | none |
| `4c7215d9` | subathon prize wheel overlay lands on "2x power hour" | 5 | 4 (4 4 4) | 4 (4-5) | don't post | frozen picture |
| `ceacc8e1` | car drives on; the bird it hit is never visible | 5 | 3 (3 3 3) | 4 (4-5) | don't post | none |
| `05d7db1b` | drives away from a booby trap that is never shown, Japanese chat burned in | 5 | 3 (3 3 3) | 4 (4-5) | don't post | none |
| `e3dc784b` | story cutscene: Trevor meets the crew | 4 | 4 (4 4 5) | 4 (4-5) | don't post | none |
| `cc257a2a` | submarine, then the pause-menu map while he complains about the mod | 5 | 4 (4 4 3) | 4 (4-5) | don't post | frozen picture |
| `0819dcac` | two characters talk in the dark ("would you kiss your sister") | 6 | 3 (3 3 3) | 4 (3-4) | don't post | black frames, frozen picture |
| `0480fd34` | RP clothes shop talk, one hug | 4 | 5 (5 4 5) | 5 (4-6) | don't post | none |
| `eaf250ce` | RP talk at a jail gate | 5 | 3 (3 3 3) | 4 (4-5) | don't post | none |
| `7dd389fc` | retells a TV episode while walking to a car | 5 | 4 (4 4 4) | 4 (3-5) | don't post | none |

**Reading it.**
- Three of the four clips the orchestrator named reach a median of 7 with the writer model. `9887c252` is solid (7 in every ask of both models). `1b838240` is 7 in three of four writer-model asks. `6ef30c38` is a coin toss: four single asks gave 6 7 7 5, and one extra run of the production path (three asks, median) stored 6. The model's reason is the same each time: the fail is shown, then the mission restarts and a second chaos effect muddles the ending.
- `3193c835` does not reach 7 with either model (writer model 5 every time, clarity 4 to 5: "the violence itself is mostly missed", who Felipe is needs the stream). I would post it, but I rate it borderline, and I did not tune the prompt further to lift it: the instructions that would lift it also lift `62c1ef5f` and `05d7db1b`.
- Every weak clip named in the task stays below 7 with both models: inventory 4, wheel 4, bird 3, the talk clips 3 to 5.
- **So on this batch fewer than three clips are clear 7s.** Two are (`9887c252`, `1b838240`), one sits on the line (`6ef30c38`), one sits just under it (`3193c835`). That is a finding about the source material (four of the fifteen are RP talk, three are menus or overlays), not something left to tune.

**What it means for approvals.** On score, 3 of the 21 would now pass (medians above). Through the whole gate, only `6ef30c38` passes, and only when its score lands on 7: `9887c252` is still rejected for `black_frames` and `frozen_video` (3.2 s of black after the fall: the game's own death screen) and `1b838240` for `music_over_limit`. Neither check was touched here. **The black/freeze rule on a death screen and the music detection on `1b838240` are now what stands between the two best clips and the feed**; they need their own look before launch. No clip with a safety flag is affected: flagged clips are rejected by the writer before rendering, and a rendered clip the gate rejects for another reason is never sent to the scorer.

**Model.** Same clips, final prompt, 8 frames:

| | Writer model (`claude-sonnet-5-5`) | Fast model (`claude-haiku-5-5`) |
|---|---|---|
| Asks | 52 (`s4`, `s5`) | 135 (`h4`, `h5`) |
| Agreement of the median of three with the orchestrator's verdicts (4 good, 11 weak) | 14 of 15 (`s5`) | 13, 13 and 14 of 15 in three groups of three |
| Single asks: good clips at 7 or more | 9 of 16 | 24 of 36 |
| Single asks: weak clips at 7 or more | 0 of 36 | 2 of 99 (both `62c1ef5f`) |
| Highest score of a weak clip | 5 (one 6 in 36) | 6 on two clips, 7 on one |
| Spread within a clip | 0 or 1 on 14 clips, 2 on one | 0 to 2 |
| Cost per ask | $0.0108 (7,945 input tokens, 8 frames) | $0.0006 |
| Seconds per ask (median, four in parallel) | 5.0 | 5.2 |

Chosen: **the writer model**. Both find the same good clips; the difference is the margin. The fast model puts weak clips at 6, one step under the line, and crossed it twice; the writer model keeps them at 5 or less. For a gate nobody checks, that margin is worth about one cent a clip. Cost per clip: **$0.011 express (one ask), $0.032 hourly (three asks)**; measured on the production path: 14 s and $0.031 for three asks including the frame grab. `ai.visual_score_model = "fast"` is there if the yield matters more than the margin (about $0.002 per hourly clip).

**Frames.** With the four P5 frames alone the good clips scored lower (writer model, two asks each: `1b838240` 6 7, `6ef30c38` 6 7, `3193c835` 5 4; fast model: `9887c252` 6 6 7, `6ef30c38` 5 6 6). The first prompt version with eight evenly spread frames gave the writer model 6 5 4 on three of the four good clips; moving four of the frames to the chat peak and telling the model that a change between two stills is the event brought two of them to 7. Eight is the cap of the vision call (`VISUAL_FRAMES_MAX`) and costs 0.4 cents more than four.

**Prompt history** (each step measured, `results\`): (1) frames evenly spread: fast model 7 6 6 6 on the good four, writer model 7 6 5 4; (2) frames at the chat peak and "the viewer watched it happen": writer model 7 7 7 5 once, 7 6 6 5 as medians of three, because the model wrote payoff 7 and clarity 7 and still answered 6; (3) the rule that the score follows payoff and clarity, with the code backstop: the table above.

### Live spend

**$1.77** with Anthropic (445 requests in `ledger.jsonl`: writer model $1.60, fast model $0.16). Nothing was uploaded or posted; nothing was written to the staging database or server.

### Tests

- `tests/unit/ai/test_ai_calls.py`: the request (frames, lines, chat summary, overlay named, the two biasing instructions absent), frame shrinking, median of several asks, `bounded_score` table, input errors, budget guard per lane, usage operation name.
- `tests/unit/gate/test_gate_rescore.py`: frame positions (P5 frames always included, peak first), the spike row as the scorer sees it, the switch (on, off, key missing), scored once, the stored `score_parts` shape, express asked once, budget raised on the hourly lane, fallback for an API error, an unexpected error, an invalid reply, a failed frame grab and a wrong setting.
- `tests/integration/test_gate_visual_score.py` (test database, real ffmpeg): approved on the visual score with one extraction and P9V before P5, rejected on a low visual score without P5, rubric limits, fallback, switched off, not scored when rejected for another reason or flagged, not scored twice, chat spike in the request, budget.
- `tests/integration/gate_support.py`: the shared `tables` fixture switches `ai.visual_score` off, so the existing gate, pre-check and CLI tests keep testing the gate's rules on the stored score (the old path); `Kit(score=...)` scripts P9V.

### Things the next person must know

- `docs/10-quality-gate.md` still says "P2 score"; it was not in scope. The score the gate reads is now the visual one when `ai.visual_score` is on.
- A live (express) spike has no offset on a video clock, so its chat peak is not placed in the clip: the extra frames fall back to fixed positions and the chat is shown relative to the peak. Not measured: all 15 clips were replay clips. Express latency was not measured on the server; one ask took 5 s here.
- The hourly lane asks three times in a row (about 15 s). The requests are not sent in parallel.
- The daily report does not read `score_parts.writer` / `.visual` yet; the data is there.
- `ai.score_samples` (P9, text only) still works and still defaults to 1. With the visual score on it would only change the writer's number kept for comparison.
- 15 clips from six streamers is a small sample. The 6/7 line should be looked at again after the first day of real clips: `select score, score_parts->'writer'->>'score', score_parts->'visual'->>'seen' from clips`.

### Commands

| Command | Result |
|---|---|
| `uv run ruff check src tests` and `uv run ruff format --check src tests` | pass |
| `uv run mypy --strict src` | pass (170 files) |
| `uv run pytest tests/unit` | 3227 passed, 6 skipped |
| `uv run pytest tests/integration/test_gate_visual_score.py` (test database `v1` through the tunnel) | 11 passed |
| `uv run pytest tests/integration/test_gate_run.py tests/integration/test_precheck_flow.py tests/integration/test_gate_cli.py tests/integration/test_settings.py` plus the new file (same database) | 101 passed |
| The rest of `tests/integration`, `tests/live` | not run here: CI |

# twitch-clips-source — viewer-made Twitch Clips as a content source

Date: 2026-11-17 UTC. Branch `twitch-clips-source` (from `develop` at `a4ffdde`). Not pushed, not merged. Decision **D110** (the task called it D109; that number was already taken by the admin console).

## Why

The only working source guessed moments from chat spikes in long Twitch streams and approved 0 of about 45 rendered clips: it mostly finds banter and menus. Twitch viewers already clip the best moments, and Helix lists those clips per game with view counts.

## What was built

| Where | What |
|---|---|
| `src/sixstars/clients/twitch.py` | `TwitchClient.get_clips(game_id, started_at, ended_at=None, max_items=100, is_featured=None) -> list[TwitchClip]`: `GET /helix/clips`, paginated with the client's shared retry, logging and usage code (`api_usage`: provider `twitch`, operation `get_clips`, units = requests). `TwitchClip` dataclass. |
| `src/sixstars/clients/fakes.py` | `FakeTwitchClient.get_clips` (window, game, featured filter, most viewed first) and `fake_clip(index, **changes)`. The fake lists **no clips by default**, so existing tests are unchanged. |
| `src/sixstars/viewer_clips/` (new package) | `config.py` (settings), `select.py` (pure rules: fit, rank, pick), `store.py` (all SQL of the `viewer_clips` table), `source.py` (the discovery step), `process.py` (the job), `__init__.py` (`register_handlers()`). |
| `src/sixstars/discovery/run.py`, `commands.py` | Step 7 of a pass calls `viewer_clips.source.run`; the summary gets a `twitch_clips` source entry, `twitch_clips` (counts) and `top_clips` (ten rows); `sixstars discovery run-once` prints the table. |
| `src/sixstars/services/worker.py` | One line in `HANDLER_MODULES`: `"sixstars.viewer_clips:register_handlers"`. |
| `src/sixstars/ai/calls.py`, `src/sixstars/gate/rescore.py`, `src/sixstars/gate/store.py` | Audience evidence for the visual score (below). |
| `src/sixstars/factory/pipeline.py` | `RenderInput.moment_chosen` and `RenderInput.whole_window` (both default false), `reject_stands`, `keep_window` (below). **This file was not on my list**; the change is about 40 lines, inert for every other caller, and the real test showed it was needed. |
| `supabase/migrations/0019_twitch_clips.sql` | Table `viewer_clips`, 11 settings. Additive and idempotent. |
| `docs/00-decisions.md` (D110), `docs/16-settings-reference.md` (new section), `docs/14-api-integrations.md` (the Get Clips facts) | |

### The discovery step (`viewer_clips.source.run`)

Each pass, after the live slots and the videos:

1. Does nothing, with one summary entry, when `twitch_clips.enabled` is false (`skipped`), when Twitch has no keys (`not_configured`), or while `control.emergency_stop` is set (`skipped`). A database without the setting (before migration 0019) is off. It never raises: an error is a source entry with status `error`.
2. One Helix request per 100 clips: the most viewed clips of `env.target_game` made in the last `twitch_clips.lookback_h` hours (`read_per_pass`, default 100).
3. Drops clips that do not fit, counted by reason: `too_short` (under `gate.hourly.min_len`; a clip over `max_len` is kept and trimmed by the factory), `low_views` (`min_views`), `too_fresh` (`min_age_min`), `title_blocklist` (`discovery.title_blocklist` plus "leak"), `no_url`.
4. Ranks the rest: **views per hour since the clip was made** (over at least `rate_floor_h` hours), times `discovery.non_english_penalty` (0.85) when Twitch reports a language other than English. Twitch always reports the broadcast language, so "other" is not taken as English. The parts are stored in `viewer_clips.rank_parts` and shown in the summary (`views`, `age_h`, `views_per_hour`, `language`, `language_factor`, `rank`).
5. `already_seen`: a clip id that is in `viewer_clips` is never stored again, whatever became of it.
6. Room for this pass = the smallest of `max_queued_per_pass`, what is left of `daily_cap` (rows stored in the last 24 hours) and what is left of `max_backlog` (rows in `new` or `processing`). With no room, no creator is looked up.
7. Creators: only for a shortlist (four times the room, not counting a streamer's clips beyond `max_per_creator_per_pass`). The broadcaster is upserted with the existing `tiers.upsert_creators` (so `discovery.excluded_channels` blocks them); a broadcaster we have never stored gets their login from `GET /helix/users`, because the clip row carries only the display name and the login is the handle shown in the credit. Then the existing rules: `restricted` / `blocked` never produce a job (`forbidden_creator`); with `discovery.require_permission_check` a creator who was never checked is checked in the pass with the existing P6 call (`tiers.check_creators`, at most `discovery.permission_checks_per_run`), and waits when the check cannot run (`creator_unchecked`; nothing is stored, so the clip is looked at again next pass; a `creator.permission` job is queued).
8. `per_creator_limit`, then `same_moment`: a clip within `gate.duplicate.source_pad_s` of a range of the same broadcast video that we already have (one of our own clips of that video in any status but `rejected`, an earlier viewer clip that was queued or rendered, or a clip picked a moment ago in this pass) is not queued. Two viewers clipping the same seconds give one render.
9. Each pick: a `viewer_clips` row (status `new`), a `viewer_clip.process` job (queue `normal`, priority 30, dedupe key `viewer_clip:<id>`, payload `{"viewer_clip_id": <id>}`) and the event `viewer_clip.queued`, in one transaction.
10. Rows left open by a job that ended without closing them (it failed for good while its worker was dead) are closed as `failed` after `vod.orphan_grace_s`, so they do not hold the backlog for ever.

Cost per pass: 1 Twitch request for the listing (2 when new broadcasters need their login), 0 to a few P6 calls on the fast model (about $0.00003 each, measured), nothing else.

### The job (`viewer_clip.process`, `viewer_clips.process.handle_job`)

1. A closed row (`done`, `skipped`, `failed`) is left alone. While `control.emergency_stop` is set, the job is queued again for `vod.stop_retry_s` later and nothing is downloaded. A broadcaster who became `restricted` or `blocked` since discovery: the row is `skipped`.
2. Download with yt-dlp through `vod.ytdlp.run` (the VOD lane's wrapper: `vod.download_timeout_s`, three network retries, the failure kinds). No proxy and no cookies (`Access()`). Format: the best landscape rendition up to `downloads.max_height`; Twitch also offers auto-cropped `portrait-*` renditions, which are never taken (the factory makes its own 9:16 layout).
3. `factory.pipeline.render_clip` with a `RenderInput` on the hourly lane, mode `normal`: the window is the whole file (its last `gate.hourly.max_len` seconds when it is longer) and the clip writer does not trim it (`whole_window`, below); the whole file is the padded window, `stream_title` is the clip's title, no chat and no verdict. **`creator` is the broadcaster's handle**: the on-screen credit and the captions name the streamer. The viewer who made the clip is stored in `viewer_clips.clipped_by` and appears nowhere else.
4. `gate.after_render(clip_id, job)`: the existing gate, exactly as after a `clip.render` job.
5. The downloaded file (`$DATA_DIR/viewer_clips/<id>/`) is deleted in a `finally`, whatever happened.

**Idempotent.** `clips.id` is the `viewer_clips` row id (`RenderInput.clip_id`). A retried job finds the same clip row: a finished clip is not rendered or downloaded again (only the gate is called if the clip still waits for it), an unfinished one is rendered again under its first series number.

**Failures.** A clip Twitch no longer has (yt-dlp: "This clip is no longer available", or one of the VOD lane's "unavailable" markers) is closed as `skipped` at once; the job ends as done and is never retried. Any other failure is raised, the queue retries with its backoff, and after the last attempt (3) the row is `failed` (a failed render also closes the clip row through `pipeline.mark_failed`, with its usual notification). `BudgetExceededError` from the render is not a failure: the row goes back to `new` and the job runs again after the next budget reset.

**Events** (entity `viewer_clip`): `viewer_clip.queued`, `.processing`, `.done` (data: `clip_id`, `clip_status`), `.skipped`, `.failed`, `.deferred`. The clip row has its own events from the factory and the gate, as always.

**AI budget.** Nothing new beyond what a render and its gate already use (P2, P8 for non-English speech, P4 once per creator, P9V, P5), in the `vod` budget lane, plus the existing P6 permission check for new broadcasters.

### Row model: why a table of its own

The task suggested a `videos` row per clip. Every row in `videos` belongs to the VOD lane: rows in `new` count against `discovery.max_vod_backlog` (they would stop real videos being queued), and a row left in `processing` is picked up by the lane's recovery (`vod.process.recover_orphans`), which queues a `vod.process` job for it; a 30-second clip would be scanned for chat spikes as if it were a stream. A status or type column would have meant touching a dozen queries in `vod/store.py` and `discovery/store.py`. So: one additive table, `viewer_clips`, that nothing else reads.

### Duplicate protection

- When Twitch gives the broadcast's `video_id` and the clip's `vod_offset` (275 of 300 GTA V clips, 217 of 300 GTA VI clips on 2026-11-17), the clip row is stored **under the broadcast video**: `clips.source_platform = 'twitch'`, `clips.source_id = <video_id>`, `start_s` / `end_s` on the video's clock (`source_offset_s = vod_offset`). That is exactly how the VOD lane stores its clips of a Twitch video, so the gate's existing `check_source_range` (`duplicate_source_range`, pad `gate.duplicate.source_pad_s`) sees a viewer clip and our own VOD-lane clip of the same moment as two clips of one video and rejects whichever comes second. **No change to the gate's duplicate check or its inputs was needed.** `clips.source_url` is the clip's URL.
- Discovery applies the same rule earlier (`same_moment`), so most duplicates are never downloaded.
- Without `video_id` / `vod_offset` the clip row is stored under the Twitch clip id from second 0. Only the gate's transcript-overlap rule (`duplicate_transcript`) can then catch the same speech from another source. Clips younger than `twitch_clips.min_age_min` (15 minutes) wait a pass, because Twitch writes `vod_offset` "typically minutes" after the clip is made.
- Not covered: a spike-lane (live) clip of the same moment. Live clips are stored under the stream id on another clock, so only the transcript-overlap rule applies there.

### Audience evidence for the visual score

`gate.rescore.run` gave P9V the chat reaction of the clip's spike. A viewer clip has no spike, so P9V was told "(no chat data for this clip)" although the system prompt says "the clip exists because chat suddenly got louder". Now, for a clip **without a spike** whose id is in `viewer_clips` (`gate.store.audience_row`, which answers None on a database without the table), the chat summary line becomes, for example:

> No chat spike: this clip was not cut around a chat peak, and there are no chat messages. A viewer of the stream clipped this moment on Twitch by hand and named the clip "THE ARMADA (the last blimp strat)". That Twitch clip had 246 views about 8 hours after it was made. This is evidence that people who watched thought the moment worth keeping, not a score: judge what the frames and the lines show.

`calls.AudienceEvidence` (views, age, title cut to 140 characters and to one line) and `RenderedClip.audience` are optional. The prompt file, the rubric, `bounded_score` and every threshold are unchanged. A clip with a spike never reads it. If the lookup fails, the clip is scored without it (`gate_audience_evidence_failed`): the score is never lost over optional context.

### `RenderInput.moment_chosen` and `twitch_clips.render_without_moment`

Found in the real test (below): the clip writer (P2) closes a clip **before rendering** when it reads no moment in the transcript, and it did that to six of the first eight viewer clips, so the gate and the visual score never saw them. P2's `reject` covers four things: a safety flag, a rejected topic, a blocked spoiler, and "there is simply no moment here". The gate checks the first three again after the render; the fourth is the transcript-only judgement that D108 found unreliable for game clips.

`RenderInput.moment_chosen` (default false; only this source sets it, from the setting `twitch_clips.render_without_moment`): a writer reject that comes with **no safety flag and no spoiler** does not stop the render (`pipeline.reject_stands`); the clip is rendered and the gate judges it on its visual score. A reject with a safety flag or a spoiler always stands. The writer's reason is logged (`clip_writer_reject_overruled`) and kept in the `clip.rendered` event (`writer_reject`). With `ai.visual_score` off such a clip is rejected by the gate for the writer's low score, so nothing is approved by default.

## Settings (migration 0019)

| Key | Default | |
|---|---|---|
| `twitch_clips.enabled` | `true` | the one switch |
| `twitch_clips.lookback_h` | `24` | |
| `twitch_clips.read_per_pass` | `100` | one Helix request per 100 |
| `twitch_clips.max_queued_per_pass` | `3` | |
| `twitch_clips.max_per_creator_per_pass` | `1` | |
| `twitch_clips.min_views` | `10` | see below |
| `twitch_clips.daily_cap` | `48` | rows stored in any 24 hours |
| `twitch_clips.max_backlog` | `6` | rows in `new` or `processing` |
| `twitch_clips.min_age_min` | `15` | wait for `vod_offset` |
| `twitch_clips.rate_floor_h` | `1` | |
| `twitch_clips.render_without_moment` | `false` | see the recommendation below |

Also read: `gate.hourly` (`min_len`, `max_len`), `gate.duplicate.source_pad_s`, `discovery.non_english_penalty`, `discovery.title_blocklist`, `discovery.excluded_channels`, `discovery.require_permission_check`, `discovery.permission_checks_per_run`, `discovery.permission_jobs_per_run`, `discovery.permission_recheck_days`, `vod.download_timeout_s`, `downloads.max_height`, `vod.stop_retry_s`, `vod.orphan_grace_s`.

**Why `min_views` is 10.** Real listings, 24 hours, 300 most viewed clips each (2026-11-17 18:40 UTC):

| | GTA V (32982) | GTA VI (1867279146, before release) |
|---|---|---|
| English | 125 of 300 | 138 of 300 |
| Shorter than 20 s | 54 | 20 |
| With `video_id` and `vod_offset` | 275 | 217 |
| 20 s or more and at least 5 views | 246 | 30 (14 English, 17 streamers) |
| ... at least 10 views | 246 | 16 (4 English, 8 streamers) |
| ... at least 20 views | 246 | 7 (3 English) |
| ... at least 50 views | 228 | 3 (1 English) |
| ... at least 100 views | 92 | 3 |
| Most viewed | 1,835 / 1,213 / 1,042 | 225 / 208 / 112 |

On GTA V every clip in the top 300 has 45 views or more, so any value up to 45 is only a floor there and the ranking and the caps do the choosing. On the thin GTA VI category a floor of 50 leaves three clips a day; 10 leaves sixteen. Under 10 the list is clips almost nobody watched (the 300th has 1 view), where the view count stops being evidence of anything. After release the GTA VI category should look like GTA V's; raise the floor then if the queue fills with weak clips.

## Constants that are not settings

`SHORTLIST_FACTOR` 4 (how deep creators are looked up), `TOP_N` 10 (summary rows), job priority 30 (the `vod.process` value), job attempts 3 (the queue's default), the audience title cut 140 characters, the two "clip is gone" phrases.

## `RenderInput.whole_window`

Also found in the real test: the clip writer trims. Given a 60-second viewer clip ("Ron blew up the hospital!!") it kept the first 20 seconds and cut off the explosion at second 21 that the clip was made for; the gate then scored a helicopter sitting on a plane (visual score 3). `RenderInput.whole_window` (default false; this source always sets it) drops the writer's in and out points and keeps the proposed window (`pipeline.keep_window`); the snap to the words and the length limits still apply, and the writer still writes the hook, title and caption from the whole clip. With it the same clip scored 6. A file longer than `gate.hourly.max_len` keeps its **last** seconds (a viewer presses "clip" after the moment; Twitch clips are at most 60 s, so with the default limit this never applies).

## Real test (2026-11-17, 18:45 to 19:35 UTC)

Setup: staging keys through `SIXSTARS_ENV_FILE` (read only), `DATABASE_URL` = a throwaway database `v3_clips` on the test server (all migrations and `supabase/seed/staging.sql`; **dropped at the end**), `DATA_DIR` outside git, the notify webhook pointed at a closed local port. Real Twitch, real yt-dlp downloads, real Claude calls, real ffmpeg renders, the real gate. Nothing could post: `FakeUploadPostClient`, Storage upload off, and a `FakeYouTubeClient` without OAuth, so the YouTube pre-check could not upload (its state was `unavailable`, which makes the gate apply the strict music limit to risky clips instead of holding them). Local transcription used the Whisper **`small`** model (an override passed to the transcriber; `vod.whisper_model` was not changed). Machine: a Windows desktop, CPU only. Nothing touched Supabase or a server.

### GTA V, the top 8 by rank

Discovery pass: 100 clips read, 80 fit (20 too short), 8 queued with `max_queued_per_pass` = 8 for the test; 12 permission checks, all `auto`.

First run, with the settings as shipped (`render_without_moment` false): the clip writer closed **6 of the 8 before rendering** ("no clear moment", "mostly talking", one "sexual talk"), and the gate rejected the other 2. **Approved: 0 of 8.**

Second run of those 6 with `twitch_clips.render_without_moment` = true, so that every clip reached the gate. Final state of the 8:

| Clip (title on Twitch) | Broadcaster | Views | Length | Language | Gate verdict | Score | My verdict from the contact sheet |
|---|---|---|---|---|---|---|---|
| (Japanese title, a gacha "SSR pick") | mitsuki_mitsuu | 1,060 | 56 s | ja | rejected: `score_too_low`, `music_strict` | 3 (writer; not scored visually, the music rule had already rejected it) | Do not post. A VTuber RP stream with a "you are unconscious" screen and a helicopter; nothing a stranger follows. |
| (Japanese title, "an NPC suddenly joins") | leotukuyomi | 370 | 24 s | ja | rejected: `score_too_low` | 4 (visual 4, writer 3) | Do not post. A car flips over for no visible reason. |
| ceniza | peereira7 | 155 | 30 s | es | rejected: `score_too_low` | 3 (visual 3, writer 2) | Do not post. A shirtless streamer talking to the camera, no game on screen, his own Spanish captions burned in. |
| (Russian title, "the boys for the boys") | rostikfacekid | 158 | 43 s | ru | rejected: `music_over_limit` (0.93), `score_too_low` | 3 (writer) | Do not post. A crowd running across a field to loud music. |
| CARMELLA WINS A GAMESHOW // NOPIXEL V | carmen | 1,848 | 45 s | en | rejected: `score_too_low` | 2 (visual 2, writer 3) | Do not post. RP characters walk through a ship; the win in the title is not in the clip. |
| L'inventaire d'Heitor... | mannyy | 170 | 33 s | fr | rejected: `score_too_low` | 3 (visual 3, writer 3) | Do not post. An inventory screen and talk. |
| ya basta no? | illojuan | 1,214 | 27 s | es | rejected: `music_over_limit` (0.63), `score_too_low` | 3 (writer) | Do not post. Driving while talking about GTA; his own captions burned in. |
| THE ARMADA (the last blimp strat) | darkviperau | 246 | 60 s | en | rejected: `music_over_limit` (0.47), `score_too_low` | 3 (writer; not scored visually) | The picture is a real moment (dozens of blimps, the streamer laughing), but the stream has a music bed and its own captions burned in under ours. Not as rendered. |

**The gate approved 0 of 8. I agree with it on all eight.** The credit on every sheet is the broadcaster (`@carmen`, `@darkviperau`, ...). Non-English speech was translated (P8) and subtitled in English; that path had not been rendered on real footage before, and it worked.

### GTA V, the best 8 English clips (extra, to see what the launch language looks like)

The same test with the language factor pushed down so that English ranked first, `render_without_moment` on.

| Clip | Broadcaster | Views | Length | Gate verdict | Score |
|---|---|---|---|---|---|
| Happy because she is even with Jordan | angelknivez | 622 | 60 s | rejected: `music_over_limit` (0.23) | 3 (writer) |
| How do you rotate the weapons? | syndicate | 733 | 28 s | rejected: `score_too_low` | 3 (visual 3) |
| Clowns get a BIG PAYDAY | twinkles | 299 | 49 s | rejected: `score_too_low` | 4 (visual 4) |
| Don't Mind Me | chatterbox | 591 | 28 s | rejected: `black_frames` (15.9 s), `frozen_video` | 3 (writer) |
| Ron blew up the hospital!! [HeliShet] | sentry | 318 | 60 s | rejected: `score_too_low` | **6** (visual 6 with payoff 7; writer 1) |
| The Last Drive In Los Santos ... | darkviperau | 180 | 30 s | rejected: `score_too_low`, `music_strict` | 3 (writer) |
| Ron's and the plane explosion | cloakrp | 219 | 28 s | rejected: `score_too_low` | 3 (visual 3) |
| :) | ster | 337 | 46 s | rejected: `score_too_low` | 3 (visual 3) |

**Approved: 0 of 8.** One clip is worth posting in my view: "Ron blew up the hospital!!" (a helicopter lands on a flying jumbo jet, an explosion throws the player into free fall). The scorer saw it ("the explosion is a clear visual payoff around 21 s") and gave 6 because the second half is a slow fall with no speech. It needs a cut around the explosion, which nothing in the pipeline can make: the writer cuts blind, from the transcript. I looked at that sheet only; the other seven of this batch are judged from the scorer's descriptions.

### GTA VI (discovery, and the 2 clips it queued)

100 clips read; **12 fit** (13 too short, 75 under 10 views). Top of the list: firelina (ja, 231 views, 30 s), KaiCenat "Kai Crazy Reaction to GTA VI" (en, 223 views, 30 s), dvniel_play (fr, 116 views, 40 s), then clips with 20 to 40 views. Before release the category is reactions to trailers and mis-filed streams, not gameplay. The two queued: firelina closed as `no_speech` (nobody speaks; the factory does not render a clip without words), KaiCenat rendered and rejected for `music_over_limit` (0.23: the trailer's music) with a writer score of 1; from the sheet I would not post it either (trailer footage with its music). That clip was made in the last 24 hours from a broadcast that is much older, which the look-back does not exclude.

### What the test says

1. **The plumbing works end to end** on real data: listing, creator rows and permission checks, download (8 to 14 s per clip), render, translation, the gate, clean-up of the download.
2. **The source did not yield an approved clip: 0 of 18.** The most viewed viewer clips of a category are mostly moments for the streamer's own community: RP conversations, in-jokes, a streamer talking to the camera. Views follow the size of the streamer, not how well the clip works for a stranger. 16 of the 17 rendered clips scored 4 or less.
3. **Music** rejected 5 of 18 (streamers with a music bed, a trailer).
4. Three of the first eight had the streamer's own captions burned in; ours are drawn on top.
5. At the default language factor (0.85) six of the top eight were not English.
6. A clip in which nobody speaks cannot be rendered (`no_speech`), which rules out pure-gameplay clips.

### Spend

Anthropic, all runs including the repeats (`api_usage` of the throwaway database): P9V 33 calls $0.37, P2 30 calls $0.22, P4 16 calls $0.06, P8 10 calls $0.04, P6 29 calls $0.001. **Total $0.70** (limit $3). Transcription was local: $0. Twitch and yt-dlp: free. Per clip: about $0.008 when the writer closes it before rendering, about $0.04 to $0.05 when it is rendered and scored three times.

### Files (outside git)

`C:\Users\iway\6stars-twitch-clips-out\`: `data\ready\*.mp4` (17 rendered clips; `data\ready\first\` holds the first render of the three that were rendered again with the whole clip kept), `sheets\<first 8 characters of the clip id>.jpg` (17 contact sheets), `data\results_final.json` (every verdict), `data\gta5_source.json` and `data\gta6_source.json` (the discovery summaries), `data\gta5_listing.json` and `data\gta6_listing.json` (the raw listings), `logs\`, `scripts\`.

## Recommendations (not done; they are the owner's or the orchestrator's call)

- **`twitch_clips.render_without_moment`**: shipped `false` (the option that renders and posts less). With `false` the transcript-only writer closes most viewer clips before the gate sees them, including the one clip of the 18 I would post (writer score 1, visual score 6). If this source is meant to find visual moments, set it to `true`; it lowers no gate threshold and costs about 4 cents and one render per clip.
- **Ranking.** Views per hour favours the communities of big streamers. Better candidates for a stranger are probably clips whose views are high *for that streamer*, and clips whose title names an event. Not built: it needs per-streamer history.
- **RP.** A large share of the weak clips are role-play conversations. `discovery.title_blocklist` applies to clip titles too (for example `nopixel`), but most RP clips do not say so in the title.
- **Trimming.** The best clip needed a cut around its visual event. A vision-based trim would be a new piece of the factory.
- **Language.** To keep the source to English streams there is no switch of its own: `discovery.non_english_penalty` is shared with streams and videos. A `twitch_clips.languages` setting would be a small addition.

## Deviations from the task

1. **D110, not D109** (taken).
2. **A table `viewer_clips` instead of rows in `videos`** (see "Row model").
3. **A package `src/sixstars/viewer_clips/`** holds the source and the job (the task offered `discovery/` and `vod/viewer_clips.py`); `discovery/run.py` calls it.
4. **`factory/pipeline.py` was edited** (two opt-in fields on `RenderInput`, two small pure functions, one event field), although the file was not on my list. Every other caller is unchanged.
5. **Extra settings** beyond the list: `max_backlog`, `min_age_min`, `rate_floor_h`, `render_without_moment`.
6. **The daily cap is a rolling 24 hours**, not a UTC day, so midnight does not release 48 clips at once.
7. **`control.emergency_stop` stops this source's queueing and defers its jobs**, as the VOD lane does, although the setting's description says "discovery and rendering continue".
8. **The fake lists no clips by default** (three default clips would have changed existing discovery tests).
9. **No change to the gate's duplicate check**: the row model made it unnecessary.
10. The extra English batch and the three repeats with the whole clip kept were not asked for; they cost $0.37 of the $0.70.
11. `docs/14-api-integrations.md` got a row with the verified Get Clips facts.

## Commands

| Command | Result |
|---|---|
| `uv sync` | pass |
| `uv run ruff check src tests`, `uv run ruff format --check src tests` | pass |
| `uv run mypy --strict src` | pass (178 files) |
| `uv run pytest tests/unit` | 3,341 passed, 6 skipped |
| `uv run pytest tests/integration/test_viewer_clips.py tests/integration/test_discovery_run.py` (test database `v3`) | pass (41) |
| `uv run pytest tests/integration/test_factory_pipeline.py tests/integration/test_services_worker.py tests/integration/test_discovery_hardening.py tests/integration/test_discovery_blocked.py tests/integration/test_gate_visual_score.py` | 55 passed, 1 failed (see below) |
| The rest of `tests/integration`, `tests/live` | not run here: CI |

The one failure, `test_gate_visual_score.py::test_a_clip_rejected_for_another_reason_is_not_scored`, is not from this branch: its fixture `black_video` (`tests/integration/gate_support.py`) has 3 seconds of black, and migration 0017 (commit `b36a781`) raised the limit to 4 seconds without changing the fixture, so the clip is approved. Neither file is touched here.

## To enable on a server

1. Merge the branch and deploy as usual (the worker image must contain `sixstars.viewer_clips`; the handler is registered through `HANDLER_MODULES`).
2. Apply `supabase/migrations/0019_twitch_clips.sql` to the project (table `viewer_clips` and 11 settings; additive and idempotent). Before it is applied the code is inert: the source is off without its setting, and the gate's lookup answers None without the table.
3. Restart the API and both workers (a new job type, and the discovery step lives in the process that serves `POST /v1/discovery/run`).
4. Twitch keys must be present (they are, D107). The source starts with the next discovery pass because `twitch_clips.enabled` is seeded `true`; to deploy it switched off, set it to `false` right after the migration.
5. Decide `twitch_clips.render_without_moment` (see the recommendation).
6. Watch: `select status, count(*) from viewer_clips group by 1;` and the `twitch_clips` entry of the `discovery.run` event.

## Not verified

- Nothing ran on the staging or production server, on Linux, or in the Docker image. The download was tested from a home connection only; whether Twitch serves clip files to the Hetzner address without a proxy is assumed from the fact that Twitch VOD downloads work there (D107).
- The Whisper `medium` model (the server's setting) was not used; `small` was.
- The YouTube pre-check path (`precheck_pending`) was not exercised: the test ran with the pre-check unavailable. Two clips carry `music_strict` only because of that; both were also rejected for their score.
- A `duplicate_source_range` rejection between a viewer clip and a VOD-lane clip of the same broadcast was shown with database rows in the integration test, not with two real renders.
- The rate limit was not approached (5 listing requests in the whole test).
- No clip was approved, so nothing after the gate (the hourly queue, posting) ran with a viewer clip. An approved viewer clip is an ordinary `clips` row with `lane = 'hourly'` and a `rank_score`, which is all the hourly queue reads.
- The admin console does not show `viewer_clips` rows (`web/` and `src/sixstars/api/` were out of scope); the clips they produce appear there like any other clip.

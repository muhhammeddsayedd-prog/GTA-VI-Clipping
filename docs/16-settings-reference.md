# 16 — Settings Reference

All runtime settings live in the `settings` table (key → JSON value). The code reads them through `sixstars.settings.get(key)` with a 30-second cache, so changes apply within 30 seconds without a redeploy. Change them with `sixstars settings set <key> '<json>'`, the control API (`POST /v1/control/settings`) or SQL.

Defaults below are the production values from `supabase/migrations/0002_seed_settings.sql`. Staging overrides are in `supabase/seed/staging.sql`. Posting accounts and their caps live in the `platform_accounts` table (see `supabase/seed/`).


## Environment

| Key | Default | Meaning |
|---|---|---|
| `env.target_game` | `"Grand Theft Auto VI"` | Twitch category name / game the system clips. Staging: Grand Theft Auto V. |
| `env.search_terms` | `["GTA 6", "GTA VI", "Grand Theft Auto VI", "GTA6"]` | YouTube search terms for discovery. Staging: GTA 5 terms. |
| `env.game_release_at` | `"2026-11-19T00:00:00Z"` | Game release time (used by the spoiler rule). |
| `launch.launch_at` | `"2026-11-18T11:00:00Z"` | Posting goes live at this time. Before it, posting runs in dry-run mode. |
| `launch.dry_run` | `true` | If true, posting logs what it would post but publishes nothing. Production sets false before launch; posting still waits for launch_at. |
| `control.emergency_stop` | `false` | If true, all posting stops. Discovery and rendering continue. |
| `control.stop_reason` | `null` | Why the emergency stop was set. |
| `app.timezone` | `"Africa/Cairo"` | IANA time zone for every time shown to people (console, Telegram, daily report) and for the daily report's day. Limits, caps, budgets, hourly slots and stored times stay UTC. Set it from the console's Settings → Time zone card. (0055) |

## Discovery

| Key | Default | Meaning |
|---|---|---|
| `discovery.apify_last_run` | `null` | Written by code: the last Apify run as `{run_id, started_at, mode, collected, status, cost_usd}` (read on a later pass). |
| `discovery.interval_s` | `600` | Expected interval between discovery runs (failover uses this). |
| `discovery.vod_lookback_h` | `48` | How far back to look for finished videos. |
| `discovery.min_vod_duration_s` | `480` | Skip videos shorter than this. |
| `discovery.live_score_cutoff` | `55` | Minimum trend score for a live slot. |
| `discovery.vod_score_cutoff` | `50` | Minimum trend score to queue a video. |
| `discovery.max_vod_per_run` | `6` | Max new videos queued per discovery run. |
| `discovery.release_after_below_s` | `1200` | Release a live slot after the stream stays below the cutoff this long. |
| `discovery.weights_live` | `{"viewers": 0.35, "growth": 0.25, "chat_rate": 0.2, "global_reach": 0.1, "history": 0.1}` | Trend score weights for live streams. |
| `discovery.weights_vod` | `{"views_per_hour": 0.45, "engagement": 0.15, "size_ratio": 0.15, "global_reach": 0.1, "history": 0.15}` | Trend score weights for videos. |
| `discovery.non_english_penalty` | `0.85` | Multiplier on trend score for non-English sources. |
| `discovery.preferred_bonus` | `15` | Points added for preferred creators. |
| `discovery.title_blocklist` | `["sponsored", "#ad", "music only", "lofi", "radio", "casino", "gambling", "slots"]` | Titles containing these are skipped. |
| `discovery.youtube_search_results` | `40` | Results read per yt-dlp YouTube search (one search per term for live, one per term and sort for recent videos). (M09) |
| `discovery.youtube_recent_sorts` | `["relevance", "views"]` | Sort orders of the recent-video searches: relevance, views, date or rating. (M09) |
| `discovery.ytdlp_workers` | `6` | yt-dlp searches and RSS reads that run at the same time. (M09) |
| `discovery.ytdlp_timeout_s` | `25` | Seconds before one yt-dlp search is killed. (M09) |
| `discovery.youtube_quota_daily_units` | `9000` | YouTube Data API quota units discovery may use per quota day (from midnight in `discovery.youtube_quota_timezone`; the project has 10,000). Over it, discovery scores from the search listing alone. (M09; 3000 until migration 0024) |
| `discovery.youtube_quota_timezone` | `"America/Los_Angeles"` | Time zone whose midnight starts the YouTube quota day (Google resets the quota at midnight Pacific time). Used for the units above and the `search.list` call count; money budgets keep the UTC day. (0024) |
| `discovery.youtube_channel_cache_ttl_s` | `3600` | Seconds a `channels.list` answer is reused from `youtube_channel_cache` (follower counts, permission checks and jobs, avatars). 0 turns the cache off. (0024) |
| `discovery.rss_max_channels_per_run` | `50` | Known YouTube creators whose RSS feed is read per run (rotating). (M09; 25 until migration 0010) |
| `discovery.rss_cursor` | `null` | Written by code: where the RSS rotation continues. (M09) |
| `discovery.apify_interval_s` | `21600` | Minimum seconds between Apify YouTube searches (they alternate live and recent). 0 switches Apify off. (M09) |
| `discovery.apify_max_results` | `5` | Results per search term of one Apify run. (M09) |
| `discovery.apify_price_per_result_usd` | `0.003` | Apify price per video written, used to project the cost of a run. (M09) |
| `discovery.apify_daily_budget_usd` | `0.4` | Apify spend allowed per UTC day. A run that would pass it is not started. (M09) |
| `discovery.apify_monthly_budget_usd` | `12` | Apify spend allowed per calendar month (UTC). A run that would pass it is not started. (M09) |
| `discovery.twitch_max_streams` | `300` | Live Twitch streams read per run. (M09) |
| `discovery.twitch_max_videos` | `50` | Twitch archives read per run. (M09) |
| `discovery.twitch_follower_lookups_per_run` | `20` | Twitch follower counts fetched per run for creators that have none. (M09) |
| `discovery.twitch_game_cache` | `null` | Written by code: Twitch game id of env.target_game, cached 24 h. (M09) |
| `discovery.growth_window_s` | `1800` | Live growth compares viewers now with viewers this long ago. (M09) |
| `discovery.min_observation_gap_s` | `120` | A source observed again within this time updates its last candidates row instead of adding one. (M09) |
| `discovery.min_live_viewers` | `50` | Live streams with fewer viewers are recorded as candidates but never get a live slot. (M09) |
| `discovery.chat_mpm_per_viewer` | `0.01` | Estimated chat messages per minute per live viewer, used when no chat of the creator was measured yet. (M09) |
| `discovery.chat_msgs_per_view` | `0.02` | Estimated chat messages per view of a finished stream, used when nothing better is known. (M09) |
| `discovery.chat_min_messages` | `1500` | Chat replay size at which a finished stream gets the full chat-replay bonus (the spike engine finds nothing on thinner chats). (M09) |
| `discovery.chat_replay_bonus` | `5` | Points added to a video with a chat replay, scaled by its estimated chat size. (M09) |
| `discovery.passive_views_engagement` | `0.005` | A video whose (likes + 2 x comments) / views is below this was viewed passively (Shorts feed): its views-per-hour score and chat estimate are scaled down in proportion. 0 switches this off. (M09) |
| `discovery.max_vod_per_creator_per_run` | `1` | Max new videos of one creator queued per discovery run. (M09) |
| `discovery.max_vod_backlog` | `24` | Discovery queues no more videos while this many are waiting for the VOD lane (videos.status = new). Videos waiting out a download block are not counted. (M09, fix-blocked-platform-queue) |
| `discovery.permission_recheck_days` | `14` | Days after which a creator's channel description is read again (prompt P6). (M09) |
| `discovery.require_permission_check` | `true` | When true a creator gets no live slot and no video job before the first permission check (P6) has run. (M09) |
| `discovery.permission_checks_per_run` | `12` | Permission checks made inside one discovery run for creators about to be picked. (M09) |
| `discovery.permission_jobs_per_run` | `25` | creator.permission jobs queued per discovery run for other new or stale creators. (M09) |
| `discovery.excluded_channels` | `[]` | Creators that are never clipped: channel ids or handles, optionally prefixed with "youtube:" or "twitch:". (M09) |
| `discovery.search_api_mode` | `"fallback"` | YouTube Data API search.list as a discovery source: "fallback" (only in a pass where the yt-dlp search failed, found nothing or is backing off), "always" or "off". (hardening-sources) |
| `discovery.search_api_calls_per_pass` | `1` | search.list calls one discovery pass may make at most. (hardening-sources) |
| `discovery.search_api_interval_s` | `1800` | Seconds between two search.list calls (1800 = every third pass at the 10-minute cadence, 48 calls a day). (hardening-sources) |
| `discovery.search_api_daily_calls` | `60` | search.list calls per UTC day at most. Google allows 100 a day in a bucket of its own; the rest is left for the doctor and manual runs. (hardening-sources) |
| `discovery.search_api_modes` | `["live", "recent"]` | What the search.list calls rotate through: "live" (eventType=live), "recent" (any video published within discovery.vod_lookback_h), "completed" (finished broadcasts only). (hardening-sources) |
| `discovery.search_api_combined_modes` | `["recent", "completed"]` | Modes that search every term of env.search_terms in one OR query per call. The other modes take one term per call, in turn (measured: the OR query finds few live streams). (hardening-sources) |
| `discovery.search_api_durations` | `{"recent": "long", "completed": "long"}` | search.list videoDuration per mode: any, long (over 20 minutes), medium (4 to 20 minutes) or short. Without it half of the recent results are Shorts. (hardening-sources) |
| `discovery.search_api_orders` | `{"live": "viewCount", "recent": "viewCount", "completed": "viewCount"}` | search.list order per mode: date, rating, relevance, title or viewCount. (hardening-sources) |
| `discovery.search_api_relevance_language` | `"en"` | search.list relevanceLanguage (results most relevant to this language); "" to leave it out. (hardening-sources) |
| `discovery.search_api_category_id` | `"20"` | search.list videoCategoryId (20 = Gaming); "" to leave it out. (hardening-sources) |
| `discovery.search_api_state` | `null` | Written by code: rotation position, time of the last search.list call, quota pause. (hardening-sources) |
| `discovery.live_recheck_max` | `50` | YouTube streams seen live before (and every stream holding a live slot) that are looked up again per pass with videos.list, 1 quota unit per 50. 0 = off. (hardening-sources) |
| `discovery.live_recheck_window_s` | `5400` | A stream is looked up again while its last live observation is at most this old. (hardening-sources) |
| `discovery.source_failure_notify_after` | `3` | Failed passes in a row after which a discovery source is reported once (warn); one info follows when it works again. (hardening-sources) |
| `discovery.ytdlp_backoff_after_failures` | `2` | Failed passes in a row after which the yt-dlp search skips itself for discovery.ytdlp_backoff_s. (hardening-sources) |
| `discovery.ytdlp_backoff_s` | `3600` | Seconds the yt-dlp search is skipped after repeated failures; then one search probes whether it works again. (hardening-sources) |
| `discovery.ytdlp_use_cookies` | `false` | true passes YOUTUBE_COOKIES_FILE (when the file exists) to the yt-dlp search as well. Off by default: the cookies are kept for chat and downloads. (hardening-sources) |
| `discovery.source_health` | `null` | Written by code: failed passes in a row per discovery source and the yt-dlp back-off. (hardening-sources) |
| `discovery.non_latin_share` | `0.3` | Share of non-Latin letters in a title, channel name or description above which a video counts as not English. (hardening-sources) |
| `discovery.non_english_terms` | `["hindi", "हिंदी", "हिन्दी", "urdu", "tamil", "telugu", "malayalam", "kannada", "bangla", "bengali", "marathi", "punjabi", "nepali", "indonesia", "tagalog", "pinoy", "español", "espanol", "en vivo", "en directo", "português", "portugues", "ao vivo", "brasil", "deutsch", "français", "francais", "türkçe", "turkce", "shortsfeed", "shortslive", "secret mod"]` | Words that mark a video as not English (or as having no usable speech, like the Shorts-feed streams) when the title, channel name, a tag or a description hashtag contains one (substring, any case). Overrules a reported English; the video is ranked lower, not dropped. (hardening-sources) |
| `discovery.edited_upload_bonus` | `6` | Score points an edited upload or highlight (no chat replay, at most discovery.edited_max_duration_s long) gets at full engagement; in proportion below it. (hardening-sources) |
| `discovery.edited_max_duration_s` | `3600` | An upload without chat replay up to this length counts as edited material; longer ones as raw recordings. (hardening-sources) |
| `discovery.edited_engagement_full` | `0.04` | (likes + 2 x comments) / views at which an edited upload gets the whole discovery.edited_upload_bonus. (hardening-sources) |
| `discovery.long_stream_min_s` | `3600` | Past streams and raw uploads at least this long are scored by how much chat they have (discovery.thin_chat_factor). (hardening-sources) |
| `discovery.thin_chat_factor` | `0.75` | Score factor of a long past stream with no chat at all; rises to 1 as the estimated chat replay reaches discovery.chat_min_messages. 1 = off. (hardening-sources) |
| `discovery.platform_weight` | `{}` | Factor on the trend score per platform, for example `{"twitch": 1.2, "youtube": 0.8}`. A platform that is not named has factor 1; the score stays inside 0 to 100. `{}` = no preference. (fix-blocked-platform-queue) |
| `discovery.max_blocked_waiting` | `12` | Videos per platform that may wait out a download block (videos.status = new, skip_reason download_blocked). Discovery marks the oldest beyond this as skipped so they do not retry for ever. (fix-blocked-platform-queue) |
| `discovery.blocked_platform_stale_s` | `7200` | A download block recorded in vod.source_health counts for this many seconds after its last refusal; then discovery queues the platform again. Keep it well above vod.blocked_backoff_s. (fix-blocked-platform-queue) |

## Twitch Clips source

Viewer-made Twitch Clips of `env.target_game` as a discovery source (decision D110, migration 0019). The length limits come from `gate.hourly` (`min_len`, `max_len`), the language factor from `discovery.non_english_penalty`, the same-moment distance from `gate.duplicate.source_pad_s`; the title blocklist, the excluded channels and the permission-check settings are those of discovery. The download uses `vod.download_timeout_s` and `downloads.max_height`; a job stopped by `control.emergency_stop` runs again after `vod.stop_retry_s`.

| Key | Default | Meaning |
|---|---|---|
| `twitch_clips.enabled` | `true` | Viewer-made Twitch Clips are a discovery source. `false`: the source does nothing. It also does nothing without the Twitch keys, while `control.emergency_stop` is set, and on a database without this key. |
| `twitch_clips.lookback_h` | `24` | Each pass reads the most viewed clips made in the last this many hours. |
| `twitch_clips.read_per_pass` | `100` | Clips read from Twitch per pass, most viewed first (100 per request; a request costs one rate-limit point). |
| `twitch_clips.max_queued_per_pass` | `3` | Clips queued for rendering per discovery pass, at most. |
| `twitch_clips.max_per_creator_per_pass` | `1` | Clips of one streamer queued per discovery pass, at most. |
| `twitch_clips.min_views` | `10` | A clip with fewer views on Twitch is not queued. Measured 2026-11-17 over 24 hours: every one of the 300 most viewed GTA V clips had 45 views or more, so there this is only a floor; in the GTA VI category before release 16 clips of 20 s or more had 10 views or more (7 had 20, 3 had 50). |
| `twitch_clips.daily_cap` | `48` | Clips queued from this source in any 24 hours, at most (two per hourly posting slot). |
| `twitch_clips.max_backlog` | `6` | No more clips are queued while this many wait to be rendered (status `new` or `processing`). |
| `twitch_clips.min_age_min` | `15` | A clip younger than this waits for a later pass: Twitch writes its position in the broadcast (`vod_offset`, which the duplicate check needs) some minutes after the clip is made. |
| `twitch_clips.rate_floor_h` | `1` | Views per hour are computed over at least this many hours. |
| `twitch_clips.render_without_moment` | `false` | `true`: a viewer clip is rendered and judged by the gate even when the clip writer (P2), reading the transcript alone, says there is no moment in it. A writer reject with a safety flag or a spoiler always stands. `false`: the reject closes the clip before rendering, as on every other lane. |

## Live lane

| Key | Default | Meaning |
|---|---|---|
| `live.max_slots` | `6` | Max streams recorded at once. |
| `live.segment_s` | `60` | Recorder segment length. |
| `live.buffer_s` | `1800` | Rolling buffer kept on disk. |
| `live.max_reconnects` | `3` | Recorder reconnect attempts before releasing the slot. |
| `live.chat_offset_s` | `{"twitch": 0, "youtube": 0}` | Extra seconds to shift chat time vs recorded video (tuned in staging). |
| `live.poll_s` | `15` | Seconds between two looks of the live manager at the live_sessions table (start assigned sessions, stop released ones). (M14) |
| `live.max_height` | `1080` | Highest video height the live recorder takes (streamlink picks the best stream at or below it). Lower it to 720 to halve disk and bandwidth per slot. (M14) |
| `live.reconnect_backoff_s` | `10` | Seconds the recorder waits before it reconnects after a stream drop. (M14) |
| `live.reconnect_reset_s` | `300` | A reconnected recorder that runs this long clears the count of failed attempts (live.max_reconnects counts failures in a row). (M14) |
| `live.start_timeout_s` | `60` | Seconds a recorder start may take to deliver the first stream data before it counts as failed. (M14) |
| `live.stall_timeout_s` | `45` | Seconds without new stream data after which a running recorder is restarted. (M14) |
| `live.ad_break_max_s` | `300` | Longest Twitch ad break the recorder waits out without data (streamlink skips the ad segments) before it treats the silence as a stalled stream. (M14) |
| `live.max_lag_s` | `30` | A recorder run whose stream arrives more than this many seconds behind real time (the platform throttles the downloads) is ended and the platform backed off like a blocked one. 0 = do not watch. (M14) |
| `live.stop_timeout_s` | `8` | Seconds a recorder gets to close its last segment when a session stops, before it is killed. (M14) |
| `live.blocked_backoff_s` | `1800` | Seconds a platform that refuses this machine (bot check, rate limit) is left alone before one stream probes it again. The first refusal waits a sixth of it. (M14) |
| `live.blocked_notify_after` | `2` | Refused stream starts in a row after which a blocked live source is reported once (warn); one info follows when it records again. (M14) |
| `live.source_health` | `null` | Written by code: refused starts in a row per live source and the end of its back-off. (M14) |
| `live.max_buffer_bytes` | `3000000000` | Hard cap of one slot's rolling buffer in bytes: the oldest segments are deleted beyond it, whatever live.buffer_s says. (M14) |
| `live.min_free_bytes` | `8000000000` | No new live session starts while the data volume has fewer free bytes than this (running ones continue inside their buffers). (M14) |
| `live.cut_settle_s` | `4` | Seconds waited after the end of a window before it is cut from the buffer, so the stream data for its last seconds has arrived. (M14) |
| `live.cut_wait_max_s` | `70` | Longest wait for a window that ends in the future before it is cut anyway. (M14) |
| `live.express_pad_wait_s` | `6` | Seconds of context after the clip window an express (S-tier) cut waits for at most; the rest of spikes.context_pad_s is given up for speed. (M14) |
| `live.cut_keep_s` | `2700` | Seconds a cut file is kept for its render job (and the job's retries) before the janitor deletes it. (M14) |
| `live.anchor_span_s` | `300` | Segments within this many seconds of a window are used to align stream timestamps with the wall clock. (M14) |
| `live.replay_delay_s` | `900` | Seconds after a YouTube stream ends before its replay scan runs (the chat replay needs time to appear). (M14) |
| `live.replay_find_interval_s` | `600` | Seconds between two look-ups of the Twitch archive of an ended stream. (M14) |
| `live.replay_find_max_s` | `7200` | How long the Twitch archive of an ended stream is looked for before giving up. (M14) |
| `live.priority_express` | `100` | Job priority of an S-tier live spike (express.render, queue express). (M14) |
| `live.priority_a` | `60` | Job priority of an A-tier live spike (clip.render, queue normal, hourly lane). (M14) |
| `live.priority_b` | `40` | Job priority of a B-tier live spike (clip.render, queue normal, hourly lane). (M14) |
| `live.ab_clips` | `true` | false = only S-tier live spikes become clips; A and B are left to the replay pass after the stream. (M14) |

## Spike engine

| Key | Default | Meaning |
|---|---|---|
| `spikes.bucket_s` | `10` | Chat bucket size in seconds. |
| `spikes.live_step_s` | `5` | Live mode: how often the sliding window advances. |
| `spikes.baseline_s` | `600` | Trailing window used for the baseline. |
| `spikes.warmup_s` | `300` | Ignore the first N seconds of a stream. |
| `spikes.min_std` | `{"msgs": 2.0, "uniq": 1.5, "react": 0.05, "clipreq": 0.5, "audio": 1.5}` | Floor for baseline standard deviations (stops quiet streams from spiking on noise). |
| `spikes.weights` | `{"msgs": 0.4, "uniq": 0.25, "react": 0.2, "clipreq": 0.1, "audio": 0.05}` | Weights of each z-score in the spike score. |
| `spikes.z_s` | `4.0` | S-tier threshold. |
| `spikes.z_a` | `3.0` | A-tier threshold. |
| `spikes.z_b` | `2.5` | B-tier threshold. |
| `spikes.s_unique_multiple` | `3.0` | S-tier also needs unique chatters >= this multiple of baseline. |
| `spikes.s_min_confidence` | `0.8` | S-tier needs Claude confidence >= this. |
| `spikes.ab_min_confidence` | `0.55` | A/B need Claude confidence >= this. (0.6 until migration 0010, see the M08 notes) |
| `spikes.min_messages` | `15` | Minimum messages in a spike bucket (absolute floor). |
| `spikes.merge_gap_s` | `60` | Spikes closer than this are merged. |
| `spikes.end_guard_s` | `300` | Replay scans ignore spikes in the last N seconds of a video (goodbye messages). Added in migration 0004. |
| `spikes.lead_s` | `20` | Clip starts this many seconds before the spike (15-30). |
| `spikes.end_z` | `1.5` | Clip ends when the score falls below this. |
| `spikes.tail_s` | `5` | Seconds added after the score falls. |
| `spikes.min_len_s` | `15` | Minimum clip length. |
| `spikes.max_len_s` | `60` | Maximum clip length. |
| `spikes.context_pad_s` | `15` | Extra seconds either side downloaded/transcribed for Claude fine-tuning. |
| `spikes.reaction_tokens` | `["LMAO", "LMFAO", "LOL", "LUL", "KEKW", "OMEGALUL", "NO WAY", "NOWAY", "WTF", "OMG", "W", "L", "POG", "POGGERS", "Pog", "PogChamp", "💀", "😂", "🤣", "😭", "🔥", "CLIP IT", "CLIP THAT", "HOLY", "BRO", "DEAD"]` | Reaction words/emotes counted (case-insensitive, whole token). |
| `spikes.clip_tokens` | `["clip", "clipped", "clip it", "clip that", "clipit"]` | Tokens that count as clip requests. |
| `spikes.ignore_users` | `["nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "soundalerts", "wizebot", "botisimo", "sery_bot", "kofistreambot"]` | Known bots always ignored. |

## VOD lane

| Key | Default | Meaning |
|---|---|---|
| `vod.max_spikes_per_video` | `25` | Max spikes processed per video (best first). |
| `vod.fallback_moments` | `[3, 8]` | Min/max moments Claude picks in transcript fallback. |
| `vod.whisper_model` | `"medium"` | Local faster-whisper model. |
| `vod.paused_until` | `null` | Set by guardian when the spend cap is hit; VOD jobs wait until this time. |
| `downloads.proxy_platforms` | `["youtube"]` | Platforms whose downloads (yt-dlp, streamlink) go through PROXY_URL. |
| `downloads.max_height` | `1080` | Max source video height downloaded. |
| `vod.max_concurrent` | `1` | Videos the VOD lane processes at the same time. The lane never uses every live worker of the normal queue: the limit in force is min(this, live normal workers - 1), at least 1. A job over the limit waits vod.concurrency_retry_s, longer each time. (M08, fix-stale-processing) |
| `vod.concurrency_retry_s` | `120` | Seconds a vod.process job waits the first time no slot is free; the wait doubles with every further deferral up to vod.concurrency_retry_max_s. (M08, fix-stale-processing) |
| `vod.processing_stale_s` | `7200` | A video in status processing that was not updated for this long may be taken over by a run that has no job (CLI). Slots are counted with vod.orphan_grace_s instead. (M08, fix-stale-processing) |
| `vod.render_priority` | `{"S": 55, "A": 55, "B": 40}` | Priority of the clip.render job per spike tier. (M08) |
| `vod.clipped_range_margin_s` | `30` | Replay pass after a live stream: spikes closer than this to a range the live lane already clipped are dropped. (M08) |
| `vod.chat_retry_delay_s` | `20` | Seconds before the single retry when YouTube reports no chat replay for a past stream. (M08) |
| `vod.confirm_with_captions` | `true` | Fetch the caption track (no media) and show the words around each spike to the confirmation prompt. (M08) |
| `vod.caption_langs` | `[".*-orig", "en"]` | Caption tracks asked for, in order of preference: language codes or patterns (.*-orig is the automatic track in the language spoken). (M08) |
| `vod.caption_timeout_s` | `180` | Seconds before the yt-dlp caption download is killed. (M08) |
| `vod.download_timeout_s` | `900` | Seconds before one yt-dlp window download is killed. (M08) |
| `vod.download_failures_max` | `3` | Failed window downloads in a row after which the video is given up for this attempt. (M08) |
| `vod.download_workers` | `3` | Windows of one video downloaded at the same time (YouTube serves each at about 1.5 times playback speed). (M08) |
| `vod.section_exact_cuts` | `false` | true re-encodes each downloaded window so it starts on the exact frame (slow); false copies the streams and relies on the edit list ffmpeg writes. (M08) |
| `vod.blocked_backoff_s` | `1800` | Downloads refused by the platform (YouTube bot check, rate limit): seconds before videos of that platform are tried again. (M08) |
| `vod.blocked_notify_interval_s` | `3600` | At most one "downloads are blocked" notification per this many seconds. (M08) |
| `vod.source_health` | `null` | Written by code: platforms that refuse video downloads from this machine (since, last refusal, count, error). Discovery queues no new videos of a platform listed here; cleared when a download works. (fix-blocked-platform-queue) |
| `vod.stop_retry_s` | `600` | Seconds a video waits while control.emergency_stop is set. (M08) |
| `vod.fallback_enabled` | `true` | Use the transcript fallback for videos without a usable chat replay. (M08) |
| `vod.fallback_below_spikes` | `3` | The transcript fallback also runs when the chat replay gave fewer spikes than this. (M08; 1 until migration 0010) |
| `vod.fallback_max_duration_s` | `43200` | Transcript fallback: videos longer than this are skipped. (M08) |
| `vod.fallback_min_confidence` | `0.5` | Transcript fallback: moments below this confidence are stored as rejected. (M08) |
| `vod.fallback_chunk_s` | `30` | Transcript fallback: seconds of transcript per block shown to the moment picker. (M08) |
| `vod.fallback_max_transcript_s` | `21600` | Transcript fallback: seconds of transcript the moment picker reads per video at most (0 = all). One request per 30 minutes. (M08) |
| `vod.fallback_transcribe_mode` | `"local"` | Transcript fallback without a caption track: local (faster-whisper, no fee) or hosted (transcribe.express_provider, paid). (M08) |
| `vod.fallback_transcribe_max_s` | `5400` | Transcript fallback without a caption track: seconds of audio transcribed per video at most (0 = never transcribe). (M08) |
| `vod.fallback_transcribe_daily_s` | `21600` | Transcript fallback without a caption track: seconds of audio transcribed per UTC day at most. (M08) |
| `vod.fallback_transcribe_block_s` | `600` | Transcript fallback: seconds of audio per transcription request. (M08) |
| `vod.fallback_whisper_model` | `null` | Local model for the fallback transcription; null uses vod.whisper_model. (M08) |
| `vod.section_retention_s` | `93600` | Downloaded windows whose render never finished are deleted after this many seconds. (M08) |
| `vod.chat_retention_s` | `604800` | Chat replay files are deleted this many seconds after the download. (M08) |
| `vod.cleanup_interval_s` | `900` | Seconds between clean-up passes over one video's downloaded windows. (M08) |
| `vod.orphan_grace_s` | `300` | A video in status processing with no running vod.process or replay.scan job stops counting as in work, and is put back to new, once it was not updated for this many seconds (covers runs started by hand with the CLI, which have no job). (fix-stale-processing) |
| `vod.recovery_interval_s` | `60` | Seconds between scheduler passes that put orphaned processing videos back to new. (fix-stale-processing) |
| `vod.concurrency_retry_max_s` | `600` | Longest wait of a vod.process job that keeps finding no free slot: the wait doubles from vod.concurrency_retry_s up to this. (fix-stale-processing) |
| `vod.concurrency_retry_jitter` | `0.2` | Share (0..1) by which the wait for a free slot is randomly shortened, so deferred jobs do not all wake at once. (fix-stale-processing) |
| `vod.worker_fresh_s` | `90` | A worker of the normal queue counts as live for the VOD concurrency limit when its heartbeat (written every 30 s) is not older than this many seconds. (fix-stale-processing) |

## Proxy (YouTube video downloads)

Residential proxy (DataImpulse) for the VOD lane only, added 2026-11-18 (migration 0025, `src/sixstars/vod/proxy.py`). Environment: `PROXY_URL`, `PROXY_STICKY_PORT_MIN` / `PROXY_STICKY_PORT_MAX` (one random port per video job; each port is its own sticky exit IP), `POT_PROVIDER_URL` (the `bgutil` compose service).

| Key | Default | Meaning |
|---|---|---|
| `proxy.lanes` | `["vod"]` | Lanes that may use `PROXY_URL`: `vod`, `live`, `discovery`, `chat`. Live recording costs about 2.5 GB per stream-hour. |
| `proxy.max_attempts` | `3` | Attempts of one YouTube video job through the proxy, each on a new port, after a bot check, a 403 on every window or a proxy error. A 401/402/407 from the proxy account is not retried. |
| `proxy.cookies_mode` | `"fallback"` | `fallback`: one more attempt with `YOUTUBE_COOKIES_FILE` after the attempts without; `first`: every attempt with cookies; `never`. |
| `proxy.daily_budget_usd` | `3` | Proxy spend per UTC day; when reached, YouTube video jobs wait for the next day (Twitch goes on) and the guardian alerts. 0 = no limit. |
| `proxy.usd_per_gb` | `1.0` | Price per GB, for `api_usage.cost_usd`. |
| `proxy.overhead_mb_per_call` | `2.0` | Estimated MB per yt-dlp call on top of the measured media bytes. |
| `proxy.cycle_gb` | `5` | GB bought this billing cycle; after a top-up set the new total and `proxy.cycle_started_at`. |
| `proxy.cycle_started_at` | `"2026-11-18T00:00:00+00:00"` | Start of the billing cycle. |
| `proxy.low_traffic_share` | `0.8` | Share of `proxy.cycle_gb` (our own count) at which the guardian asks for a top-up. |
| `proxy.health_interval_s` | `900` | Seconds between two guardian probes of the proxy. |
| `proxy.health` | written by code | Last probe: `{ok, reason, checked_at}`. |
| `proxy.health_checked_at` | written by code | Time of the last probe. |

## Clip factory

| Key | Default | Meaning |
|---|---|---|
| `factory.width` | `1080` | Output width. |
| `factory.height` | `1920` | Output height. |
| `factory.fps` | `30` | Output fps. |
| `factory.crf` | `21` | x264 CRF for normal renders. |
| `factory.preset_normal` | `"veryfast"` | x264 preset for normal renders. |
| `factory.preset_express` | `"superfast"` | x264 preset for express renders. |
| `factory.use_nvenc` | `false` | Use NVIDIA encoder if a GPU is present. |
| `factory.loudness_lufs` | `-12` | Target loudness (was `-14`; migration 0021, with the compressor of `factory.style`). |
| `factory.safe_top_px` | `220` | No text above this. |
| `factory.safe_bottom_px` | `420` | No text in the bottom N px. |
| `factory.accent_color` | `"#FF2BD6"` | Accent colour (keywords, progress bar). Magenta since migration 0021 (was `"#FFD400"`). |
| `factory.font` | `"Montserrat Black"` | Subtitle font. |
| `factory.hook_seconds` | `3.0` | How long the hook title shows. |
| `factory.end_card_s` | `0` | End card length; `0` is no end card, so the clip ends on the payoff and loops (migration 0021, was `1.5`). The gate reads it for the expected file length. |
| `factory.watermark` | `"6 Stars Clips"` | Watermark text. The brand name, not a handle: one file goes to all platforms (D111). |
| `factory.series_label` | `"6 Stars Moments"` | Series label shown on the end card. |
| `factory.bleep` | `true` | Bleep profanity in audio and mask it in subtitles. |
| `factory.music_duck_db` | `-18` | Gain applied to segments flagged as music (hourly clips under the music limit). |
| `factory.cut_snap` | `{"pause_s": 0.4, "start_back_s": 4.0, "start_forward_s": 2.0, "end_forward_s": 3.0, "end_back_s": 2.0, "vad": true, "punctuate_below": 0.02}` | Snap the cut to a pause, so a clip does not open or close mid-sentence. A gap between words of `pause_s` seconds or more, or a sentence-ending mark, is a phrase boundary. The clip start moves back to the nearest one within `start_back_s`, else forward within `start_forward_s` (never past the spike floor); the end moves forward within `end_forward_s`, else back within `end_back_s`. A sentence end is preferred to a bare pause; the clip length limits always hold. `vad`: find the pauses Whisper hides inside stretched words with voice activity detection. `punctuate_below`: when a smaller share of the transcript's words than this ends a sentence (Whisper left the punctuation out), the fast model is asked where the sentences end (prompt P10, $0.0002 to $0.001 per window); `0` switches that off. `pause_s` `0` switches the whole rule off. Added by migration 0011. |
| `factory.style` | `{"caption_words": 2, "caption_chars": 14, "caption_font": "Anton", "caption_size": 150, "caption_caps": true, "hook_mode": "title_bar", "sfx_gain_db": -10, "payoff_fx": true, "compress": true}` | The editing style (docs/27). `caption_words` / `caption_chars`: most words and characters on screen at once (`0` words: whole lines with the spoken word highlighted, the classic look). `caption_font` / `caption_size` / `caption_caps`: caption font (empty: `factory.font`), size on the 1080x1920 canvas, capitals. `hook_mode`: `box` draws the hook as a subtitle box, `title_bar` as a TikTok-style bar; both show it for the first `factory.hook_seconds`. `sfx_gain_db`: level of every sound effect. The zoom and punch-in rules (`punchin_max`, `punchin_zoom`, `punchin_len_s`, `punchin_min_gap_s`, `punchin_db`, `zoomcut_every_s`, `zoomcut_scale`, `cut_sfx`) were removed by Render v2 R0 (D119, migration 0043, 2026-11-23); a value that still carries them is read without them (logged once). `payoff_fx`: emoji and bass hit at the clip writer's `payoff_s`. `compress`: compressor before loudness normalisation. A missing or invalid value gives the classic look. Added by migration 0021. Since migration 0027 only the fallback of `factory.styles`. |
| `factory.styles` | `{"cam_top_title": {...}, "cam_top_viral": {...}}` (migration 0027) | The catalogue of design styles (development-plan/M18, docs/27 section 5). Per style: `enabled`, `weight` (share in the weighted pick), `lanes` (`spike`, `hourly`), `accounts` (`"*"` or a list of `platform_accounts.id`) and `rules`: the `factory.style` keys plus `facecam_top` (facecam on top, layout C), `captions` (`false`: no spoken captions) and `title_whole_clip` (the title bar stays for the whole clip), `frame` (`auto`, `headline_card`, `pov`), `title_source` (`hook`, `title_line`, `headline`, `question`, `pov_line`), `title_look` (`bar`, `question`) and `watermark`; optional `requires` (`facecam`, `title_line`, `headline`, `question`, `pov_line`): the style is used only for a clip that has them. Migration 0028 adds `cam_top_question`, `headline_card` and `pov_fullframe`; 0029 adds `cam_top_wordcap` and the rule keys `caption_y` (caption centre, 0: the layout's), `caption_active_color` (the spoken word's colour) and `show_title`; 0031 adds `hype_montage` (hourly, requires `facecam` and `broll`) and the rule keys `frame: montage`, `montage_every_s` (B-roll cut length and spacing) and `caption_tiers` (two-tier keyword captions). Styles are taken in id order; a malformed entry is left out; without a usable entry the single `factory.style` is used. |
| `factory.style_director` | `{"no_repeat_n": 1, "diverse_across_accounts": true}` | How each platform account's style is picked (`factory/director.py`). `no_repeat_n`: skip the styles of the account's last n posts when another is allowed. `diverse_across_accounts`: give the accounts of one clip different styles when possible. `affinity` (migration 0028): per clip-writer `moment_type`, a weight multiplier per style (missing: 1), e.g. `{"discovery": {"headline_card": 3}}`. Phase D (migration 0030): `mode` (`weighted` or `bandit`: learn from post views per account, docs/27 section 6), `explore_floor` (0.1), `min_samples` (20), `affinity_weight` (0.3), `prior_n` (2), `min_age_h` (48), `window_d` (14). Migration 0027. |
| `factory.layout_v2` | `{"enabled": false, "cam_h": 720, "g0_game_h": 1350, "g0_game_y": 285, "max_slide": 0.25, "seam_y": 720, "safe_right_x": 920, "safe_bottom_y": 1540, "cam_face_center_y": 0.45, "cam_face_h_min": 0.40, "cam_face_h_max": 0.60, "cam_fill_blur": 30, "cam_fill_darken": 0.30, "cam_unsharp": 0.5, "scaler": "lanczos"}` | Render v2 frames (D119, development-plan/M19). `enabled`: the master switch; on, every clip is drawn as layout **G** (a facecam: cam panel 1080x`cam_h` on top, gameplay 1080x(1920-`cam_h`) below, no seam, no progress bar) or **G0** (no facecam: `g0_game_h` of 4:5 gameplay at `g0_game_y` over a blurred, darkened copy) and the style only changes the text; off, the frames of `factory.styles`. `max_slide`: the gameplay crop starts centred and slides sideways (at most this share of the source width) to the nearest position clear of the facecam and the streamer's overlays, else to the least overlap. `seam_y`, `safe_right_x`, `safe_bottom_y`: text places (R4). `cam_face_center_y`, `cam_face_h_min`, `cam_face_h_max`: face centre and height share in the cam panel; `cam_fill_blur`, `cam_fill_darken`: the blurred fill around a small cam; `cam_unsharp`: sharpening of the cam (R2). `scaler`: ffmpeg scaler of every crop (`bicubic`, `bilinear`, `lanczos`, `spline`, `area`), used with `enabled` off too (R0). Pixels on the 1080x1920 canvas. A missing or invalid value gives these defaults with `enabled` off and the scaler `bicubic`. Migration 0054. |
| `factory.text_v2` | `{"font": "Anton", "size_min": 110, "size_max": 130, "outline_px": 9, "color": "#FFFFFF", "key_color": "#FFD400", "max_words": 3, "hook_s": 2.5, "mark_text": "6 Stars Clips", "mark_size": 34, "mark_opacity": 0.7, "mark_x_right": 1040, "mark_y": 740, "credit_size": 30, "credit_x": 40, "credit_y": 740}` | Render v2 text (D119, read from R4 on): one text element at a time on the seam. The hook for `hook_s`, then captions of 1 to `max_words` words in `font` between `size_min` and `size_max` px, `color` with a `outline_px` black outline, the active or key word in `key_color`. `mark_*`: the brand mark, right-aligned at `mark_x_right`, top-right of the gameplay; `credit_*`: the creator credit, top-left of the gameplay. Migration 0054. |
| `facecam.detect` | `{"min_confidence": 0.75, "check_frames": 5, "min_face_hits": 3, "min_face_share": 0.25, "detector_score": 0.6, "cache_scope": "source", "creator_prior_days": 7}` | Render v2 facecam verification (D119, read from R3 on): the layout prompt's cam box is used only with confidence ≥ `min_confidence` and a face (detector score ≥ `detector_score`) in ≥ `min_face_hits` of `check_frames` frames, its median height ≥ `min_face_share` of the box; else layout G0. `cache_scope` `source`: one cached layout per source; `creator_prior_days`: how long the creator's last accepted box is tried first. Migration 0054. |
| `factory.pre_p2_checks` | `false` | AI cost plan A5a (D117): before the clip writer (P2), reject a clip the gate is certain to reject: the creator's daily cap or tier, or music in every possible cut of the window over the profile limit (`gate.hourly` / `gate.express`, `gate.music_min_run_frames`). Uses the gate's own check functions and settings; reason `pre_p2: ...`, `scoring_log` stage `factory.pre_p2`. Migration 0038. |
| `factory.pre_p2_throttle` | `"log"` | AI cost plan A5b (D117): queue throttle before the clip writer. `off`; `log` writes a `scoring_log` row (stage `factory.pre_p2_throttle`, decision `would_skip`/`keep`) and changes nothing; `enforce` rejects a would-skip (`pre_p2_throttle: ...`). Would skip: hourly lane, the effective queue (approved hourly clips the busiest hourly account reaches before `posting.max_clip_age_h`) holds at least `factory.pre_p2_queue_min` clips, and the candidate's chat percentile x spike bonus x freshness is below the queue median. Tier S and clips without a percentile are never throttled. Migration 0039. |
| `factory.pre_p2_queue_min` | `40` | The effective queue size from which the throttle compares (above). Migration 0039. |
| `factory.story` | `{"enabled": true, "min_s": 60, "max_s": 150, "pad_before_s": 90, "pad_after_s": 30, "sources": ["vod", "live"]}` | Story cuts (development-plan/M18 Phase C, docs/09 section 12): the writer may propose a longer cut of `min_s`..`max_s` seconds for hourly, normal-mode renders of the listed sources, whose source window is widened by `pad_before_s` / `pad_after_s`. `enabled` false or a missing setting: no story cuts and the old windows. Migration 0029. |

## AI

| Key | Default | Meaning |
|---|---|---|
| `ai.model_writer` | `"claude-sonnet-5-5"` | Model for clip writing, moment picking, vision layout. |
| `ai.model_fast` | `"claude-haiku-5-5"` | Model for spike confirmation and classifiers. |
| `ai.daily_budget_usd` | `25` | Daily Anthropic spend cap. Over it: VOD lane pauses, express continues. |
| `ai.score_samples` | `1` | How many times a clip is scored. `1`: the clip writer's own score is stored. `3` (or another odd number): the fast model scores the finished cut that many times with the same rubric (prompt P9) and the median is stored; about $0.001 more per clip. Added by migration 0011. |
| `ai.visual_score` | `true` | Score the rendered clip from its frames before the gate decides (prompt P9V, `gate/rescore.py`); `clips.score` becomes that score and `clips.score_parts` keeps the writer's and the visual parts under `writer` and `visual`. `false`: the clip writer's score is judged, as before. If the call fails the writer's score stands. Added by migration 0016. |
| `ai.visual_score_model` | `"writer"` | Which model gives the visual score: `"writer"` (`ai.model_writer`) or `"fast"` (`ai.model_fast`). Measured on 15 staging clips: the writer model kept every weak clip at 5 or less (about $0.011 per request); the fast model is 18 times cheaper but put weak clips at 6 and one at 7 in 2 of 9 asks. (migration 0016) |
| `ai.visual_score_frames` | `8` | Frames the visual score sees, 4 to 8: the four P5 frames plus extra ones around the chat peak and near the end of the clip. One extraction serves P9V and P5. With 4 the good clips scored lower. (migration 0016) |
| `ai.visual_score_samples` | `3` | How many times an hourly clip is scored visually; the median is stored. Use an odd number. Express clips are always scored once. (migration 0016) |
| `ai.model.<prompt_id>` | *(not set)* | AI cost plan A3: the model one prompt runs on, as `"anthropic:<model id>"` (a bare id means Anthropic), e.g. `ai.model.p3_vod_moment_picker`. Not set: the prompt's role model (`ai.model_writer` for P2/P3/P4/P8, `ai.visual_score_model` for P9V, else `ai.model_fast`). A provider the app cannot call is ignored with a warning. |
| `ai.shadow.<prompt_id>` | `["anthropic:claude-haiku-5-5"]` for `p2_clip_writer` and `p9_visual_score` | AI cost plan B1: models asked the same prompt in shadow after a sampled production call; the answers go to `ai_shadow` and never to production (`ai/shadow.py`, job `ai.shadow`). Migration 0041. |
| `ai.shadow_rate` | `0.2` | Share of hourly/VOD P2 and P9V calls that get a shadow run; express calls never do. `0` turns shadow runs off. Migration 0041. |
| `ai.shadow_daily_budget_usd` | `2` | Daily cap on shadow requests, recorded in `api_usage` under provider `anthropic_shadow`, which `ai.daily_budget_usd` does not count. Migration 0041. |
| `ai.visual_score_adaptive` | `true` | AI cost plan A2: ask the further `ai.visual_score_samples` only when the first P9V score is from `ai.visual_score_margin` below the hourly pass line up to the line; the frames are cached for the re-asks. Migration 0042. |
| `ai.visual_score_margin` | `1` | See `ai.visual_score_adaptive`. Migration 0042. |
| `transcribe.express_provider` | `"openai"` | Hosted transcription for express: groq | openai. |
| `transcribe.daily_budget_usd` | `5` | Daily hosted transcription cap. |

## Quality gate

| Key | Default | Meaning |
|---|---|---|
| `gate.hourly` | `{"min_len": 20, "max_len": 60, "max_music_ratio": 0.1, "min_score": 7, "topic_dedupe_h": 48, "precheck_risky": true}` | Quality gate profile for hourly clips. |
| `gate.hourly_len_by_platform` | `{"youtube": {"min_len": 8, "max_len": 30}}` | Hourly clip length per platform (end card included), replacing `gate.hourly` `min_len`/`max_len` for that platform. The gate and the clip writer allow the union (the shortest minimum, the longest maximum); the posting engine sends a short hourly clip (gate profile `hourly`) only to the platforms whose window fits it, the others using `gate.hourly` lowered like the gate lowers it (`cut.adaptive` `min_s`). Spike clips and story cuts are not affected. Migration 0036. Absent: `gate.hourly` everywhere. |
| `gate.express` | `{"min_len": 15, "max_len": 45, "max_music_ratio": 0.0, "min_score": 8, "topic_dedupe_h": 6, "tiers": ["preferred", "auto_no_music"]}` | Quality gate profile for express clips. |
| `gate.spoiler_block_until` | `"2026-12-03T00:00:00Z"` | Story-spoiler clips rejected until this time. Titles/hooks never contain spoilers. |
| `gate.reject_topics` | `["real-world politics", "gambling", "sponsored segment", "real-world violence", "sexual content", "personal information"]` | Always rejected. |
| `gate.auto_creator_daily_cap` | `4` | Max posted clips per day per auto-tier creator. |
| `precheck.enabled` | `true` | Run YouTube private-upload pre-check for risky hourly clips. |
| `precheck.wait_s` | `1800` | Wait before reading pre-check status. |
| `precheck.daily_max` | `20` | Max pre-check uploads per day. |
| `gate.spoiler_terms` | `["spoiler", "spoilers", "ending", "endings", "true ending", "secret ending", "final mission", "last mission", "final boss", "final heist", "final choice", "final cutscene", "epilogue", "post credits", "credits scene", "plot twist", "story twist", "character death", "main character dies", "who dies", "death scene", "killed off", "betrayal", "betrays"]` | Words and phrases that may never appear in a hook, title, caption or hashtag (whole words, any case). Checked by the gate whatever the model said (D20). Added by migration 0007. |
| `gate.strict_max_music_ratio` | `0.0` | Music limit of a risky hourly clip when the YouTube pre-check cannot run (no OAuth token, switched off, daily quota used; D103). Never above `gate.hourly.max_music_ratio`. Added by migration 0007. |
| `gate.music_min_run_frames` | `2` | Music frames in a run shorter than this many seconds are ignored by the gate. `1` counts every flagged frame; `2` ignores an isolated flagged second, which is how the detector mishears speech over a game sound (3 of 6 runs in M06, 3 of 16 real renders in the hardening check). The zero-music limits protect against copyrighted music long enough to be matched, and one second is below that; the cost is that one real second of music could pass. Added by migration 0007 as `1`, set to `2` by migration 0011. |
| `gate.checks` | `{"max_black_s": 2.0, "max_freeze_s": 2.0, "freeze_noise": 0.003, "band_freeze_noise": 0.001, "band_black_pixel": 0.03, "max_subtitle_drift_s": 0.3, "max_file_mb": 100, "width": 1080, "height": 1920, "duration_tolerance_s": 1.0}` | Technical gate limits: a black or frozen picture of this length or more fails (`freeze_noise` is the freezedetect tolerance on the whole frame as in docs/10; `band_freeze_noise` and `band_black_pixel` are the tighter tolerances inside the gameplay band, where our moving overlays cannot hide a frozen or black source), subtitle drift, file size (MB of 10^6 bytes) and frame size, allowed difference between the file length and the clip length plus end card. Added by migration 0007. |
| `gate.duplicate` | `{"source_pad_s": 30, "topic_overlap_same_creator": 0.5, "topic_overlap_any_creator": 0.8, "transcript_overlap": 0.5}` | Duplicate detection: padding around a source range; shared topic words (same creator: share of the shorter topic, any creator: Jaccard); shared three-word sequences of the transcripts. `0` switches a rule off. Added by migration 0007. |
| `precheck.max_status_checks` | `3` | How many times the pre-check status is read while YouTube is still processing before the clip is rejected as inconclusive. Added by migration 0007. |
| `gate.render_v2` | `{"face_frames": 8, "face_hit_share": 0.8, "face_min_panel_share": 0.30, "min_game_share_g": 0.60, "min_game_share_g0": 0.70, "max_black_band": 0.05}` | Render v2 checks (D119, read from R5 on): a face in ≥ `face_hit_share` of `face_frames` frames of the cam panel, never within 4 px of its edge, median height ≥ `face_min_panel_share` of the panel; gameplay ≥ `min_game_share_g` (G) or `min_game_share_g0` (G0) of the frame; no black band taller than `max_black_band` of the frame. A failure re-renders the clip once as G0. Migration 0054. |

## Posting

| Key | Default | Meaning |
|---|---|---|
| `posting.jitter_max_s` | `300` | Hourly slot random offset 0..N seconds. |
| `posting.min_gap_s` | `900` | Minimum seconds between posts on one account. The hourly lane can add its own longer gap per account: column `platform_accounts.hourly_min_gap_s` (counted from the last hourly post only; `youtube_main` 3000 since migration 0034). |
| `posting.spike_max_per_hour` | `2` | Max spike posts per account per rolling hour. |
| `posting.skip_hourly_after_spike_s` | `1800` | Skip an hourly slot if a spike posted within this many seconds before it. |
| `posting.spike_max_wait_s` | `900` | A spike may wait this long for the min gap; after that it is demoted. |
| `posting.spike_max_age_s` | `1800` | Spike clips older than this lose spike status. |
| `posting.spike_hold_s` | `420` | Holding window (R12, docs/11): the spike lane waits this long after the first waiting spike clip was approved, then posts the best-ranked clip of the batch first; never later than 2 min before a clip reaches `posting.spike_max_age_s`. `0` or absent: each spike clip posts as soon as it is ready. Added by migration 0023. |
| `posting.freshness_half_life_h` | `{"live": 6, "vod": 24}` | Freshness decay half-life by source. |
| `posting.max_clip_age_h` | `72` | Approved clips older than this expire. |
| `posting.rank_uses_visual_score` | `false` | AI cost plan A2: the posting rank starts from the clip writer's score instead of the P9V score (which did not predict views); the gate still decides on P9V. Migration 0042. |
| `posting.sibling_gap_h` | `12` | Hours between two clips of one family (a short clip and its story cut) on the same account (M18 Phase C, migration 0029). Absent: 12. |
| `posting.cross_platform_min_gap_s` | `2700` | D116, hourly lane only: an hourly post does not take a clip another platform posted less than this many seconds ago (planned posts of the same slot count); the account takes its next best clip. `0`: off, every platform gets the top clip (D18). Migration 0035. Absent: 0. |
| `posting.platform_priority` | `["youtube", "tiktok", "instagram"]` | D116: the order in which platforms pick their clip in an hourly slot; unlisted platforms last, then by account id. Missing or malformed: account id order. Migration 0035. |
| `posting.max_duration_s` | `{"tiktok": 600, "instagram": 900, "youtube": 180}` | The longest clip in seconds each platform is given (M18 Phase C, migration 0029). Absent: no limit. |
| `posting.queue_low_watermark` | `12` | Below this many approved hourly clips, guardian widens discovery. |
| `posting.first_comment` | `"Would you have survived this? 👇 Follow {handle} for daily GTA 6 chaos"` | First comment text. `{handle}` becomes the posting account's handle (D111). A question invites replies (migration 0034; was "Follow {handle} for the best GTA 6 moments every hour 🎮⭐"). |
| `posting.base_hashtags` | `["#GTA6", "#GTAVI", "#GTA6Clips"]` | Hashtags added to every caption, on every platform. Migration 0034 dropped `#Gaming`. |
| `posting.youtube_extra_hashtags` | `["#Shorts"]` | Extra hashtags for YouTube. |
| `posting.platform_max_24h` | `{"tiktok": 15, "instagram": 50, "youtube": 30}` | Hard ceiling of posts per account per rolling 24 h set by Upload-Post. An account cap above it is ignored. |
| `posting.staging_allow_instagram` | `false` | Staging only: allow a real Instagram post (Instagram cannot post privately). Set true only for the single M15 test post. |

## Guardian

| Key | Default | Meaning |
|---|---|---|
| `guardian.check_interval_s` | `900` | How often claims/removals are checked (launch week). |
| `guardian.check_lookback_h` | `72` | Posts younger than this are checked. |
| `guardian.strike_limit` | `2` | Strikes/removals in the window that trigger emergency stop. |
| `guardian.strike_window_d` | `7` | Window for the strike limit. |
| `guardian.heartbeat_stale_s` | `300` | A service is unhealthy if its heartbeat is older than this. |
| `guardian.stuck_job_s` | `7200` | Running jobs older than this are reset. |
| `guardian.express_slow_s` | `360` | Alert if express spike-to-post exceeds this 3 times in a row. |
| `guardian.failover_grace_s` | `{"hourly": 4200, "discovery": 1500, "guardian": 2700, "stats": 14400}` | If an n8n trigger has not arrived for this long, the scheduler runs it itself. The guardian and stats keys were added in M13 (migration 0009). |
| `guardian.failover_interval_s` | `{"discovery": 600, "guardian": 900, "stats": 7200}` | While an n8n trigger is missing, the scheduler runs that job itself this often. The hourly slot is run once per slot. (M13) |
| `guardian.failover_enabled` | `true` | false: the scheduler only alerts about a missing n8n trigger and does not run the job itself. (M13) |
| `guardian.check_min_age_s` | `900` | Claims check: a post younger than this is not checked yet. (M13) |
| `guardian.check_max_posts_per_run` | `80` | Claims check: posts read per pass at most; the rest wait for the next pass. (M13) |
| `guardian.check_budget_s` | `20` | Claims check: seconds one pass may spend reading posts (the n8n request times out after 30 s). (M13) |
| `guardian.check_private_posts` | `false` | Claims check without the owner's YouTube OAuth token: also judge posts on accounts that post privately (staging), through Upload-Post. Off: they are counted as unverifiable. (M13) |
| `guardian.mass_missing_min` | `5` | Claims check: when at least this many posts of one platform are gone in one pass... (M13) |
| `guardian.mass_missing_ratio` | `0.8` | ...and they are at least this share of the posts read on that platform, it is an account-level event: emergency stop, no creator is blocked. (M13) |
| `guardian.takedown_body_max_chars` | `20000` | Takedown intake: characters of an email body that are kept; links are still read from the whole body. (M13) |
| `guardian.takedown_max_per_hour` | `60` | Takedown intake: emails classified per hour at most. Over it an email is stored as open without classification and the owner is warned. (M13) |
| `guardian.handle_match_min_chars` | `4` | Takedown intake: a creator name from an email is matched only when it has at least this many letters or digits. (M13) |
| `guardian.expected_services` | `["scheduler", "worker-express", "worker-normal", "live-manager"]` | Services whose heartbeat the health check expects. A worker counts as alive when any process of its queue is. (M13) |
| `guardian.restart_storm_limit` | `3` | Restarts of one service within `guardian.restart_storm_window_s` that raise a critical alert. (M13) |
| `guardian.restart_storm_window_s` | `3600` | Window for `guardian.restart_storm_limit`. (M13) |
| `guardian.express_slow_count` | `3` | Spike posts in a row slower than `guardian.express_slow_s` that raise a warning. (M13) |
| `guardian.disk_warn_pct` | `80` | Warn when the data directory is fuller than this percentage. (M13) |
| `guardian.spend_pause_s` | `3600` | When a provider is over its daily budget the VOD lane is paused for this long at a time (never past 00:00 UTC). (M13) |

## Cleanup

| Key | Default | Meaning |
|---|---|---|
| `cleanup.posted_clip_days` | `3` | Delete clip files this many days after posting. |
| `cleanup.rejected_clip_hours` | `24` | Delete rejected clip files after this many hours. |
| `cleanup.events_days` | `30` | Events older than this many days are deleted by the scheduler. (M13) |
| `cleanup.candidates_days` | `30` | Discovery candidates older than this many days are deleted by the scheduler. (M13) |

## Reports

| Key | Default | Meaning |
|---|---|---|
| `reports.ypp_targets` | `{"subscribers": 1000, "shorts_views_90d": 10000000}` | YouTube Partner Program targets shown in reports. |
| `reports.top_clips` | `3` | Daily report: number of top clips listed. (M13) |
| `stats.post_lookback_d` | `7` | Stats: posts published in the last this many days get their numbers refreshed. (M13) |
| `stats.max_posts_per_run` | `150` | Stats: posts refreshed per pass at most (oldest numbers first). (M13) |
| `stats.collect_budget_s` | `20` | Stats: seconds one pass may spend on Upload-Post calls. (M13) |
| `stats.account_min_interval_s` | `1800` | Stats: an account gets at most one snapshot per this many seconds, so a repeated trigger stores nothing twice. (M13) |
| `launch.summary_lead_s` | `900` | The scheduler sends one health summary this many seconds before `launch.launch_at`. (M13) |

## Admin console

| Key | Default | Meaning |
|---|---|---|
| `admin.statement_timeout_ms` | `5000` | Statement timeout of one admin console read (`GET /v1/admin/*`); a slower query answers 504. (M17) |
| `admin.list_max_limit` | `200` | Largest page an admin console list returns; the default page is 50. (M17) |
| `admin.month_ceiling_usd` | `840` | Monthly spend ceiling shown on the Spend page (top of the docs/23 cost model). Display only. (M17) |
| `admin.noisy_event_types` | `["post.dry_run", "post.link_updated", "stats.collected", "clip.scored", "job.recovered"]` | Event types left out of the overview's recent events. (M17) |

## Console media

Clip thumbnails and creator profile pictures for the admin console (docs/26, migration 0022).

| Key | Default | Meaning |
|---|---|---|
| `console.thumb_frame_s` | `1.5` | The clip thumbnail is the frame this many seconds into the rendered clip (the middle of a shorter clip). |
| `console.thumb_width_px` | `540` | Width of the clip thumbnail JPEG; the height keeps the 9:16 shape. |
| `console.thumb_jpeg_qscale` | `5` | ffmpeg JPEG quality of the thumbnail (`-q:v`, 2 best to 31 worst; 5 is about JPEG quality 80). |
| `console.avatar_refresh_days` | `7` | A creator's profile picture is looked up again after this many days. |
| `console.avatars_per_pass` | `100` | Creators whose picture is looked up per discovery pass, at most (Twitch: 100 per request; YouTube: 50 per request, 1 quota unit each, inside `discovery.youtube_quota_daily_units`). |

## Clipping campaigns (lanes)

Campaign lanes, decision D118 (migration 0050). The campaigns themselves live in `clip_campaigns`, not in settings.

| Key | Default | Meaning |
|---|---|---|
| `lanes.campaign_share` | `{"youtube_main": 6, "instagram_main": 18, "tiktok_main": 6}` | Posts a day per account that all campaigns together may take, inside `cap_24h`. The Normal lane gets the rest (`lane_quotas` row `normal`), which also holds the spike reserve. A save whose campaign quotas add up to more is refused. |
| `lanes.campaign_max_per_day` | `{"youtube": 3, "instagram": 10, "tiktok": 3}` | Most posts one campaign may get per platform and day; a quota above it is refused. |
| `lanes.campaign_hours_utc` | `{"default": [8..21], "youtube": [8..17]}` | UTC hours for campaign posts: 10:00–24:00 Cairo, YouTube 10:00–20:00. A spike clip made outside them is a Normal (`brand`) clip; hourly-lane campaign clips are held for these hours by posting (PR 4). |
| `live.campaign_slots` | `4` | Live slots (of `live.max_slots`) that live campaign streams may take. When the lane is full, the weakest trending session is released for one. Unused slots stay with trending streams. |
| `campaigns.watch_interval_s` | `60` | Seconds between campaign watcher passes (scheduler service). Each pass makes one Twitch Get Streams call by user id for up to 100 streamers. |
| `campaigns.watch_youtube_s` | `180` | Seconds between reads of a YouTube campaign streamer's channel feed (free). New or live videos cost 1 quota unit per 50. |
| `campaigns.feed_lookback_h` | `48` | YouTube feed entries younger than this are checked for live or finished streams. |
| `campaigns.vod_priority` | `35` | Job priority of a campaign VOD and of a campaign session's replay (normal VODs 30, replays 25). |
| `campaigns.watcher_stale_s` | `300` | The guardian alerts when the `campaign:watcher` heartbeat is older than this while a campaign is active. |
| `twitch_clips.campaign_first` | `true` | Viewer clips of campaign streamers are read per broadcaster and come before the game-wide list, inside `twitch_clips.daily_cap`. |
| `lanes.return_unused_at_utc` | `18` | From this UTC hour (20:00 Cairo) unused campaign quota goes back to the Normal lane for the rest of the day. Before it, Normal posts leave room for the campaign minimums still open. |
| `lanes.campaign_gap_s` | `3600` | Seconds between two posts of the same campaign on one account. |

## Long-form episodes

One daily 1920×1080 "TOP N" YouTube episode built from moments the engine already clipped (D122, development-plan/M20, docs/31). Migration 0058.

| Key | Default | Meaning |
|---|---|---|
| `longform.enabled` | `false` | Master switch (D122, development-plan/M20). Off: no episode is planned, built or published. Staging: `true`. Migration 0058. |
| `longform.publish_mode` | `"off"` | `off` = build only (console preview), `private` = upload as a private YouTube video, `public` = publish. Staging is always private. Production starts `private`; `public` needs the owner's go. |
| `longform.build_hour_utc` | `10` | UTC hour the daily episode is planned and built. |
| `longform.publish_hour_utc` | `16` | UTC hour the ready episode is uploaded. Keep it outside `youtube_main.hourly_hours_utc`. |
| `longform.pool_hours` | `48` | Clips created within this many hours are candidates. Staging: `72`. |
| `longform.pool_statuses` | `["approved","posted","expired"]` | Clip statuses that are candidates (passed the quality gate). |
| `longform.pool_reject_reasons` | `["creator_daily_cap","duplicate_topic","unreadable_subtitles","creator_tier_express"]` | Rejected clips with one of these reasons are candidates too: they were refused by Shorts-only rules. |
| `longform.min_score` | `7` | Minimum clip score (Claude, 1–10). Staging: `6`. |
| `longform.target_s` | `720` | Target length of the moments together, in seconds. |
| `longform.min_s` | `600` | Below this the day is skipped (notify), never padded. |
| `longform.max_s` | `870` | Hard maximum (14:30), under YouTube's 15 min default. |
| `longform.segment_min_s` | `15` | Shortest moment, in seconds. |
| `longform.segment_max_s` | `45` | Longest moment, in seconds. |
| `longform.lead_s` | `3` | Seconds kept before the build-up starts. |
| `longform.tail_s` | `2` | Seconds kept after the payoff pause. |
| `longform.max_per_creator` | `3` | Most moments of one creator per episode. |
| `longform.max_creator_s` | `90` | Most seconds of one creator per episode. |
| `longform.reuse_after_days` | `7` | A clip can be in a new episode again after this many days. |
| `longform.fetch_pad_s` | `5` | Extra seconds downloaded on each side of a moment, for alignment. |
| `longform.whisper_model` | `"medium"` | Local faster-whisper model for alignment and subtitles (no hosted cost). Must be baked into the image (`docker/Dockerfile` `WHISPER_MODEL`); the workers run offline (0060). |
| `longform.render` | `{"width": 1920, "height": 1080, "fps": 30, "crf": 20, "maxrate": "8M"}` | Segment encode: full frame, scale + pad, no crop. Every segment shares it so they join without re-encoding. |
| `longform.priority` | `15` | Job priority on the normal queue (below hourly 110, spikes 90, VODs 30). |
| `longform.keep_days` | `7` | Local and Storage episode files are deleted after this many days. |
| `longform.cold_open_s` | `5` | Seconds of #1 shown before the countdown. |
| `longform.end_card_s` | `10` | End card length (room for YouTube end-screen elements). |
| `longform.rank_weights` | `{"score": 1.0, "spike_z": 1.0, "spike_z_cap": 5.0, "views": 1.0, "fresh": 0.5}` | Ranking of candidates: score × score + spike_z × min(score_z, spike_z_cap) / spike_z_cap + views × (views / best views in the pool) + fresh × (1 − age / pool_hours). |
| `longform.account_id` | `"youtube_main"` | `platform_accounts` row the episode posts to. |
| `longform.auto_build` | `false` | On: the moments of a planned episode are downloaded and rendered right after planning. Off: plan only (review the Telegram summary first). Migration 0059. |
| `longform.cut` | `{"pause_s": 0.4, "snap_before_s": 3.0, "snap_after_s": 3.0, "lead_share": 0.6, "match_tolerance_s": 0.6, "min_matches": 3, "max_shift_s": 12.0}` | How a clip is found in its re-downloaded footage (at least `min_matches` words agreeing within `match_tolerance_s`, at most `max_shift_s` from the expected place), and how the cut is widened toward the moment length (`lead_share` of the extra goes before the clip) with its ends snapped to the nearest speech pause of at least `pause_s`. |
| `longform.format_cycle` | `["mix", "creator"]` | Day rotation (0061). Each new episode takes the format after the last built one; a skipped day does not break it. `mix` = top moments of many creators; `creator` = one top creator only. |
| `longform.solo` | `{"pool_hours": 168, "min_s": 360, "target_s": 480, "max_s": 600, "cooldown_days": 14, "min_moments": 8}` | Creator day: the creator with the most good moments within `pool_hours` who had no creator episode within `cooldown_days`; at least `min_moments` moments and `min_s` seconds, filled to `target_s`, never above `max_s`. Nobody qualifies: the day is a mix day (0061). |
| `longform.thumb` | `{"ai": true, "model": "gpt-image-2.6-sunburst", "quality": "high", "size": "1536x864", "timeout_s": 240, "usd_max": 0.5, "frames": 2, "max_bytes": 2000000}` | Thumbnail (0061). `ai`: OpenAI image edit from `frames` episode frames (+ the creator picture on a creator day) in the style of `assets/longform/thumb_style.md`; off, refused, failed or over `usd_max` USD today → the #1 frame. The words are ours (template `thumb_16x9.html`). `max_bytes`: YouTube's custom thumbnail limit. |
| `longform.publish` | `{"video_url_ttl_s": 21600, "wait_timeout_s": 5400, "poll_s": 20, "category_id": "20"}` | Upload (0061): Upload-Post fetches the MP4 from a signed URL valid `video_url_ttl_s`; the result is awaited up to `wait_timeout_s`, polling every `poll_s`; YouTube category 20 = Gaming. |
| `longform.style` | `see migration 0059` | The 16:9 overlay. Captions bottom-centre; the `#N` badge top-left for `badge_s`; the creator credit bottom-left for `credit_s`; the brand mark top-right; `cold_open_text` and `end_card_text`; `thumb_text_size` for the 1280×720 thumbnail words. |

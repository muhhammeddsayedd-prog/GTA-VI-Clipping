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

## Discovery

| Key | Default | Meaning |
|---|---|---|
| `discovery.apify_last_run` | `null` | Written by code: id of the last Apify run (read on the next pass). |
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

## Live lane

| Key | Default | Meaning |
|---|---|---|
| `live.max_slots` | `6` | Max streams recorded at once. |
| `live.segment_s` | `60` | Recorder segment length. |
| `live.buffer_s` | `1800` | Rolling buffer kept on disk. |
| `live.max_reconnects` | `3` | Recorder reconnect attempts before releasing the slot. |
| `live.chat_offset_s` | `{"twitch": 0, "youtube": 0}` | Extra seconds to shift chat time vs recorded video (tuned in staging). |

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
| `spikes.ab_min_confidence` | `0.6` | A/B need Claude confidence >= this. |
| `spikes.min_messages` | `15` | Minimum messages in a spike bucket (absolute floor). |
| `spikes.merge_gap_s` | `60` | Spikes closer than this are merged. |
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
| `factory.loudness_lufs` | `-14` | Target loudness. |
| `factory.safe_top_px` | `220` | No text above this. |
| `factory.safe_bottom_px` | `420` | No text in the bottom N px. |
| `factory.accent_color` | `"#FFD400"` | Accent colour (word highlight, keywords). |
| `factory.font` | `"Montserrat Black"` | Subtitle font. |
| `factory.hook_seconds` | `3.0` | How long the hook title shows. |
| `factory.end_card_s` | `1.5` | End card length. |
| `factory.watermark` | `"@6starsclipping"` | Watermark text. |
| `factory.series_label` | `"6 Stars Moments"` | Series label shown on the end card. |
| `factory.bleep` | `true` | Bleep profanity in audio and mask it in subtitles. |
| `factory.music_duck_db` | `-18` | Gain applied to segments flagged as music (hourly clips under the music limit). |

## AI

| Key | Default | Meaning |
|---|---|---|
| `ai.model_writer` | `"claude-sonnet-5-5"` | Model for clip writing, moment picking, vision layout. |
| `ai.model_fast` | `"claude-haiku-5-5"` | Model for spike confirmation and classifiers. |
| `ai.daily_budget_usd` | `25` | Daily Anthropic spend cap. Over it: VOD lane pauses, express continues. |
| `transcribe.express_provider` | `"openai"` | Hosted transcription for express: groq | openai. |
| `transcribe.daily_budget_usd` | `5` | Daily hosted transcription cap. |

## Quality gate

| Key | Default | Meaning |
|---|---|---|
| `gate.hourly` | `{"min_len": 20, "max_len": 60, "max_music_ratio": 0.1, "min_score": 7, "topic_dedupe_h": 48, "precheck_risky": true}` | Quality gate profile for hourly clips. |
| `gate.express` | `{"min_len": 15, "max_len": 45, "max_music_ratio": 0.0, "min_score": 8, "topic_dedupe_h": 6, "tiers": ["preferred", "auto_no_music"]}` | Quality gate profile for express clips. |
| `gate.spoiler_block_until` | `"2026-12-03T00:00:00Z"` | Story-spoiler clips rejected until this time. Titles/hooks never contain spoilers. |
| `gate.reject_topics` | `["real-world politics", "gambling", "sponsored segment", "real-world violence", "sexual content", "personal information"]` | Always rejected. |
| `gate.auto_creator_daily_cap` | `4` | Max posted clips per day per auto-tier creator. |
| `precheck.enabled` | `true` | Run YouTube private-upload pre-check for risky hourly clips. |
| `precheck.wait_s` | `1800` | Wait before reading pre-check status. |
| `precheck.daily_max` | `20` | Max pre-check uploads per day. |

## Posting

| Key | Default | Meaning |
|---|---|---|
| `posting.jitter_max_s` | `300` | Hourly slot random offset 0..N seconds. |
| `posting.min_gap_s` | `900` | Minimum seconds between posts on one account. |
| `posting.spike_max_per_hour` | `2` | Max spike posts per account per rolling hour. |
| `posting.skip_hourly_after_spike_s` | `1800` | Skip an hourly slot if a spike posted within this many seconds before it. |
| `posting.spike_max_wait_s` | `900` | A spike may wait this long for the min gap; after that it is demoted. |
| `posting.spike_max_age_s` | `1800` | Spike clips older than this lose spike status. |
| `posting.freshness_half_life_h` | `{"live": 6, "vod": 24}` | Freshness decay half-life by source. |
| `posting.max_clip_age_h` | `72` | Approved clips older than this expire. |
| `posting.queue_low_watermark` | `12` | Below this many approved hourly clips, guardian widens discovery. |
| `posting.first_comment` | `"Follow @6starsclipping for the best GTA 6 moments every hour 🎮⭐"` | First comment text. |
| `posting.base_hashtags` | `["#GTA6", "#GTAVI", "#GTA6Clips", "#Gaming"]` | Hashtags added to every caption. |
| `posting.youtube_extra_hashtags` | `["#Shorts"]` | Extra hashtags for YouTube. |

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
| `guardian.failover_grace_s` | `{"hourly": 4200, "discovery": 1500}` | If an n8n trigger has not arrived for this long, the scheduler runs it itself. |

## Cleanup

| Key | Default | Meaning |
|---|---|---|
| `cleanup.posted_clip_days` | `3` | Delete clip files this many days after posting. |
| `cleanup.rejected_clip_hours` | `24` | Delete rejected clip files after this many hours. |

## Reports

| Key | Default | Meaning |
|---|---|---|
| `reports.timezone` | `"UTC"` | Report timezone. |
| `reports.ypp_targets` | `{"subscribers": 1000, "shorts_views_90d": 10000000}` | YouTube Partner Program targets shown in reports. |

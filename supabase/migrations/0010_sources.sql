-- Source hardening (staging server findings): Data API search as a discovery source, source health,
-- language signals, yield ranking. Additive and idempotent: new keys are inserted with
-- `on conflict do nothing`; the three changed values below are only changed while they still hold
-- the value an earlier migration seeded, so a value the owner set by hand is never overwritten.
insert into settings (key, value, description) values
  ('discovery.search_api_mode', '"fallback"'::jsonb, 'YouTube Data API search.list as a discovery source: "fallback" (only in a pass where the yt-dlp search failed, found nothing or is backing off), "always" or "off".'),
  ('discovery.search_api_calls_per_pass', '1'::jsonb, 'search.list calls one discovery pass may make at most.'),
  ('discovery.search_api_interval_s', '1800'::jsonb, 'Seconds between two search.list calls (1800 = every third pass at the 10-minute cadence, 48 calls a day).'),
  ('discovery.search_api_daily_calls', '60'::jsonb, 'search.list calls per UTC day at most. Google allows 100 a day in a bucket of its own; the rest is left for the doctor and manual runs.'),
  ('discovery.search_api_modes', '["live", "recent"]'::jsonb, 'What the search.list calls rotate through: "live" (eventType=live), "recent" (any video published within discovery.vod_lookback_h), "completed" (finished broadcasts only).'),
  ('discovery.search_api_combined_modes', '["recent", "completed"]'::jsonb, 'Modes that search every term of env.search_terms in one OR query per call. The other modes take one term per call, in turn (measured: the OR query finds few live streams).'),
  ('discovery.search_api_durations', '{"recent": "long", "completed": "long"}'::jsonb, 'search.list videoDuration per mode: any, long (over 20 minutes), medium (4 to 20 minutes) or short. Without it half of the recent results are Shorts.'),
  ('discovery.search_api_orders', '{"live": "viewCount", "recent": "viewCount", "completed": "viewCount"}'::jsonb, 'search.list order per mode: date, rating, relevance, title or viewCount.'),
  ('discovery.search_api_relevance_language', '"en"'::jsonb, 'search.list relevanceLanguage (results most relevant to this language); "" to leave it out.'),
  ('discovery.search_api_category_id', '"20"'::jsonb, 'search.list videoCategoryId (20 = Gaming); "" to leave it out.'),
  ('discovery.search_api_state', 'null'::jsonb, 'Written by code: rotation position, time of the last search.list call, quota pause.'),
  ('discovery.live_recheck_max', '50'::jsonb, 'YouTube streams seen live before (and every stream holding a live slot) that are looked up again per pass with videos.list, 1 quota unit per 50. 0 = off.'),
  ('discovery.live_recheck_window_s', '5400'::jsonb, 'A stream is looked up again while its last live observation is at most this old.'),
  ('discovery.source_failure_notify_after', '3'::jsonb, 'Failed passes in a row after which a discovery source is reported once (warn); one info follows when it works again.'),
  ('discovery.ytdlp_backoff_after_failures', '2'::jsonb, 'Failed passes in a row after which the yt-dlp search skips itself for discovery.ytdlp_backoff_s.'),
  ('discovery.ytdlp_backoff_s', '3600'::jsonb, 'Seconds the yt-dlp search is skipped after repeated failures; then one search probes whether it works again.'),
  ('discovery.ytdlp_use_cookies', 'false'::jsonb, 'true passes YOUTUBE_COOKIES_FILE (when the file exists) to the yt-dlp search as well. Off by default: the cookies are kept for chat and downloads.'),
  ('discovery.source_health', 'null'::jsonb, 'Written by code: failed passes in a row per discovery source and the yt-dlp back-off.'),
  ('discovery.non_latin_share', '0.3'::jsonb, 'Share of non-Latin letters in a title, channel name or description above which a video counts as not English.'),
  ('discovery.non_english_terms', '["hindi", "हिंदी", "हिन्दी", "urdu", "tamil", "telugu", "malayalam", "kannada", "bangla", "bengali", "marathi", "punjabi", "nepali", "indonesia", "tagalog", "pinoy", "español", "espanol", "en vivo", "en directo", "português", "portugues", "ao vivo", "brasil", "deutsch", "français", "francais", "türkçe", "turkce", "shortsfeed", "shortslive", "secret mod"]'::jsonb, 'Words that mark a video as not English (or as having no usable speech, like the Shorts-feed streams) when the title, channel name, a tag or a description hashtag contains one (substring, any case). Overrules a reported English; the video is ranked lower, not dropped.'),
  ('discovery.edited_upload_bonus', '6'::jsonb, 'Score points an edited upload or highlight (no chat replay, at most discovery.edited_max_duration_s long) gets at full engagement; in proportion below it.'),
  ('discovery.edited_max_duration_s', '3600'::jsonb, 'An upload without chat replay up to this length counts as edited material; longer ones as raw recordings.'),
  ('discovery.edited_engagement_full', '0.04'::jsonb, '(likes + 2 x comments) / views at which an edited upload gets the whole discovery.edited_upload_bonus.'),
  ('discovery.long_stream_min_s', '3600'::jsonb, 'Past streams and raw uploads at least this long are scored by how much chat they have (discovery.thin_chat_factor).'),
  ('discovery.thin_chat_factor', '0.75'::jsonb, 'Score factor of a long past stream with no chat at all; rises to 1 as the estimated chat replay reaches discovery.chat_min_messages. 1 = off.')
on conflict (key) do nothing;

-- Yield (M08 notes, "Proposed settings"): P1 reads the caption text now and answers real moments at
-- 0.50 to 0.55; at 0.55 the lane kept one more clip-worthy moment of 13.7 hours of streams.
update settings set value = '0.55'::jsonb
where key = 'spikes.ab_min_confidence' and value = '0.6'::jsonb;

-- Let the transcript fallback top up streams whose chat gave fewer than three confirmed spikes
-- (about $0.06 per hour of stream); before, it ran only when the chat gave none.
update settings set value = '3'::jsonb
where key = 'vod.fallback_below_spikes' and value = '1'::jsonb;

-- Channel feeds are free and are not refused from the server: read twice as many per pass.
update settings set value = '50'::jsonb
where key = 'discovery.rss_max_channels_per_run' and value = '25'::jsonb;

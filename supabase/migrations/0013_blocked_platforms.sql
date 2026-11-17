-- Blocked platforms (fix-blocked-platform-queue): a platform whose downloads are refused gets no new VOD
-- jobs and cannot fill the backlog, live slots go to platforms that can record, and a per-platform score
-- weight. Additive and idempotent: new keys only, inserted with `on conflict do nothing`; one description
-- is brought up to date. The code falls back to the same values
-- (`sixstars.discovery.config.BLOCKED_DEFAULTS`) on a database where this migration is not applied.
insert into settings (key, value, description) values
  ('discovery.platform_weight', '{}'::jsonb, 'Factor on the trend score per platform, for example {"twitch": 1.2, "youtube": 0.8}. A platform that is not named has factor 1; the score stays inside 0 to 100. {} = no preference.'),
  ('discovery.max_blocked_waiting', '12'::jsonb, 'Videos per platform that may wait out a download block (videos.status = new, skip_reason download_blocked). Discovery marks the oldest beyond this as skipped so they do not retry for ever.'),
  ('discovery.blocked_platform_stale_s', '7200'::jsonb, 'A download block recorded in vod.source_health counts for this many seconds after its last refusal; then discovery queues the platform again. Keep it well above vod.blocked_backoff_s.'),
  ('vod.source_health', 'null'::jsonb, 'Written by code: platforms that refuse video downloads from this machine (since, last refusal, count, error). Discovery queues no new videos of a platform listed here; cleared when a download works.')
on conflict (key) do nothing;

update settings
set description = 'Discovery queues no more videos while this many are waiting for the VOD lane (videos.status = new). Videos waiting out a download block are not counted.'
where key = 'discovery.max_vod_backlog'
  and description is distinct from 'Discovery queues no more videos while this many are waiting for the VOD lane (videos.status = new). Videos waiting out a download block are not counted.';

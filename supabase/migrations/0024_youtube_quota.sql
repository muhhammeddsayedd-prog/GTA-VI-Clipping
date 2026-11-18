-- YouTube Data API quota (owner's request, 2026-11-18). Additive: one new table, two new settings, and the
-- internal daily cap raised from 3,000 to 9,000 units only where it still holds the seeded 3,000 (a value
-- set by hand is kept).
--
-- Numbered 0024, not 0023: 0023 is held by the spike-hold work (0023_spike_hold.sql), still on develop.
--
-- Google's default is 10,000 units a day per Cloud project for every endpoint except search.list and
-- videos.insert (own buckets of 100 calls); "Daily quotas reset at midnight Pacific Time (PT)."
-- (https://developers.google.com/youtube/v3/determine_quota_cost, checked 2026-11-18). 9,000 keeps 10 % of
-- the project's quota free for the owner-channel OAuth calls and for mistakes.
--
-- youtube_channel_cache  raw channels.list items (parts snippet + statistics) by channel id, reused for
--                        discovery.youtube_channel_cache_ttl_s by the follower counts, the permission
--                        checks (in-pass and creator.permission jobs) and the avatar lookups. A channel the
--                        API did not return is stored as {"id": ..., "missing": true}.

create table if not exists youtube_channel_cache (
  channel_id  text primary key,
  item        jsonb not null,
  fetched_at  timestamptz not null default now()
);
create index if not exists idx_youtube_channel_cache_fetched on youtube_channel_cache (fetched_at);
alter table youtube_channel_cache enable row level security;

insert into settings (key, value, description) values
  ('discovery.youtube_quota_timezone', '"America/Los_Angeles"'::jsonb,
   'Time zone whose midnight starts the YouTube quota day (Google resets the Data API quota at midnight Pacific time). Used for discovery.youtube_quota_daily_units and the search.list call count only; money budgets keep the UTC day.'),
  ('discovery.youtube_channel_cache_ttl_s', '3600'::jsonb,
   'Seconds a YouTube channels.list answer (title, description, subscriber count, picture) is reused from youtube_channel_cache before it is asked for again. 0 turns the cache off.')
on conflict (key) do nothing;

update settings
set value = '9000'::jsonb,
    description = 'YouTube Data API quota units discovery may use per quota day (from midnight in discovery.youtube_quota_timezone; Google gives the project 10,000). Over it, discovery scores from the search listing alone.'
where key = 'discovery.youtube_quota_daily_units' and value = '3000'::jsonb;

-- Learning loop L0: the delivery guard (docs/28 "Fix first"). Additive and idempotent.
--
-- posts.delivery_suspect: the post sat in a zero-view window; Star Score medians and training skip it.
-- posts.organic_only / posts.boosted: ad rules (docs/28 Part B) read them; nothing sets them yet.
-- platform_accounts.hourly_paused_until / hourly_pause_reason: pause only the hourly lane of one account
-- (paused_until pauses both lanes). Only the owner clears it (POST /v1/control/lane/resume, /resume).

alter table posts add column if not exists delivery_suspect boolean not null default false;
alter table posts add column if not exists organic_only boolean not null default false;
alter table posts add column if not exists boosted boolean not null default false;

alter table platform_accounts add column if not exists hourly_paused_until timestamptz;
alter table platform_accounts add column if not exists hourly_pause_reason text;

insert into scoring_config (key, value, description) values
  ('learning.delivery_guard',
   '{"enabled": false, "last_posts": 5, "min_age_h": 6, "tiktok_missing_id_h": 6}'::jsonb,
   'L0. After each stats pass: for each enabled account, the median views of its last_posts published posts that are at least min_age_h old. A post counts as 0 views only once it has been read (stats_updated_at set); a TikTok post with no platform post id tiktok_missing_id_h after posting also counts as 0. A median of 0 pauses the account''s hourly lane (hourly_paused_until), marks those posts delivery_suspect and alerts on Telegram. Only the owner resumes the lane.'),
  ('learning.exclude_tiktok',
   '{"enabled": true}'::jsonb,
   'L0 owner amendment (19 Nov 2026). While on, every TikTok post is left out of Star Score medians, percentiles and training, until TikTok delivery is confirmed fixed.')
on conflict (key) do nothing;

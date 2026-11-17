-- Posting limits for the M10 posting engine. Additive and idempotent: safe to run again.

-- Upload-Post's own limits per account per rolling 24 h, and the staging switch for the one Instagram test.
insert into settings (key, value, description) values
  ('posting.platform_max_24h', '{"tiktok": 15, "instagram": 50, "youtube": 30}'::jsonb,
   'Hard ceiling of posts per account per rolling 24 h set by Upload-Post. An account cap above it is ignored.'),
  ('posting.staging_allow_instagram', 'false'::jsonb,
   'Staging only: allow a real Instagram post (Instagram cannot post privately). Set true only for the single M15 test post.')
on conflict (key) do nothing;

-- Upload-Post caps YouTube at 30 posts per 24 h: 28 leaves a margin of 2, and 28 - 4 = 24 hourly posts.
-- Only the seeded values are replaced, so a cap the system lowered after a rate limit is kept. On a fresh
-- database the row does not exist yet (the seed files, which carry the same values, run after migrations).
update platform_accounts
set cap_24h = 28, spike_reserve = 4
where id = 'youtube_main' and cap_24h = 40 and spike_reserve = 8;

-- The posting engine reads all posts of a clip ("already posted") and the posts of the last 24 h.
create index if not exists idx_posts_clip on posts (clip_id);
create index if not exists idx_posts_created on posts (created_at);

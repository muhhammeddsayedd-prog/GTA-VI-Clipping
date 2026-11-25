-- Long-form episodes go-live (development-plan/M20, decision D122, 2026-11-25). Idempotent.
--
-- 1. Day formats: episodes alternate between a "mix" day (top moments of many creators) and a "creator" day
--    (one top creator only, 6-10 min). longform.format_cycle is the rotation; longform.solo the creator-day
--    rules. A creator day that no creator can fill falls back to a mix day.
-- 2. Generated packaging: one writer call per episode (prompt PL1) writes title, intro, hashtags, tags,
--    thumbnail words and the thumbnail scene.
-- 3. AI thumbnail: OpenAI image generation from the episode's own frames (and the creator's picture on a
--    creator day), our own typography on top. longform.thumb.
-- 4. Publishing: longform.publish (signed URL lifetime, upload wait, YouTube category).

alter table episodes add column if not exists format text not null default 'mix';
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'episodes_format_check') then
    alter table episodes add constraint episodes_format_check check (format in ('mix','creator'));
  end if;
end $$;
alter table episodes add column if not exists featured_creator_id uuid references creators(id);
alter table episodes add column if not exists hashtags text[];
alter table episodes add column if not exists thumb_scene text;
alter table episodes add column if not exists thumb_source text;
create index if not exists episodes_featured_creator_idx on episodes (featured_creator_id)
  where featured_creator_id is not null;
create index if not exists episode_items_creator_idx on episode_items (creator_id);

insert into settings (key, value, description) values
  ('longform.format_cycle', '["mix","creator"]'::jsonb,
   'Long-form episodes: the day rotation. Each new episode takes the format after the last built one (a skipped day does not break it). mix = top moments of many creators; creator = one top creator only.'),
  ('longform.solo',
   '{"pool_hours": 168, "min_s": 360, "target_s": 480, "max_s": 600, "cooldown_days": 14, "min_moments": 8}'::jsonb,
   'Long-form episodes, creator day: the creator with the most good moments within pool_hours who has not had a creator episode within cooldown_days; at least min_moments moments and min_s seconds, filled to target_s, never above max_s. Nobody qualifies: the day is a mix day.'),
  ('longform.thumb',
   '{"ai": true, "model": "gpt-image-2.6-sunburst", "quality": "high", "size": "1536x864", "timeout_s": 240,
     "usd_max": 0.5, "frames": 2, "max_bytes": 2000000}'::jsonb,
   'Long-form episodes: thumbnail. ai = OpenAI image edit from the episode frames (+ the creator picture on a creator day), else the plain frame thumbnail; model / quality / size as in the OpenAI Images API; usd_max = the most one thumbnail may cost; max_bytes = YouTube custom thumbnail limit.'),
  ('longform.publish',
   '{"video_url_ttl_s": 21600, "wait_timeout_s": 5400, "poll_s": 20, "category_id": "20"}'::jsonb,
   'Long-form episodes: upload. The video goes to Upload-Post as a signed Storage URL valid video_url_ttl_s; the upload is awaited up to wait_timeout_s, polling every poll_s; category_id = YouTube category (20 = Gaming).')
on conflict (key) do nothing;

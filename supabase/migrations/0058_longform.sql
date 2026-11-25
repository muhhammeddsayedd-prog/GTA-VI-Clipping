-- Long-form episodes L1 (development-plan/M20, decision D122, 2026-11-25). Idempotent: tables, indexes and the
-- bucket are created only where missing; settings are added only where missing (an operator's value is kept).
--
-- One daily 1920x1080 "TOP N" episode built from moments the engine already clipped (docs/31). Episodes have
-- their own tables and post record: posts, clips.lane and the posting Lane enum are not touched.

-- 1. episodes: one row per day. status runs planned -> fetching -> rendering -> assembling -> ready ->
--    publishing -> posted_private | posted; side exits failed, skipped (pool too thin) and removed (takedown).
create table if not exists episodes (
  id                uuid primary key default gen_random_uuid(),
  episode_date      date not null unique,
  status            text not null default 'planned'
                    check (status in ('planned','fetching','rendering','assembling','ready','publishing',
                                      'posted_private','posted','failed','skipped','removed')),
  n_moments         int,
  duration_s        numeric,
  title             text,
  description       text,
  chapters          jsonb,
  tags              text[],
  thumb_text        text,
  storage_path      text,
  thumb_path        text,
  local_path        text,
  account_id        text references platform_accounts(id),
  privacy           text check (privacy in ('private','unlisted','public')),
  upload_request_id text,
  platform_post_id  text,
  post_url          text,
  published_at      timestamptz,
  error             text,
  attempts          int not null default 0,
  views             bigint,
  likes             bigint,
  comments          bigint,
  stats_at          timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index if not exists idx_episodes_status on episodes (status, episode_date desc);
drop trigger if exists trg_episodes_updated on episodes;
create trigger trg_episodes_updated before update on episodes
  for each row execute function set_updated_at();

-- 2. episode_items: one row per moment. position is the countdown number (N .. 1; 1 plays last).
--    fetch_* are positions in the re-downloaded video, cut_* the final cut inside the fetched file.
create table if not exists episode_items (
  id               uuid primary key default gen_random_uuid(),
  episode_id       uuid not null references episodes(id) on delete cascade,
  position         int not null check (position >= 1),
  clip_id          uuid not null references clips(id),
  creator_id       uuid not null references creators(id),
  est_s            numeric,
  fetch_url        text,
  fetch_start_s    numeric,
  fetch_end_s      numeric,
  cut_start_s      numeric,
  cut_end_s        numeric,
  align_offset_s   numeric,
  segment_path     text,
  duration_s       numeric,
  status           text not null default 'planned'
                   check (status in ('planned','fetched','rendered','failed','dropped')),
  error            text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (episode_id, position)
);
create index if not exists idx_episode_items_clip on episode_items (clip_id);
create index if not exists idx_episode_items_creator on episode_items (creator_id);
drop trigger if exists trg_episode_items_updated on episode_items;
create trigger trg_episode_items_updated before update on episode_items
  for each row execute function set_updated_at();

-- 3. Private bucket for the episode MP4s and thumbnails, capped at 2 GB per file where the storage schema has
--    the column (Supabase); the CI stub only has id/name/public.
do $$
begin
  if exists (select 1 from information_schema.columns
              where table_schema = 'storage' and table_name = 'buckets' and column_name = 'file_size_limit') then
    execute $q$insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
                values ('episodes', 'episodes', false, 2147483648, array['video/mp4','image/jpeg','image/png'])
                on conflict (id) do nothing$q$;
  else
    insert into storage.buckets (id, name, public) values ('episodes', 'episodes', false)
    on conflict (id) do nothing;
  end if;
end $$;

-- 4. Settings (docs/16 "Long-form episodes"). Production defaults: the master switch and publishing start off;
--    staging turns them on as operator values (enabled, publish_mode private, pool_hours 72, min_score 6).
insert into settings (key, value, description) values
  ('longform.enabled', 'false'::jsonb,
   'Long-form episodes (D122): master switch. Off: no episode is planned, built or published.'),
  ('longform.publish_mode', '"off"'::jsonb,
   'Long-form episodes: off = build only (console preview), private = upload as a private YouTube video, public = publish. Staging is always private. public needs the owner''s go.'),
  ('longform.build_hour_utc', '10'::jsonb, 'Long-form episodes: UTC hour the daily episode is planned and built.'),
  ('longform.publish_hour_utc', '16'::jsonb, 'Long-form episodes: UTC hour the ready episode is uploaded.'),
  ('longform.pool_hours', '48'::jsonb, 'Long-form episodes: clips created within this many hours are candidates.'),
  ('longform.pool_statuses', '["approved","posted","expired"]'::jsonb,
   'Long-form episodes: clip statuses that are candidates (passed the quality gate).'),
  ('longform.pool_reject_reasons',
   '["creator_daily_cap","duplicate_topic","unreadable_subtitles","creator_tier_express"]'::jsonb,
   'Long-form episodes: rejected clips with one of these reasons are also candidates (Shorts-only rules).'),
  ('longform.min_score', '7'::jsonb, 'Long-form episodes: minimum clip score (Claude 1..10).'),
  ('longform.target_s', '720'::jsonb, 'Long-form episodes: target episode length in seconds (moments only).'),
  ('longform.min_s', '600'::jsonb, 'Long-form episodes: below this the day is skipped, never padded.'),
  ('longform.max_s', '870'::jsonb, 'Long-form episodes: hard maximum (14:30, under the 15 min YouTube default).'),
  ('longform.segment_min_s', '15'::jsonb, 'Long-form episodes: shortest moment in seconds.'),
  ('longform.segment_max_s', '45'::jsonb, 'Long-form episodes: longest moment in seconds.'),
  ('longform.lead_s', '3'::jsonb, 'Long-form episodes: seconds kept before the build-up starts.'),
  ('longform.tail_s', '2'::jsonb, 'Long-form episodes: seconds kept after the payoff pause.'),
  ('longform.max_per_creator', '3'::jsonb, 'Long-form episodes: most moments of one creator per episode.'),
  ('longform.max_creator_s', '90'::jsonb, 'Long-form episodes: most seconds of one creator per episode.'),
  ('longform.reuse_after_days', '7'::jsonb, 'Long-form episodes: a clip can be in a new episode again after this many days.'),
  ('longform.fetch_pad_s', '5'::jsonb, 'Long-form episodes: extra seconds downloaded on each side of a moment.'),
  ('longform.whisper_model', '"small"'::jsonb, 'Long-form episodes: local faster-whisper model for alignment and subtitles.'),
  ('longform.render', '{"width": 1920, "height": 1080, "fps": 30, "crf": 20, "maxrate": "8M"}'::jsonb,
   'Long-form episodes: segment encode (full frame, scale + pad, no crop).'),
  ('longform.priority', '15'::jsonb, 'Long-form episodes: job priority on the normal queue (below hourly and spikes).'),
  ('longform.keep_days', '7'::jsonb, 'Long-form episodes: local and Storage files are deleted after this many days.'),
  ('longform.cold_open_s', '5'::jsonb, 'Long-form episodes: seconds of #1 shown before the countdown.'),
  ('longform.end_card_s', '10'::jsonb, 'Long-form episodes: end card length (room for YouTube end-screen elements).'),
  ('longform.rank_weights', '{"score": 1.0, "spike_z": 1.0, "spike_z_cap": 5.0, "views": 1.0, "fresh": 0.5}'::jsonb,
   'Long-form episodes: ranking = score x score + spike_z x min(score_z, spike_z_cap)/spike_z_cap + views x (views / best views in the pool) + fresh x (1 - age / pool_hours).'),
  ('longform.account_id', '"youtube_main"'::jsonb, 'Long-form episodes: platform_accounts row the episode posts to.')
on conflict (key) do nothing;

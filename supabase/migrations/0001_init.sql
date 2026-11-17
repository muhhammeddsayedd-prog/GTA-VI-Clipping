-- 6 Stars Clips — initial schema
-- Apply with: supabase db push  (or supabase migration up)
-- All times UTC (timestamptz). Text + CHECK constraints instead of enums for easy evolution.

create extension if not exists pgcrypto;

-- ─────────────────────────────────────────────────────────────
-- Helpers
-- ─────────────────────────────────────────────────────────────
create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- ─────────────────────────────────────────────────────────────
-- settings: runtime configuration (key/value JSON)
-- ─────────────────────────────────────────────────────────────
create table settings (
  key          text primary key,
  value        jsonb not null,
  description  text,
  updated_at   timestamptz not null default now()
);
create trigger trg_settings_updated before update on settings
  for each row execute function set_updated_at();

-- ─────────────────────────────────────────────────────────────
-- creators
-- ─────────────────────────────────────────────────────────────
create table creators (
  id               uuid primary key default gen_random_uuid(),
  platform         text not null check (platform in ('twitch','youtube')),
  channel_id       text not null,                -- Twitch user_id or YouTube channel id
  handle           text not null,                -- login / @handle
  display_name     text,
  language         text,                         -- ISO 639-1 if known
  tier             text not null default 'auto'
                   check (tier in ('preferred','auto','restricted','blocked')),
  tier_reason      text,
  description_checked_at timestamptz,
  follower_count   bigint,
  audience_spread  numeric,                      -- 0..1, higher = more international
  avg_clip_views   numeric,                      -- rolling, from our posts
  claims_count     int not null default 0,
  blocked_at       timestamptz,
  discovered_at    timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (platform, channel_id)
);
create trigger trg_creators_updated before update on creators
  for each row execute function set_updated_at();
create index idx_creators_tier on creators (tier);

-- Facecam / layout cache per creator (layouts rarely change)
create table creator_layouts (
  creator_id       uuid primary key references creators(id) on delete cascade,
  source_width     int not null,
  source_height    int not null,
  facecam_box      jsonb,          -- {"x":..,"y":..,"w":..,"h":..} in source pixels, null = no facecam
  gameplay_box     jsonb,          -- region to use as gameplay (defaults to full frame)
  confidence       numeric,
  detected_from    text,           -- clip/frame reference
  detected_at      timestamptz not null default now(),
  expires_at       timestamptz not null default now() + interval '7 days'
);

-- ─────────────────────────────────────────────────────────────
-- candidates: every discovery observation
-- ─────────────────────────────────────────────────────────────
create table candidates (
  id               bigint generated always as identity primary key,
  creator_id       uuid not null references creators(id) on delete cascade,
  source_type      text not null check (source_type in ('live','vod')),
  platform         text not null check (platform in ('twitch','youtube')),
  source_id        text not null,           -- stream id / video id
  url              text not null,
  title            text,
  language         text,
  viewers          int,
  views            bigint,
  views_per_hour   numeric,
  likes            bigint,
  comments         bigint,
  published_at     timestamptz,
  started_at       timestamptz,
  trend_score      numeric not null,
  score_parts      jsonb,
  checked_at       timestamptz not null default now(),
  picked           boolean not null default false
);
create index idx_candidates_source on candidates (platform, source_id, checked_at desc);
create index idx_candidates_checked on candidates (checked_at desc);

-- ─────────────────────────────────────────────────────────────
-- live_sessions: one recorded live stream
-- ─────────────────────────────────────────────────────────────
create table live_sessions (
  id               uuid primary key default gen_random_uuid(),
  creator_id       uuid not null references creators(id),
  platform         text not null check (platform in ('twitch','youtube')),
  source_id        text not null,           -- Twitch stream id / YouTube video id of the live
  channel_login    text not null,           -- Twitch login or YouTube channel id (for chat)
  url              text not null,
  status           text not null default 'assigned'
                   check (status in ('assigned','recording','ended','replay_queued','replay_done','failed','released')),
  trend_score      numeric,
  below_cutoff_since timestamptz,
  chat_baseline    jsonb,
  bot_names        text[] not null default '{}',
  clipped_ranges   jsonb not null default '[]',   -- [{"start_wall":ts,"end_wall":ts,"clip_id":..}]
  vod_source_id    text,                          -- Twitch VOD id / same YouTube id after stream ends
  buffer_dir       text,
  reconnects       int not null default 0,
  assigned_at      timestamptz not null default now(),
  started_at       timestamptz,
  ended_at         timestamptz,
  updated_at       timestamptz not null default now(),
  unique (platform, source_id)
);
create trigger trg_live_sessions_updated before update on live_sessions
  for each row execute function set_updated_at();
create index idx_live_sessions_status on live_sessions (status);

-- ─────────────────────────────────────────────────────────────
-- videos: finished streams / uploads for the VOD lane
-- ─────────────────────────────────────────────────────────────
create table videos (
  id               uuid primary key default gen_random_uuid(),
  creator_id       uuid not null references creators(id),
  platform         text not null check (platform in ('twitch','youtube')),
  source_id        text not null,
  url              text not null,
  title            text,
  duration_s       int,
  published_at     timestamptz,
  trend_score      numeric,
  has_chat         boolean,
  live_session_id  uuid references live_sessions(id),
  status           text not null default 'new'
                   check (status in ('new','processing','done','failed','skipped')),
  skip_reason      text,
  attempts         int not null default 0,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (platform, source_id)
);
create trigger trg_videos_updated before update on videos
  for each row execute function set_updated_at();
create index idx_videos_status on videos (status, trend_score desc);

-- ─────────────────────────────────────────────────────────────
-- chat_files: downloaded chat replays
-- ─────────────────────────────────────────────────────────────
create table chat_files (
  id               uuid primary key default gen_random_uuid(),
  platform         text not null check (platform in ('twitch','youtube')),
  source_id        text not null,
  video_id         uuid references videos(id) on delete cascade,
  storage_path     text,                    -- local path or storage key
  messages_count   int,
  duration_s       int,
  baseline_rate    numeric,                 -- msgs per 10 s, whole-stream median
  bot_names        text[] not null default '{}',
  downloaded_at    timestamptz not null default now(),
  unique (platform, source_id)
);

-- ─────────────────────────────────────────────────────────────
-- spikes: every detected chat spike (live or replay)
-- ─────────────────────────────────────────────────────────────
create table spikes (
  id               uuid primary key default gen_random_uuid(),
  mode             text not null check (mode in ('live','replay','transcript')),
  live_session_id  uuid references live_sessions(id),
  video_id         uuid references videos(id),
  creator_id       uuid not null references creators(id),
  detected_at      timestamptz not null default now(),
  -- position of the spike: wall clock (live) or offset in VOD seconds (replay)
  spike_wall       timestamptz,
  spike_offset_s   numeric,
  score_z          numeric,
  parts            jsonb,                   -- {"msgs_z":..,"uniq_z":..,"react_z":..,"clipreq_z":..,"audio_z":..}
  msgs_in_bucket   int,
  unique_chatters  int,
  reaction_count   int,
  clip_requests    int,
  chat_sample      jsonb,                   -- up to 40 messages around the spike
  tier             text check (tier in ('S','A','B','rejected')),
  claude_confidence numeric,
  claude_reason    text,
  window_start_s   numeric,                 -- seconds (offset in VOD, or relative to buffer)
  window_end_s     numeric,
  window_start_wall timestamptz,
  window_end_wall  timestamptz,
  status           text not null default 'detected'
                   check (status in ('detected','confirmed','rejected','rendering','clipped','failed','merged')),
  merged_into      uuid references spikes(id),
  clip_id          uuid,
  spike_to_post_s  int
);
create index idx_spikes_session on spikes (live_session_id, detected_at);
create index idx_spikes_video on spikes (video_id);
create index idx_spikes_status on spikes (status);

-- ─────────────────────────────────────────────────────────────
-- clips
-- ─────────────────────────────────────────────────────────────
create table clips (
  id               uuid primary key default gen_random_uuid(),
  series_no        int,
  spike_id         uuid references spikes(id),
  creator_id       uuid not null references creators(id),
  source_platform  text not null check (source_platform in ('twitch','youtube')),
  source_id        text not null,
  source_url       text not null,
  source_type      text not null check (source_type in ('live','vod')),
  lane             text not null check (lane in ('spike','hourly')),
  start_s          numeric not null,
  end_s            numeric not null,
  duration_s       numeric,
  transcript       jsonb,                   -- words with timings (English)
  original_language text,
  hook             text,
  title            text,                    -- YouTube title
  caption          text,                    -- TikTok/Instagram caption (also YT description body)
  hashtags         text[],
  commentary       text,
  topic            text,                    -- slug for dedupe
  keywords         jsonb,                   -- per subtitle line
  spoiler          boolean,
  safety_flags     jsonb,
  score            numeric,                 -- Claude 1..10
  score_parts      jsonb,
  rank_score       numeric,                 -- score x freshness x spike bonus (recomputed by ranking)
  music_ratio      numeric,
  profanity_count  int,
  gate_profile     text check (gate_profile in ('hourly','express')),
  gate_result      jsonb,
  reject_reason    text,
  needs_precheck   boolean not null default false,
  precheck_video_id text,
  precheck_result  jsonb,
  storage_path     text,
  local_path       text,
  file_size_bytes  bigint,
  status           text not null default 'rendering'
                   check (status in ('rendering','gating','precheck_pending','approved','rejected','posted','removed','expired')),
  render_ms        int,
  created_at       timestamptz not null default now(),
  approved_at      timestamptz,
  posted_at        timestamptz,
  updated_at       timestamptz not null default now()
);
create trigger trg_clips_updated before update on clips
  for each row execute function set_updated_at();
create index idx_clips_queue on clips (status, lane, rank_score desc);
create index idx_clips_topic on clips (topic, created_at desc);
create index idx_clips_creator on clips (creator_id, created_at desc);
alter table spikes add constraint fk_spikes_clip foreign key (clip_id) references clips(id);

create sequence clip_series_seq start 1;

-- ─────────────────────────────────────────────────────────────
-- platform_accounts: each posting destination + its caps
-- ─────────────────────────────────────────────────────────────
create table platform_accounts (
  id               text primary key,        -- e.g. 'tiktok_main','tiktok_live','instagram_main','youtube_main'
  platform         text not null check (platform in ('tiktok','instagram','youtube')),
  upload_post_user text not null,           -- Upload-Post profile name
  handle           text not null,
  lanes            text[] not null,         -- which lanes may post here: {'hourly'}, {'spike'}, {'hourly','spike'}
  cap_24h          int not null,            -- system cap (below platform limit)
  spike_reserve    int not null default 0,  -- posts kept free for spikes
  min_gap_s        int not null default 900,
  privacy          text,                    -- staging: SELF_ONLY / private
  hourly_hours_utc int[],                   -- allowed hourly slots (null = all 24)
  enabled          boolean not null default true,
  paused_until     timestamptz,
  pause_reason     text,
  updated_at       timestamptz not null default now()
);
create trigger trg_platform_accounts_updated before update on platform_accounts
  for each row execute function set_updated_at();

-- ─────────────────────────────────────────────────────────────
-- posts: one row per clip per account
-- ─────────────────────────────────────────────────────────────
create table posts (
  id               uuid primary key default gen_random_uuid(),
  clip_id          uuid not null references clips(id),
  account_id       text not null references platform_accounts(id),
  platform         text not null,
  lane             text not null check (lane in ('spike','hourly')),
  slot_at          timestamptz,             -- hourly slot time (null for spikes)
  status           text not null default 'pending'
                   check (status in ('pending','published','failed','skipped','removed','remove_failed')),
  dry_run          boolean not null default false,
  idempotency_key  text not null unique,
  upload_request_id text,
  platform_post_id text,
  post_url         text,
  error            text,
  attempts         int not null default 0,
  published_at     timestamptz,
  views            bigint,
  likes            bigint,
  comments         bigint,
  shares           bigint,
  stats_updated_at timestamptz,
  claim_status     text,                    -- null / 'ok' / 'blocked' / 'claimed' / 'removed_by_platform'
  removed_at       timestamptz,
  remove_reason    text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create trigger trg_posts_updated before update on posts
  for each row execute function set_updated_at();
create index idx_posts_account_time on posts (account_id, published_at desc);
create index idx_posts_status on posts (status);

-- ─────────────────────────────────────────────────────────────
-- account_stats: periodic snapshots per account
-- ─────────────────────────────────────────────────────────────
create table account_stats (
  id               bigint generated always as identity primary key,
  account_id       text not null references platform_accounts(id),
  captured_at      timestamptz not null default now(),
  followers        bigint,
  views_total      bigint,
  reach            bigint,
  impressions      bigint,
  raw              jsonb
);
create index idx_account_stats on account_stats (account_id, captured_at desc);

-- ─────────────────────────────────────────────────────────────
-- takedowns
-- ─────────────────────────────────────────────────────────────
create table takedowns (
  id               uuid primary key default gen_random_uuid(),
  received_at      timestamptz not null default now(),
  source           text not null,           -- 'email','platform','claim'
  sender           text,
  subject          text,
  body_excerpt     text,
  is_takedown      boolean,
  creator_id       uuid references creators(id),
  matched_post_ids uuid[],
  actions          jsonb,
  status           text not null default 'open'
                   check (status in ('open','actioned','partial','ignored'))
);

-- ─────────────────────────────────────────────────────────────
-- jobs: Postgres job queue
-- ─────────────────────────────────────────────────────────────
create table jobs (
  id               bigint generated always as identity primary key,
  queue            text not null check (queue in ('express','normal')),
  type             text not null,
  payload          jsonb not null default '{}',
  priority         int not null default 0,
  status           text not null default 'queued'
                   check (status in ('queued','running','done','failed','cancelled')),
  attempts         int not null default 0,
  max_attempts     int not null default 3,
  run_after        timestamptz not null default now(),
  locked_by        text,
  locked_at        timestamptz,
  last_error       text,
  dedupe_key       text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create trigger trg_jobs_updated before update on jobs
  for each row execute function set_updated_at();
create index idx_jobs_claim on jobs (queue, status, priority desc, run_after, id);
create unique index idx_jobs_dedupe on jobs (dedupe_key) where dedupe_key is not null and status in ('queued','running');

-- Claim function used by workers
create or replace function claim_job(p_queue text, p_worker text)
returns setof jobs language plpgsql as $$
begin
  return query
  update jobs j set status = 'running', locked_by = p_worker, locked_at = now(), attempts = j.attempts + 1
  where j.id = (
    select id from jobs
    where queue = p_queue and status = 'queued' and run_after <= now()
    order by priority desc, run_after, id
    for update skip locked
    limit 1
  )
  returning j.*;
end $$;

-- ─────────────────────────────────────────────────────────────
-- events: audit log
-- ─────────────────────────────────────────────────────────────
create table events (
  id               bigint generated always as identity primary key,
  created_at       timestamptz not null default now(),
  level            text not null default 'info' check (level in ('debug','info','warn','error','critical')),
  type             text not null,           -- e.g. 'spike.detected','post.published','guardian.stop'
  service          text,
  entity_type      text,
  entity_id        text,
  message          text,
  data             jsonb
);
create index idx_events_time on events (created_at desc);
create index idx_events_type on events (type, created_at desc);

-- ─────────────────────────────────────────────────────────────
-- notify_outbox: alerts waiting for n8n (survives n8n downtime)
-- ─────────────────────────────────────────────────────────────
create table notify_outbox (
  id               bigint generated always as identity primary key,
  created_at       timestamptz not null default now(),
  severity         text not null check (severity in ('info','warn','critical')),
  title            text not null,
  body             text,
  sent_at          timestamptz,
  attempts         int not null default 0
);
create index idx_outbox_unsent on notify_outbox (sent_at) where sent_at is null;

-- ─────────────────────────────────────────────────────────────
-- api_usage: spend tracking per provider per call
-- ─────────────────────────────────────────────────────────────
create table api_usage (
  id               bigint generated always as identity primary key,
  created_at       timestamptz not null default now(),
  provider         text not null,           -- anthropic, groq, openai, apify, upload_post, youtube
  operation        text not null,           -- e.g. 'clip_writer'
  model            text,
  input_tokens     int,
  output_tokens    int,
  units            numeric,                 -- seconds of audio, quota units, etc.
  cost_usd         numeric not null default 0,
  ref_id           text
);
create index idx_api_usage_day on api_usage (created_at, provider);

-- ─────────────────────────────────────────────────────────────
-- heartbeats: service liveness + trigger tracking
-- ─────────────────────────────────────────────────────────────
create table heartbeats (
  name             text primary key,        -- 'live-manager','worker-express-1','trigger:hourly','trigger:discovery'
  last_seen        timestamptz not null default now(),
  info             jsonb
);

-- ─────────────────────────────────────────────────────────────
-- Views used by reports
-- ─────────────────────────────────────────────────────────────
create view v_posts_last_24h with (security_invoker = true) as
  select account_id, count(*) filter (where status = 'published') as published,
         count(*) filter (where status = 'failed') as failed
  from posts where created_at > now() - interval '24 hours'
  group by account_id;

create view v_queue_depth with (security_invoker = true) as
  select lane, count(*) as approved
  from clips where status = 'approved' group by lane;

-- ─────────────────────────────────────────────────────────────
-- Security: RLS on, no policies. Only server-side connections
-- (postgres role / service role) can read or write.
-- ─────────────────────────────────────────────────────────────
alter table settings          enable row level security;
alter table creators          enable row level security;
alter table creator_layouts   enable row level security;
alter table candidates        enable row level security;
alter table live_sessions     enable row level security;
alter table videos            enable row level security;
alter table chat_files        enable row level security;
alter table spikes            enable row level security;
alter table clips             enable row level security;
alter table platform_accounts enable row level security;
alter table posts             enable row level security;
alter table account_stats     enable row level security;
alter table takedowns         enable row level security;
alter table jobs              enable row level security;
alter table events            enable row level security;
alter table notify_outbox     enable row level security;
alter table api_usage         enable row level security;
alter table heartbeats        enable row level security;

-- Storage bucket for clips (private)
insert into storage.buckets (id, name, public)
values ('clips', 'clips', false)
on conflict (id) do nothing;

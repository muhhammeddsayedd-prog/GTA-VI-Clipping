-- Clipping campaigns PR 1 (2026-11-23): campaign registry, render profiles, lane quotas, tier `campaign`.
-- Decision D117 (supersedes D05): paid clipping campaigns (Content Rewards) run inside the system as lanes.
-- Spec: "6 Stars Clips — Lanes & Clipping Campaigns Plan" (Plan + Operating flows tabs).
-- Naming: clip_campaigns / clip_campaign_posts, so they never mix with the paid-ads `ad_campaigns` of docs/28.
--
-- A campaign is a tag, not a new lane value: clips.lane / posts.lane stay spike|hourly and a nullable
-- campaign_id marks the campaign lane. Falling back to the Normal lane = campaign_id null + profile 'brand'.
-- Additive and idempotent.

-- 1. Render profiles: one row per look. A campaign brief that needs a new look is a new row, not code.
create table if not exists render_profiles (
  id               text primary key check (id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  watermark        boolean not null default true,        -- the "6 Stars Clips" mark
  logo_path        text,                                 -- storage path of a campaign logo (PNG)
  logo_pos         text check (logo_pos in ('bottom_right','bottom_left','top_right','top_left')),
  keep_overlay     boolean not null default false,       -- keep the streamer's own stream overlay in the crop
  disclosure_text  text,                                 -- e.g. "Paid partnership"
  disclosure_s     numeric check (disclosure_s is null or (disclosure_s > 0 and disclosure_s <= 5)),
  description      text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
drop trigger if exists trg_render_profiles_updated on render_profiles;
create trigger trg_render_profiles_updated before update on render_profiles
  for each row execute function set_updated_at();
alter table render_profiles enable row level security;

insert into render_profiles (id, watermark, logo_path, logo_pos, keep_overlay, disclosure_text, disclosure_s, description) values
  ('brand',          true,  null, null,           false, null,               null, 'Today''s look: Normal and Spike lanes'),
  ('clean-disclose', false, null, null,           false, 'Paid partnership', 1.5,  'Campaigns that ban logos'),
  ('partner-logo',   false, null, 'bottom_right', false, 'Paid partnership', 1.5,  'Campaign logo bottom-right; copy this row per campaign with its own logo_path'),
  ('overlay-keep',   false, null, null,           true,  'Paid partnership', 1.5,  'Keeps the streamer''s stream overlay in the crop'),
  ('brand-disclose', true,  null, null,           false, 'Paid partnership', 1.5,  'Campaigns that allow our mark')
on conflict (id) do nothing;

-- 2. The registry. Rows are entered by hand (CLI now, admin console later); never scraped.
create table if not exists clip_campaigns (
  id                uuid primary key default gen_random_uuid(),
  slug              text not null unique check (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  name              text not null,
  status            text not null default 'draft' check (status in ('draft','active','paused','ended')),
  platforms         text[] not null default '{}'
                    check (platforms <@ array['youtube','instagram','tiktok']::text[]),
  rate_per_1k       numeric not null check (rate_per_1k > 0),
  min_payout        numeric not null default 0 check (min_payout >= 0),
  max_payout        numeric check (max_payout is null or max_payout > 0),
  currency          text not null default 'USD',
  budget_total      numeric check (budget_total is null or budget_total > 0),
  budget_left       numeric check (budget_left is null or budget_left >= 0),
  budget_checked_at timestamptz,
  starts_at         timestamptz,
  ends_at           timestamptz,
  render_profile    text not null default 'brand-disclose' references render_profiles(id),
  caption_rules     jsonb not null default '{}'::jsonb,  -- {"required_tags": [...], "mention": "@x", "cta": "...", "banned_words": [...]}
  brief_rules       jsonb not null default '{}'::jsonb,  -- {"streamer_on_screen": true, "min_len": 10, "max_len": 60, "overlay_required": false}
  quota             jsonb not null default '{}'::jsonb,  -- {"youtube": {"min": 2, "max": 3}, "instagram": {...}}
  priority          int not null default 0,              -- wins when two campaigns share a streamer
  required_game     text default 'Grand Theft Auto VI',  -- null = any game
  submit_url        text,
  notes             text,
  version           int not null default 1,
  activated_at      timestamptz,
  ended_at          timestamptz,
  end_reason        text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  check (max_payout is null or max_payout >= min_payout),
  check (ends_at is null or starts_at is null or ends_at > starts_at)
);
drop trigger if exists trg_clip_campaigns_updated on clip_campaigns;
create trigger trg_clip_campaigns_updated before update on clip_campaigns
  for each row execute function set_updated_at();
create index if not exists idx_clip_campaigns_status on clip_campaigns (status);
alter table clip_campaigns enable row level security;

-- The streamers of a campaign: one person can have several sources (Twitch + YouTube simulcast).
create table if not exists clip_campaign_creators (
  campaign_id  uuid not null references clip_campaigns(id) on delete cascade,
  creator_id   uuid not null references creators(id),
  created_at   timestamptz not null default now(),
  primary key (campaign_id, creator_id)
);
create index if not exists idx_clip_campaign_creators_creator on clip_campaign_creators (creator_id);
alter table clip_campaign_creators enable row level security;

-- 3. One row per campaign post: submission, approval and money (filled from PR 5).
create table if not exists clip_campaign_posts (
  id             uuid primary key default gen_random_uuid(),
  post_id        uuid not null unique references posts(id),
  campaign_id    uuid not null references clip_campaigns(id),
  platform       text not null,
  post_url       text,
  status         text not null default 'to_submit'
                 check (status in ('to_submit','submitted','skipped','missed')),
  deadline       timestamptz,
  submitted_at   timestamptz,
  reminded_at    timestamptz,
  approval       text not null default 'pending' check (approval in ('pending','approved','rejected')),
  reject_reason  text,
  payable_views  bigint,
  expected_usd   numeric,
  paid_usd       numeric,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
drop trigger if exists trg_clip_campaign_posts_updated on clip_campaign_posts;
create trigger trg_clip_campaign_posts_updated before update on clip_campaign_posts
  for each row execute function set_updated_at();
create index if not exists idx_clip_campaign_posts_campaign on clip_campaign_posts (campaign_id, created_at desc);
create index if not exists idx_clip_campaign_posts_status on clip_campaign_posts (status, deadline);
alter table clip_campaign_posts enable row level security;

-- 4. Daily quotas per account and lane, in one place. lane_key: 'normal', 'spike' or 'campaign:<slug>'.
--    Campaign rows are written by campaigns.service when a campaign is saved; Normal and Spike rows below.
create table if not exists lane_quotas (
  account_id  text not null references platform_accounts(id),
  lane_key    text not null check (lane_key in ('normal','spike') or lane_key ~ '^campaign:[a-z0-9][a-z0-9-]{1,62}$'),
  min_day     int not null default 0 check (min_day >= 0),
  max_day     int not null check (max_day >= min_day),
  updated_at  timestamptz not null default now(),
  primary key (account_id, lane_key)
);
drop trigger if exists trg_lane_quotas_updated on lane_quotas;
create trigger trg_lane_quotas_updated before update on lane_quotas
  for each row execute function set_updated_at();
alter table lane_quotas enable row level security;

-- 5. Live status of campaign streamers, written by the campaign watcher (PR 2).
create table if not exists creator_live_status (
  creator_id  uuid primary key references creators(id),
  live        boolean not null default false,
  stream_id   text,
  game        text,
  title       text,
  viewers     int,
  started_at  timestamptz,
  checked_at  timestamptz not null default now()
);
alter table creator_live_status enable row level security;

-- 6. Tier `campaign` (above `preferred`): set while the creator has an active campaign; the P6 permission
--    read never overwrites it. The tier to go back to is kept in tier_before_campaign.
alter table creators drop constraint if exists creators_tier_check;
alter table creators add constraint creators_tier_check
  check (tier in ('campaign','preferred','auto','restricted','blocked'));
alter table creators add column if not exists tier_before_campaign text
  check (tier_before_campaign is null or tier_before_campaign in ('preferred','auto'));

-- 7. The campaign tag on the work that flows through the engine.
alter table live_sessions add column if not exists campaign_id uuid references clip_campaigns(id);
alter table videos        add column if not exists campaign_id uuid references clip_campaigns(id);
alter table viewer_clips  add column if not exists campaign_id uuid references clip_campaigns(id);
alter table spikes        add column if not exists campaign_id uuid references clip_campaigns(id);
alter table clips         add column if not exists campaign_id uuid references clip_campaigns(id);
alter table clips         add column if not exists render_profile text references render_profiles(id);
alter table posts         add column if not exists campaign_id uuid references clip_campaigns(id);
create index if not exists idx_clips_campaign on clips (campaign_id) where campaign_id is not null;
create index if not exists idx_posts_campaign on posts (campaign_id, created_at desc) where campaign_id is not null;

-- 8. Settings. Campaign share = posts a day per account that all campaigns together may take (inside
--    cap_24h; the rest is the Normal lane, which also holds the spike reserve). Instagram: the plan says 20 of
--    50, the account cap is 45, so 18. Per-campaign limits per platform and day.
insert into settings (key, value, description) values
  ('lanes.campaign_share', '{"youtube_main": 6, "instagram_main": 18, "tiktok_main": 6}'::jsonb,
   'Daily posts per account that all clipping campaigns together may take, inside cap_24h. Unused share returns to the Normal lane at lanes.return_unused_at_utc.'),
  ('lanes.campaign_max_per_day', '{"youtube": 3, "instagram": 10, "tiktok": 3}'::jsonb,
   'Most posts one campaign may get per platform and day.')
on conflict (key) do nothing;

-- Normal and Spike lane rows for every account: spike = its reserve, normal = the rest of the cap.
insert into lane_quotas (account_id, lane_key, min_day, max_day)
select id, 'spike', 0, spike_reserve from platform_accounts
on conflict (account_id, lane_key) do nothing;
insert into lane_quotas (account_id, lane_key, min_day, max_day)
select a.id, 'normal', 0,
       greatest(a.cap_24h - coalesce((s.value ->> a.id)::int, 0), 0)
from platform_accounts a
left join settings s on s.key = 'lanes.campaign_share'
on conflict (account_id, lane_key) do nothing;

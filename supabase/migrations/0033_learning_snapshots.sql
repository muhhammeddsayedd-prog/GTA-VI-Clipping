-- Learning loop L1: per-post metric snapshots at fixed ages (docs/28 Part A). Additive and idempotent.
--
-- One row per post and target age (1, 3, 6, 24, 72, 168 h), insert only. Missing platform fields are null,
-- never 0. paid: the post was boosted when read; paid_* hold the paid share where the platform splits it.

create table if not exists post_metrics_snapshots (
  id                 bigserial primary key,
  post_id            uuid not null references posts(id) on delete cascade,
  account_id         text not null,
  platform           text not null,
  captured_at        timestamptz not null default now(),
  target_age_h       numeric not null,
  age_h              numeric not null,
  source             text not null,            -- upload_post | youtube_data | youtube_data+analytics
  views              bigint,
  likes              bigint,
  comments           bigint,
  shares             bigint,
  saves              bigint,
  reach              bigint,
  new_followers      bigint,
  completion_rate    double precision,         -- 0..1 (TikTok full_video_watched_rate)
  avg_view_pct       double precision,         -- 0..1 (YouTube averageViewPercentage / 100)
  avg_watch_s        double precision,
  total_watch_s      double precision,
  impression_sources jsonb,
  audience_types     jsonb,
  paid               boolean not null default false,
  paid_views         bigint,
  paid_followers     bigint,
  raw                jsonb,
  unique (post_id, target_age_h)
);

create index if not exists post_metrics_snapshots_age_idx
  on post_metrics_snapshots (platform, target_age_h, captured_at desc);
create index if not exists post_metrics_snapshots_account_idx
  on post_metrics_snapshots (account_id, target_age_h, captured_at desc);

alter table post_metrics_snapshots enable row level security;

insert into scoring_config (key, value, description) values
  ('learning.snapshots',
   '{"enabled": false, "ages_h": [1, 3, 6, 24, 72, 168], "min_tolerance_h": 0.75, "tolerance_frac": 0.25, "bucket_capacity": 90, "bucket_refill_per_s": 0.3}'::jsonb,
   'L1. Each stats pass first takes the due readings (a reading for target age t is due from t until t + max(min_tolerance_h, tolerance_frac * t) hours after posting; youngest post first) into post_metrics_snapshots, then refreshes the posts.* totals. Upload-Post calls are paced by a token bucket (bucket_capacity calls, refilled bucket_refill_per_s per second; the API allows 100 per 5 minutes). Readings left when the inline budget ends are handed to a worker job (stats.collect).')
on conflict (key) do nothing;

insert into settings (key, value, description) values
  ('stats.job_budget_s', '240'::jsonb,
   'Time budget of one stats pass run as a worker job (stats.collect), when the inline pass of the n8n request (stats.collect_budget_s) could not take every due snapshot.')
on conflict (key) do nothing;


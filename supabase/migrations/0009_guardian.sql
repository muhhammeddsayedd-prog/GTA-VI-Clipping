-- M13 guardian, stats, daily report, failover.
-- Additive and idempotent: two nullable columns, one partial unique index, new settings. Existing
-- values are never changed, except that `guardian.failover_grace_s` gains the keys it did not have.

-- Takedown intake is idempotent on the Gmail message id.
alter table takedowns add column if not exists message_id text;
create unique index if not exists idx_takedowns_message_id on takedowns (message_id) where message_id is not null;
create index if not exists idx_takedowns_received on takedowns (received_at desc);

-- The claims check works through the published posts in turn, oldest check first.
alter table posts add column if not exists claim_checked_at timestamptz;

-- Failover now covers all four n8n triggers (the guardian check must not stop when n8n does).
update settings
set value = '{"guardian": 2700, "stats": 14400}'::jsonb || value
where key = 'guardian.failover_grace_s' and jsonb_typeof(value) = 'object';

insert into settings (key, value, description) values
  ('guardian.check_min_age_s', '900'::jsonb, 'Claims check: a post younger than this is not checked yet (the platform may still be processing it).'),
  ('guardian.check_max_posts_per_run', '80'::jsonb, 'Claims check: posts read per pass at most; the rest wait for the next pass (oldest check first).'),
  ('guardian.check_budget_s', '20'::jsonb, 'Claims check: seconds one pass may spend reading posts (the n8n request times out after 30 s).'),
  ('guardian.check_private_posts', 'false'::jsonb, 'Claims check without the owner''s YouTube OAuth token: also judge posts on accounts that post privately (staging), through Upload-Post. Off: they are counted as unverifiable.'),
  ('guardian.mass_missing_min', '5'::jsonb, 'Claims check: when at least this many posts of one platform are gone in one pass...'),
  ('guardian.mass_missing_ratio', '0.8'::jsonb, '...and they are at least this share of the posts read on that platform, it is treated as an account-level event: emergency stop, no creator is blocked.'),
  ('guardian.takedown_body_max_chars', '20000'::jsonb, 'Takedown intake: characters of an email body that are kept and searched for links; the rest is dropped.'),
  ('guardian.takedown_max_per_hour', '60'::jsonb, 'Takedown intake: emails classified per hour at most. Over it an email is stored as open without classification and the owner is warned (protects the AI budget against a mail flood).'),
  ('guardian.handle_match_min_chars', '4'::jsonb, 'Takedown intake: a creator name from an email is matched only when it has at least this many letters or digits.'),
  ('guardian.expected_services', '["scheduler", "worker-express", "worker-normal", "live-manager"]'::jsonb, 'Services whose heartbeat the health check expects. A worker counts as alive when any process of its queue is.'),
  ('guardian.restart_storm_limit', '3'::jsonb, 'Restarts of one service within guardian.restart_storm_window_s that raise a critical alert.'),
  ('guardian.restart_storm_window_s', '3600'::jsonb, 'Window for guardian.restart_storm_limit.'),
  ('guardian.express_slow_count', '3'::jsonb, 'Spike posts in a row slower than guardian.express_slow_s that raise a warning.'),
  ('guardian.disk_warn_pct', '80'::jsonb, 'Warn when the data directory is fuller than this percentage.'),
  ('guardian.spend_pause_s', '3600'::jsonb, 'When a provider is over its daily budget the VOD lane is paused for this long at a time (never past 00:00 UTC).'),
  ('guardian.failover_interval_s', '{"discovery": 600, "guardian": 900, "stats": 7200}'::jsonb, 'While an n8n trigger is missing, the scheduler runs that job itself this often. The hourly slot is run once per slot.'),
  ('guardian.failover_enabled', 'true'::jsonb, 'false: the scheduler only alerts about a missing n8n trigger and does not run the job itself.'),
  ('stats.post_lookback_d', '7'::jsonb, 'Stats: posts published in the last this many days get their numbers refreshed.'),
  ('stats.max_posts_per_run', '150'::jsonb, 'Stats: posts refreshed per pass at most (oldest numbers first).'),
  ('stats.collect_budget_s', '20'::jsonb, 'Stats: seconds one pass may spend on Upload-Post calls (the n8n request times out after 30 s).'),
  ('stats.account_min_interval_s', '1800'::jsonb, 'Stats: an account gets at most one snapshot per this many seconds, so a repeated trigger stores nothing twice.'),
  ('launch.summary_lead_s', '900'::jsonb, 'The scheduler sends one health summary this many seconds before launch.launch_at.'),
  ('reports.top_clips', '3'::jsonb, 'Daily report: number of top clips listed.'),
  ('cleanup.events_days', '30'::jsonb, 'Events older than this many days are deleted by the scheduler.'),
  ('cleanup.candidates_days', '30'::jsonb, 'Discovery candidates older than this many days are deleted by the scheduler.')
on conflict (key) do nothing;

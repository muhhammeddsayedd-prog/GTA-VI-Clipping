-- Staging seed: run once on the staging project after migrations.
-- Staging clips GTA 5, posts only to private test accounts through Upload-Post profile '6stars-staging'.
update settings set value = '"Grand Theft Auto V"'::jsonb where key = 'env.target_game';
update settings set value = '["GTA 5","GTA V","GTA RP","GTA Online"]'::jsonb where key = 'env.search_terms';
update settings set value = '"2013-09-17T00:00:00Z"'::jsonb where key = 'env.game_release_at';
update settings set value = '"2000-01-01T00:00:00Z"'::jsonb where key = 'gate.spoiler_block_until';
update settings set value = '"2026-11-17T00:00:00Z"'::jsonb where key = 'launch.launch_at';
update settings set value = 'true'::jsonb where key = 'launch.dry_run';
update settings set value = '3'::jsonb where key = 'live.max_slots';
update settings set value = '5'::jsonb where key = 'ai.daily_budget_usd';
update settings set value = '"Follow for more (staging test)"'::jsonb where key = 'posting.first_comment';

insert into platform_accounts (id, platform, upload_post_user, handle, lanes, cap_24h, spike_reserve, min_gap_s, privacy, hourly_hours_utc, enabled) values
  ('tiktok_main',    'tiktok',    '6stars-staging', '@6stars.test',  '{hourly,spike}', 14, 4, 900, 'SELF_ONLY', null, true),
  ('tiktok_live',    'tiktok',    '6stars-staging', '@6stars.test',  '{spike}',        14, 14, 900, 'SELF_ONLY', null, false),
  ('instagram_main', 'instagram', '6stars-staging', '@6stars.test',  '{hourly,spike}', 45, 10, 900, null,        null, true),
  ('youtube_main',   'youtube',   '6stars-staging', '@6StarsTest',   '{hourly,spike}', 40, 8,  900, 'private',   null, true)
on conflict (id) do nothing;
-- Note: staging dry_run starts true. Set it to false only for the end-to-end private posting test (M15).

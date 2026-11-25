-- Staging seed: run on the staging project after migrations (safe to re-run).
-- Staging clips GTA 6, like production (owner's order of 2026-11-18: GTA 5 is gone everywhere). It posts through the same Upload-Post profile as production, 'default', on the
-- production accounts (decisions D101, D102): TikTok is SELF_ONLY and YouTube is private. Instagram cannot
-- post privately, so staging stays in dry run except for the single M15 test post.
update settings set value = '"Grand Theft Auto VI"'::jsonb where key = 'env.target_game';
update settings set value = '["GTA 6","GTA VI","Grand Theft Auto VI","GTA6"]'::jsonb where key = 'env.search_terms';
update settings set value = '"2026-11-19T00:00:00Z"'::jsonb where key = 'env.game_release_at';
update settings set value = '"2000-01-01T00:00:00Z"'::jsonb where key = 'gate.spoiler_block_until';
update settings set value = '"2026-11-17T00:00:00Z"'::jsonb where key = 'launch.launch_at';
update settings set value = 'true'::jsonb where key = 'launch.dry_run';
update settings set value = '3'::jsonb where key = 'live.max_slots';
update settings set value = '5'::jsonb where key = 'ai.daily_budget_usd';
update settings set value = '"Follow for more (staging test)"'::jsonb where key = 'posting.first_comment';

-- There is no second TikTok account, so 'tiktok_live' exists but is disabled.
insert into platform_accounts (id, platform, upload_post_user, handle, lanes, cap_24h, spike_reserve, min_gap_s, privacy, hourly_hours_utc, enabled, hourly_min_gap_s) values
  ('tiktok_main',    'tiktok',    'default', '@6starsclipping',  '{hourly,spike}', 14, 4,  900, 'SELF_ONLY', null, true, null),
  ('tiktok_live',    'tiktok',    'default', '@6starsclipping',  '{spike}',        14, 14, 900, 'SELF_ONLY', null, false, null),
  ('instagram_main', 'instagram', 'default', '@6starsclippy',    '{hourly,spike}', 45, 10, 900, null,        null, true, null),
  ('youtube_main',   'youtube',   'default', '@6starsClips-m3x', '{hourly,spike}', 16, 10, 900, 'private', '{0,14,17,19,21,23}', true, 3000)
on conflict (id) do nothing;
-- Note: staging dry_run starts true. Set it to false only for the end-to-end private posting test (M15).

-- Long-form episodes (M20, D122, migration 0058): on in staging, uploads stay private.
update settings set value = 'true'::jsonb where key = 'longform.enabled';
update settings set value = '"private"'::jsonb where key = 'longform.publish_mode';
update settings set value = '72'::jsonb where key = 'longform.pool_hours';
update settings set value = '6'::jsonb where key = 'longform.min_score';

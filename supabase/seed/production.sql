-- Production seed: posting accounts. Run on the production project after migrations (safe to re-run).
-- One Upload-Post profile, 'default', holds every account (decisions D101, D102):
-- TikTok @6starsclipping, Instagram @6starsclippy, YouTube @6starsClips-m3x.
-- There is no second TikTok account, so 'tiktok_live' exists but is disabled.
insert into platform_accounts (id, platform, upload_post_user, handle, lanes, cap_24h, spike_reserve, min_gap_s, privacy, hourly_hours_utc, enabled, hourly_min_gap_s) values
  ('tiktok_main',    'tiktok',    'default', '@6starsclipping',  '{hourly}',        14, 0,  900, 'PUBLIC_TO_EVERYONE', '{0,1,2,11,12,14,15,17,18,19,20,21,22,23}', true, null),
  ('tiktok_live',    'tiktok',    'default', '@6starsclipping',  '{spike}',         14, 14, 900, 'PUBLIC_TO_EVERYONE', null,                                        false, null),
  ('instagram_main', 'instagram', 'default', '@6starsclippy',    '{hourly,spike}',  45, 10, 900, null,                 null,                                        true, null),
  ('youtube_main',   'youtube',   'default', '@6starsClips-m3x', '{hourly,spike}', 16, 10, 900, 'public', '{0,14,17,19,21,23}', true, 3000)
on conflict (id) do nothing;

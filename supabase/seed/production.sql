-- Production seed: posting accounts. Run once on the production project after migrations.
-- Upload-Post profiles: '6stars-main' (TikTok @6starsclips, Instagram, YouTube) and '6stars-live' (TikTok @6starsclips.live).
insert into platform_accounts (id, platform, upload_post_user, handle, lanes, cap_24h, spike_reserve, min_gap_s, privacy, hourly_hours_utc) values
  ('tiktok_main',    'tiktok',    '6stars-main', '@6starsclips',      '{hourly}',        14, 0,  900, 'PUBLIC_TO_EVERYONE', '{0,1,2,11,12,14,15,17,18,19,20,21,22,23}'),
  ('tiktok_live',    'tiktok',    '6stars-live', '@6starsclips.live', '{spike}',         14, 14, 900, 'PUBLIC_TO_EVERYONE', null),
  ('instagram_main', 'instagram', '6stars-main', '@6starsclips',      '{hourly,spike}',  45, 10, 900, null,                 null),
  ('youtube_main',   'youtube',   '6stars-main', '@6StarsClips',      '{hourly,spike}',  40, 8,  900, 'public',             null)
on conflict (id) do nothing;

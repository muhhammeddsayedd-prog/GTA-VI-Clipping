-- YouTube spike-dominant split (2026-11-23, D121). Additive and idempotent: safe to run again. Every row is
-- changed only while it still holds the value migration 0034 set, so an operator's value wins.
--
-- Why: the owner wants spike clips to make up most of youtube_main's 16 posts a day. Spike posts got a median
-- of 946 views against 453 for hourly posts (migration 0034's evidence), and production got 5-11 YouTube
-- spikes a day on 2026-11-20..23, while the 0034 split kept only 4 of the 16 slots for spikes.
--
-- New split: cap_24h stays 16; spike_reserve 4 -> 10, so the hourly lane takes at most 16 - 10 = 6 posts.
-- Spikes may still use any free slot up to the cap (posting.spike_max_per_hour still applies).
-- The unused reserve is NOT released to the hourly lane (posting.reserve_release_* stays unset, owner), so on
-- a day with fewer than 10 spikes YouTube posts fewer than 16.
--
-- Instagram, TikTok, the cap, the hourly gap (3000 s) and every spike rule are not touched.

update platform_accounts
   set spike_reserve = 10
 where id = 'youtube_main'
   and spike_reserve = 4;

-- hourly_hours_utc: 6 hours = the 6 hourly posts, picked from 0034's 12 audience hours: 14 European
-- afternoon, 17 and 19 European evening, 21, 23 and 0 the American afternoon and evening. At least 2 h apart,
-- so the 50-minute hourly gap never blocks one of them.
update platform_accounts
   set hourly_hours_utc = '{0,14,17,19,21,23}'
 where id = 'youtube_main'
   and hourly_hours_utc = '{0,10,13,14,16,17,18,19,20,21,22,23}';

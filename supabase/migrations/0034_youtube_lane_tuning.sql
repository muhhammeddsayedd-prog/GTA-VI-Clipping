-- YouTube lane tuning (2026-11-21, PROGRESS.md "YouTube lane tuning A"). Additive and idempotent: safe to run
-- again. Every row is changed only while it still holds a value the system set, so an operator's value wins.
--
-- Evidence (production, 2026-11-17..21): youtube_main was lowered to the learned-cap floor of 10 after
-- YouTube's per-channel "daily upload limit"; the channel has since passed ID verification (advanced
-- features), so the cap is raised on purpose. Spike posts got a median of 946 views against 453 for hourly
-- posts, so the spike lane is left exactly as it is (spike_reserve stays 4, no change to spike rules).
--
-- Unchanged on purpose: posting.max_clip_age_h (72) and posting.freshness_half_life_h.vod (24).
-- Instagram, TikTok and every spike setting are not touched.

-- 1. The hourly lane's own gap, counted from the account's last HOURLY post only, so a spike post never
--    pushes an hourly slot back through it (posting.min_gap_s, 900 s, still applies to every post).
--    null = no extra hourly gap.
alter table platform_accounts add column if not exists hourly_min_gap_s int
  check (hourly_min_gap_s is null or hourly_min_gap_s >= 0);
comment on column platform_accounts.hourly_min_gap_s is
  'Seconds between two hourly posts of this account, from its last hourly post (spike posts do not count). Null: no extra gap.';

-- 2. youtube_main.
--    cap_24h 16: about 1.6x the 10/day the channel was held to before verification, under Upload-Post's
--      ceiling posting.platform_max_24h.youtube (30), which least() enforces. If YouTube still refuses,
--      on_rate_limit lowers it by 2 again (floor 10).
--    spike_reserve 4 (unchanged): 16 - 4 = 12 hourly posts a day.
--    The cap is raised only from a value the system set: the learned values 10..16 and the seeded 28
--    (migration 0005, still on staging). A cap a human set to anything else above 16 is kept.
update platform_accounts
   set cap_24h = least(16, coalesce((select (value ->> 'youtube')::int from settings
                                      where key = 'posting.platform_max_24h'), 16))
 where id = 'youtube_main'
   and (cap_24h <= 16 or (cap_24h = 28 and spike_reserve = 4));

--    hourly_hours_utc: 12 hours = the 12 hourly posts (cap 16 - reserve 4). Audience 31% North America,
--      25% Northern Europe, 12% Western Europe, 8% Australia/NZ: 16..23 and 0 UTC cover the European evening
--      and the American afternoon/evening; 10, 13, 14 UTC the European midday and the Australian evening.
--      Set only while the account still posts in every hour (null).
update platform_accounts
   set hourly_hours_utc = '{0,10,13,14,16,17,18,19,20,21,22,23}'
 where id = 'youtube_main'
   and hourly_hours_utc is null;

--    hourly_min_gap_s 3000 (50 min), not 3600: slots fire at a random 0..posting.jitter_max_s (300 s) past
--      the hour and published_at is stamped after the upload, so two consecutive hourly slots can be as
--      little as 3600 - 300 - upload time apart; 60 min would make them block each other.
update platform_accounts
   set hourly_min_gap_s = 3000
 where id = 'youtube_main'
   and hourly_min_gap_s is null;

-- 3. Hashtags shared by every platform: three GTA 6 tags; #Gaming dropped (generic, no reach of its own).
--    #Shorts stays in posting.youtube_extra_hashtags. Replaced only from a known earlier value (the GTA 6
--    list of migration 0002, or the GTA 5 list production held until 2026-11-18).
update settings
   set value = '["#GTA6", "#GTAVI", "#GTA6Clips"]'::jsonb
 where key = 'posting.base_hashtags'
   and value in ('["#GTA6", "#GTAVI", "#GTA6Clips", "#Gaming"]'::jsonb,
                 '["#GTA5", "#GTAV", "#GTA", "#Gaming"]'::jsonb);

-- 4. Pinned first comment: the old line got no replies; a question invites them. Replaced only from the
--    known earlier texts (migration 0020 and the text production holds); staging's test text is kept.
update settings
   set value = '"Would you have survived this? 👇 Follow {handle} for daily GTA 6 chaos"'::jsonb
 where key = 'posting.first_comment'
   and value in ('"Follow {handle} for the best GTA 6 moments every hour 🎮⭐"'::jsonb,
                 '"Follow {handle} for the best GTA moments every hour 🎮⭐"'::jsonb);

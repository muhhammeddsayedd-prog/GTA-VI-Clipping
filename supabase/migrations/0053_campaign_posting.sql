-- Clipping campaigns PR 4 (2026-11-23): campaign lanes in posting (D118). Additive and idempotent.
-- The hourly slot fills each account's day from its lanes: campaign clips below their daily minimum first
-- (inside lanes.campaign_hours_utc), then Normal clips (leaving room for open campaign minimums until
-- lanes.return_unused_at_utc), then campaign clips up to their maximum. Two posts of one campaign on one
-- account are at least lanes.campaign_gap_s apart. Campaign posts carry their brief's caption line and tags,
-- no first comment, and the paid-partnership fields Upload-Post supports (YouTube, TikTok).
insert into settings (key, value, description) values
  ('lanes.return_unused_at_utc', '18'::jsonb,
   'From this UTC hour (20:00 Cairo) unused campaign quota goes back to the Normal lane for the rest of the day.'),
  ('lanes.campaign_gap_s', '3600'::jsonb,
   'Seconds between two posts of the same campaign on one account.')
on conflict (key) do nothing;

-- Clipping campaigns PR 2 (2026-11-23): sourcing for campaign streamers (D118).
-- The campaign watcher (scheduler service) asks Twitch and YouTube about campaign streamers by id; live
-- campaign streams get reserved live slots; their VODs and viewer clips jump the queue; a new clip is
-- tagged with its streamer's active campaign. Additive and idempotent.
insert into settings (key, value, description) values
  ('live.campaign_slots', '4'::jsonb,
   'Live slots (of live.max_slots) a live campaign stream may take; a full lane releases its weakest trending session for one. Unused, they stay with trending streams.'),
  ('campaigns.watch_interval_s', '60'::jsonb,
   'Seconds between campaign watcher passes (Twitch Get Streams by user id: one request per pass for up to 100 streamers).'),
  ('campaigns.watch_youtube_s', '180'::jsonb,
   'Seconds between reads of a YouTube campaign streamer''s channel feed (free); a new or live video costs 1 quota unit per 50.'),
  ('campaigns.feed_lookback_h', '48'::jsonb,
   'YouTube feed entries younger than this are checked for live or finished streams.'),
  ('campaigns.vod_priority', '35'::jsonb,
   'Job priority of a campaign VOD (normal VODs 30, live replays 25): campaign VODs jump the queue.'),
  ('campaigns.watcher_stale_s', '300'::jsonb,
   'The guardian alerts when the campaign:watcher heartbeat is older than this while a campaign is active.'),
  ('twitch_clips.campaign_first', 'true'::jsonb,
   'Read viewer clips of campaign streamers by broadcaster before the game-wide list, inside twitch_clips.daily_cap.'),
  ('lanes.campaign_hours_utc', '{"default": [8,9,10,11,12,13,14,15,16,17,18,19,20,21], "youtube": [8,9,10,11,12,13,14,15,16,17]}'::jsonb,
   'UTC hours in which campaign posts go out (10:00-24:00 Cairo; YouTube 10:00-20:00). A spike clip outside them is made as a Normal clip.')
on conflict (key) do nothing;

create index if not exists idx_live_sessions_campaign on live_sessions (campaign_id) where campaign_id is not null;
create index if not exists idx_videos_campaign on videos (campaign_id) where campaign_id is not null;

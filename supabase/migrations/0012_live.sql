-- Live lane (M14): recorder, rolling buffer, blocked-source back-off, express cut and replay hand-off.
-- Additive and idempotent: new keys only, inserted with `on conflict do nothing`. The code falls back to
-- the same values (`sixstars.live.config.DEFAULTS`) on a database where this migration is not applied.
insert into settings (key, value, description) values
  ('live.poll_s', '15'::jsonb, 'Seconds between two looks of the live manager at the live_sessions table (start assigned sessions, stop released ones).'),
  ('live.max_height', '1080'::jsonb, 'Highest video height the live recorder takes (streamlink picks the best stream at or below it). Lower it to 720 to halve disk and bandwidth per slot.'),
  ('live.reconnect_backoff_s', '10'::jsonb, 'Seconds the recorder waits before it reconnects after a stream drop.'),
  ('live.reconnect_reset_s', '300'::jsonb, 'A reconnected recorder that runs this long clears the count of failed attempts (live.max_reconnects counts failures in a row).'),
  ('live.start_timeout_s', '60'::jsonb, 'Seconds a recorder start may take to deliver the first stream data before it counts as failed.'),
  ('live.stall_timeout_s', '45'::jsonb, 'Seconds without new stream data after which a running recorder is restarted.'),
  ('live.ad_break_max_s', '300'::jsonb, 'Longest Twitch ad break the recorder waits out without data (streamlink skips the ad segments) before it treats the silence as a stalled stream.'),
  ('live.stop_timeout_s', '8'::jsonb, 'Seconds a recorder gets to close its last segment when a session stops, before it is killed.'),
  ('live.blocked_backoff_s', '1800'::jsonb, 'Seconds a platform that refuses this machine (bot check, rate limit) is left alone before one stream probes it again. The first refusal waits a sixth of it.'),
  ('live.blocked_notify_after', '2'::jsonb, 'Refused stream starts in a row after which a blocked live source is reported once (warn); one info follows when it records again.'),
  ('live.source_health', 'null'::jsonb, 'Written by code: refused starts in a row per live source and the end of its back-off.'),
  ('live.max_buffer_bytes', '3000000000'::jsonb, 'Hard cap of one slot''s rolling buffer in bytes: the oldest segments are deleted beyond it, whatever live.buffer_s says.'),
  ('live.min_free_bytes', '8000000000'::jsonb, 'No new live session starts while the data volume has fewer free bytes than this (running ones continue inside their buffers).'),
  ('live.cut_settle_s', '4'::jsonb, 'Seconds waited after the end of a window before it is cut from the buffer, so the stream data for its last seconds has arrived.'),
  ('live.cut_wait_max_s', '70'::jsonb, 'Longest wait for a window that ends in the future before it is cut anyway.'),
  ('live.express_pad_wait_s', '6'::jsonb, 'Seconds of context after the clip window an express (S-tier) cut waits for at most; the rest of spikes.context_pad_s is given up for speed.'),
  ('live.cut_keep_s', '2700'::jsonb, 'Seconds a cut file is kept for its render job (and the job''s retries) before the janitor deletes it.'),
  ('live.anchor_span_s', '300'::jsonb, 'Segments within this many seconds of a window are used to align stream timestamps with the wall clock.'),
  ('live.replay_delay_s', '900'::jsonb, 'Seconds after a YouTube stream ends before its replay scan runs (the chat replay needs time to appear).'),
  ('live.replay_find_interval_s', '600'::jsonb, 'Seconds between two look-ups of the Twitch archive of an ended stream.'),
  ('live.replay_find_max_s', '7200'::jsonb, 'How long the Twitch archive of an ended stream is looked for before giving up.'),
  ('live.priority_express', '100'::jsonb, 'Job priority of an S-tier live spike (express.render, queue express).'),
  ('live.priority_a', '60'::jsonb, 'Job priority of an A-tier live spike (clip.render, queue normal, hourly lane).'),
  ('live.priority_b', '40'::jsonb, 'Job priority of a B-tier live spike (clip.render, queue normal, hourly lane).'),
  ('live.ab_clips', 'true'::jsonb, 'false = only S-tier live spikes become clips; A and B are left to the replay pass after the stream.')
on conflict (key) do nothing;

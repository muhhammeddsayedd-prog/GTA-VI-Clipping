-- fix-stale-processing: a video left in status processing by a restarted worker held a vod.max_concurrent
-- slot and stalled the whole VOD lane. Settings only, additive and idempotent: existing values are never
-- changed.
insert into settings (key, value, description) values
  ('vod.orphan_grace_s', '300'::jsonb, 'A video in status processing with no running vod.process or replay.scan job stops counting as in work, and is put back to new, once it was not updated for this many seconds (covers runs started by hand with the CLI, which have no job).'),
  ('vod.recovery_interval_s', '60'::jsonb, 'Seconds between scheduler passes that put orphaned processing videos back to new.'),
  ('vod.concurrency_retry_max_s', '600'::jsonb, 'Longest wait of a vod.process job that keeps finding no free slot: the wait doubles from vod.concurrency_retry_s up to this.'),
  ('vod.concurrency_retry_jitter', '0.2'::jsonb, 'Share (0..1) by which the wait for a free slot is randomly shortened, so deferred jobs do not all wake at once.'),
  ('vod.worker_fresh_s', '90'::jsonb, 'A worker of the normal queue counts as live for the VOD concurrency limit when its heartbeat (written every 30 s) is not older than this many seconds.')
on conflict (key) do nothing;

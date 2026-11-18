-- R12 holding window for the spike lane (owner's request, 2026-11-18): settings only, idempotent.
--
-- Spike clips used to post the moment they passed the gate, so on a busy stream the first 7s of the hour
-- took the two spike posts and a 9 or 10 that came a few minutes later went to the hourly queue. Now the
-- spike lane waits posting.spike_hold_s after the first waiting spike clip was approved and then decides
-- the whole batch best rank first; the release never comes later than 2 minutes before any clip of the
-- batch reaches posting.spike_max_age_s. Absent or 0: every spike clip posts as soon as it is ready.
insert into settings (key, value, description) values
  ('posting.spike_hold_s', '420'::jsonb,
   'Spike-lane holding window (R12): seconds the spike lane waits after the first waiting spike clip was approved, then posts the best-ranked clip of the batch first. 0 posts each spike clip as soon as it is ready.')
on conflict (key) do nothing;

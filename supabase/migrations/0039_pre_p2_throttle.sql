-- AI cost plan A5b (D117, 2026-11-23): queue-aware supply throttle before the clip writer (P2), LOG ONLY.
-- In `log` mode every hourly-lane candidate gets a scoring_log row (stage factory.pre_p2_throttle,
-- decision would_skip / keep, with its rank and the queue's) and still goes on to P2: nothing changes.
-- The owner reviews what the would-skip clips became before `enforce` is set.

insert into settings (key, value, description) values
  ('factory.pre_p2_throttle', '"log"'::jsonb,
   'Queue throttle before the clip writer: "off", "log" (record would_skip/keep in scoring_log, change nothing) or "enforce" (reject a would-skip with reason pre_p2_throttle). Hourly lane only; tier S and clips without a chat percentile are never throttled.'),
  ('factory.pre_p2_queue_min', '40'::jsonb,
   'The throttle compares a candidate with the queue only when at least this many approved hourly clips can still post before posting.max_clip_age_h.')
on conflict (key) do nothing;

-- AI cost plan A5a (D117, 2026-11-23): checks before the clip writer (P2). With the switch on, a clip the gate
-- is certain to reject is closed before P2 is paid for: the creator's daily cap or tier, or music in every
-- possible cut of the window over the profile limit. The checks call the gate's own functions with the
-- gate's own settings (gate.hourly / gate.express, gate.music_min_run_frames, gate.auto_creator_daily_cap);
-- they change no limit. Seeded off: it is switched on per environment after a check on staging.

insert into settings (key, value, description) values
  ('factory.pre_p2_checks', 'false'::jsonb,
   'Before the clip writer (P2), reject a clip the gate would certainly reject (creator daily cap or tier, music in every possible cut over the profile limit), with reason "pre_p2: ...". Uses the gate''s own checks and settings. Off: P2 is asked for every clip.')
on conflict (key) do nothing;

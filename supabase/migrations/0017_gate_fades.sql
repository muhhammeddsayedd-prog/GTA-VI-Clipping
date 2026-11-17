-- GTA's own death and mission-failed screens fade to black and hold a still frame for about 3 seconds.
-- The 2-second limits rejected good clips for that (decision D108). 4 seconds still fails dead video.
-- Idempotent: only the seeded values change.
update settings
set value = jsonb_set(jsonb_set(value, '{max_black_s}', '4.0'::jsonb), '{max_freeze_s}', '4.0'::jsonb)
where key = 'gate.checks'
  and (value ->> 'max_black_s')::numeric = 2.0
  and (value ->> 'max_freeze_s')::numeric = 2.0;

-- D116: the hourly lane keeps one clip apart across platforms (2026-11-21). Additive and idempotent: the keys
-- are added only where they are missing, so an operator's value is kept.
--
-- Why: by D18 every platform took the top clip of the hourly slot, so YouTube and Instagram published the
-- same clip within about five minutes of each other; Instagram cut distribution for a full day on
-- 2026-11-20. The spike lane is not affected (spike posts stay immediate on every platform).

insert into settings (key, value, description) values
  -- 45 min: in the next hourly slot (60 min later) the other platform may take the clip after all.
  ('posting.cross_platform_min_gap_s', '2700'::jsonb,
   'D116, hourly lane only: an hourly post does not take a clip that another platform posted less than this many seconds ago; the account takes its next best clip. 0: off (D18, every platform the same clip).'),
  -- YouTube first: it is the lane being tuned, so it gets the best clip of the slot.
  ('posting.platform_priority', '["youtube", "tiktok", "instagram"]'::jsonb,
   'D116: the order in which platforms pick their clip in an hourly slot (unlisted platforms last, then by account id). Missing or malformed: account id order.')
on conflict (key) do nothing;

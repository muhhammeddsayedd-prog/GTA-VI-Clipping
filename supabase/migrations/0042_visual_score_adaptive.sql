-- AI cost plan A2 (D117, 2026-11-23).
-- 1. Adaptive P9V sampling: the first visual score decides whether the other ai.visual_score_samples are
--    asked. Only a first score from ai.visual_score_margin below the hourly pass line up to the line itself
--    is asked again (42.9% of hourly clips in production landed within one point of the line). The frames are
--    cached with the first ask, so the re-asks read them from the prompt cache.
-- 2. The posting rank no longer starts from the P9V score: on approved clips it did not predict views
--    (Spearman 0.04, n=198). The gate still passes and fails on it.

insert into settings (key, value, description) values
  ('ai.visual_score_adaptive', 'true'::jsonb,
   'P9V: ask the further ai.visual_score_samples only when the first score is from ai.visual_score_margin below the hourly pass line up to the line. Off: every sample is asked.'),
  ('ai.visual_score_margin', '1'::jsonb,
   'P9V adaptive sampling: how far below the pass line a first score still gets the further samples.'),
  ('posting.rank_uses_visual_score', 'false'::jsonb,
   'Posting rank: start from the visual judge''s score (true) or from the clip writer''s score (false, the visual score did not predict views). The gate decides on the visual score either way.')
on conflict (key) do nothing;

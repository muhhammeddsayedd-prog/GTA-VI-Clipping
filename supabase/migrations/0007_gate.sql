-- Quality gate (M07): settings only, idempotent. Limits that docs/10 gives as fixed numbers become
-- settings, plus the spoiler keyword list (D20), the stricter music rule that replaces the YouTube
-- pre-check while it cannot run (D103) and the duplicate thresholds.
insert into settings (key, value, description) values
  ('gate.spoiler_terms',
   '["spoiler", "spoilers", "ending", "endings", "true ending", "secret ending", "final mission", "last mission", "final boss", "final heist", "final choice", "final cutscene", "epilogue", "post credits", "credits scene", "plot twist", "story twist", "character death", "main character dies", "who dies", "death scene", "killed off", "betrayal", "betrays"]'::jsonb,
   'Words and phrases that may never appear in a hook, title, caption or hashtag (whole words, any case).'),
  ('gate.strict_max_music_ratio', '0.0'::jsonb,
   'Music limit of a risky hourly clip when the YouTube pre-check cannot run (D103). Never above gate.hourly.max_music_ratio.'),
  ('gate.music_min_run_frames', '1'::jsonb,
   'Music frames in a run shorter than this many seconds are ignored by the gate. 1 counts every flagged frame.'),
  ('gate.checks',
   '{"max_black_s": 2.0, "max_freeze_s": 2.0, "freeze_noise": 0.003, "max_subtitle_drift_s": 0.3, "max_file_mb": 100, "width": 1080, "height": 1920, "duration_tolerance_s": 1.0}'::jsonb,
   'Technical gate limits: black or frozen picture of this length fails, subtitle drift, file size and frame size, allowed difference between file length and clip length plus end card.'),
  ('gate.duplicate',
   '{"source_pad_s": 30, "topic_overlap_same_creator": 0.5, "topic_overlap_any_creator": 0.8, "transcript_overlap": 0.5}'::jsonb,
   'Duplicate detection: padding around a source range, shared topic words (same creator: share of the shorter topic; any creator: Jaccard), shared three-word sequences of the transcripts. 0 switches a rule off.'),
  ('precheck.max_status_checks', '3'::jsonb,
   'How many times the pre-check status is read while YouTube is still processing before the clip is rejected as inconclusive.')
on conflict (key) do nothing;

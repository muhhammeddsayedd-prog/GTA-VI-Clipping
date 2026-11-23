-- Cover engine C1 (2026-11-23): cover-ready video, stage 0 of docs/29a (spec docs/29, probe docs/29b).
-- Additive and idempotent. The flag starts OFF in every environment; staging turns it on by hand
-- (docs/CHANGELOG-scoring.md), production only through the docs/29a section 10 gate.

-- 1. The flag. Rollback = set enabled to false (the factory reads it within 30 s, no redeploy).
insert into scoring_config (key, value, description) values
  ('cover.video_ready',
   '{"enabled": false, "facecam_h": 768, "face_target": 0.40, "face_min_crop_px": 180, "face_samples": 5, "open_s": 2.0, "peak_pad_s": 1.0, "phrase_words": 6, "phrase_chars": 26, "dark_luma": 50, "dark_gamma": 1.15, "dark_s": 1.5, "dark_fade_s": 0.5}'::jsonb,
   'Cover engine stage 0. Layout C draws the facecam facecam_h px tall (of 1920; 672 when off). The facecam crop of layouts A and C is placed on the streamer''s face (MediaPipe, face_samples frames) so it fills face_target of the panel height, never cropping under face_min_crop_px source px. In the first open_s seconds and peak_pad_s around the peak (cut anchor, else writer payoff, else busiest chat second), chunked captions show one still phrase of at most phrase_words words / phrase_chars characters on one row. Gameplay whose mean luma over the first dark_s seconds is under dark_luma gets gamma dark_gamma there, easing back over dark_fade_s. Never changes length, cut points or audio.')
on conflict (key) do nothing;

-- 2. What the render used, for the cover engine (C3 mines frames around the peak) and the gate (the
--    gameplay band sits lower under a taller facecam). Null when the flag was off.
alter table clips add column if not exists peak_s numeric;
alter table clips add column if not exists peak_source text;
alter table clips add column if not exists facecam_h int;

comment on column clips.peak_s is 'Cover-ready video: the clip''s peak in clip time (seconds).';
comment on column clips.peak_source is 'Where peak_s came from: anchor | payoff | chat.';
comment on column clips.facecam_h is 'Cover-ready video: layout C facecam height on the 1080x1920 canvas.';

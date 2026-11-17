-- Visual scoring: settings only, additive, idempotent.
--
-- The clip writer (prompt P2) scores a clip from its transcript and chat sample before it is rendered.
-- In game clips the payoff is usually on screen and not in the words, so on staging every one of 21
-- rendered clips scored 4 to 6 and none passed gate.hourly.min_score = 7, including clips whose whole
-- point is visual (a jump without a parachute, a car launched over the city). With ai.visual_score on,
-- the gate scores the rendered clip again before it decides (prompt P9V, operation p9_visual_score):
-- frames of the clip, the hook, the subtitle lines and the chat reaction, on the same four scales and
-- bands. clips.score becomes that score; clips.score_parts keeps the writer's score and parts under
-- "writer" and the visual ones under "visual". If the call fails, the writer's score stands.
-- No gate threshold is changed. Measurements: development-plan/progress-notes/visual-scoring.md.
insert into settings (key, value, description) values
  ('ai.visual_score', 'true'::jsonb,
   'Score the rendered clip from its frames before the gate decides (prompt P9V); clips.score becomes that score. false: the clip writer''s score is judged, as before.'),
  ('ai.visual_score_model', '"writer"'::jsonb,
   'Which model scores the rendered clip: "fast" (ai.model_fast) or "writer" (ai.model_writer).'),
  ('ai.visual_score_frames', '8'::jsonb,
   'Frames of the rendered clip the visual score sees, 4 to 8: the four P5 frames plus extra ones around the chat peak and near the end of the clip.'),
  ('ai.visual_score_samples', '3'::jsonb,
   'How many times the rendered clip is scored on the hourly lane; the median is stored. Use an odd number. Express clips are always scored once.')
on conflict (key) do nothing;

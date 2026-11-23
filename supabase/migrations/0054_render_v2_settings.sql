-- Render v2 R1 (development-plan/M19, decision D119, 2026-11-23). Idempotent: keys are added only where
-- missing, styles are switched off and affinities removed only where still present.
--
-- 1. The four settings of Render v2 (docs/16 "Clip factory: Render v2"). factory.layout_v2.enabled is the
--    master switch and starts off everywhere: the renderer draws the frames of factory.styles until it is
--    turned on (staging from R2, production in R7 with the owner's OK). factory.layout_v2.scaler is read
--    already (R0): every crop is scaled with Lanczos from this migration on, in every frame.
--    factory.text_v2, facecam.detect and gate.render_v2 are read from R2-R5 on.
insert into settings (key, value, description) values
  ('factory.layout_v2',
   '{"enabled": false, "cam_h": 720, "g0_game_h": 1350, "g0_game_y": 285, "max_slide": 0.25, "seam_y": 720,
     "safe_right_x": 920, "safe_bottom_y": 1540, "cam_face_center_y": 0.45, "cam_face_h_min": 0.40,
     "cam_face_h_max": 0.60, "cam_fill_blur": 30, "cam_fill_darken": 0.30, "cam_unsharp": 0.5,
     "scaler": "lanczos"}'::jsonb,
   'Render v2 frames (D119). enabled: draw every clip as G (verified facecam: cam panel 1080 x cam_h on top, gameplay below) or G0 (no cam: g0_game_h of gameplay at g0_game_y over a blurred copy); off: the frames of factory.styles. max_slide: share of the source width the gameplay crop may move from the centre to stay clear of the facecam and the streamer''s overlays. seam_y / safe_right_x / safe_bottom_y: text places (R4). cam_face_*: face height share and centre in the cam panel; cam_fill_blur / cam_fill_darken: the fill around a small cam; cam_unsharp: sharpening of the cam (R2). scaler: ffmpeg scaler of every crop (also with enabled off). Pixels on the 1080x1920 canvas.'),
  ('factory.text_v2',
   '{"font": "Anton", "size_min": 110, "size_max": 130, "outline_px": 9, "color": "#FFFFFF",
     "key_color": "#FFD400", "max_words": 3, "hook_s": 2.5, "mark_text": "6 Stars Clips", "mark_size": 34,
     "mark_opacity": 0.7, "mark_x_right": 1040, "mark_y": 740, "credit_size": 30, "credit_x": 40,
     "credit_y": 740}'::jsonb,
   'Render v2 text (D119, R4): one text element at a time on the seam. font / size_min / size_max / outline_px / color / key_color: captions of 1..max_words words; hook_s: the hook line at the start; mark_*: the "6 Stars Clips" mark at the top right of the gameplay; credit_*: the creator credit at its top left. Read from R4 on.'),
  ('facecam.detect',
   '{"min_confidence": 0.75, "check_frames": 5, "min_face_hits": 3, "min_face_share": 0.25,
     "detector_score": 0.6, "cache_scope": "source", "creator_prior_days": 7}'::jsonb,
   'Render v2 facecam verification (D119, R3): the cam box of the layout prompt is used only with confidence >= min_confidence and a face (detector score >= detector_score) in >= min_face_hits of check_frames frames, at least min_face_share of the box height. cache_scope: cache per source; creator_prior_days: how long the creator''s last accepted box is tried first. Read from R3 on.'),
  ('gate.render_v2',
   '{"face_frames": 8, "face_hit_share": 0.8, "face_min_panel_share": 0.30, "min_game_share_g": 0.60,
     "min_game_share_g0": 0.70, "max_black_band": 0.05}'::jsonb,
   'Render v2 quality gate (D119, R5): a face in >= face_hit_share of face_frames frames of the cam panel, never touching its edge, at least face_min_panel_share of its height; gameplay at least min_game_share_g (G) / min_game_share_g0 (G0) of the frame; no black band taller than max_black_band. Read from R5 on.')
on conflict (key) do nothing;

-- 2. The frames Render v2 retires: hype_montage (layout M), headline_card (H) and pov_fullframe (P) are
--    switched off (kept in the catalogue so a clip already planned with them still renders) and leave the
--    style director's moment-type affinity.
do $$
declare
  retired constant text[] := array['hype_montage', 'headline_card', 'pov_fullframe'];
  styles jsonb;
  director jsonb;
  kind text;
  style_id text;
begin
  select value into styles from settings where key = 'factory.styles';
  if styles is not null and jsonb_typeof(styles) = 'object' then
    foreach style_id in array retired loop
      if jsonb_typeof(styles -> style_id) = 'object' then
        styles := jsonb_set(styles, array[style_id, 'enabled'], 'false'::jsonb);
      end if;
    end loop;
    update settings set value = styles where key = 'factory.styles' and value is distinct from styles;
  end if;

  select value into director from settings where key = 'factory.style_director';
  if director is not null and jsonb_typeof(director -> 'affinity') = 'object' then
    for kind in select jsonb_object_keys(director -> 'affinity') loop
      if jsonb_typeof(director -> 'affinity' -> kind) = 'object' then
        director := jsonb_set(director, array['affinity', kind], (director -> 'affinity' -> kind) - retired);
      end if;
    end loop;
    update settings set value = director
     where key = 'factory.style_director' and value is distinct from director;
  end if;
end $$;

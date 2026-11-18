-- The editing style of the clips (docs/27-style-reference.md): settings only, idempotent.
--
-- The look is modelled on GTA short clips that reached millions of views: captions of one or two words in
-- heavy capitals on the seam, the hook as a title bar for the whole clip, hard punch-ins to the facecam on
-- the loudest reactions, a step zoom every 2.5 s, whoosh/impact on every punch-in and a bass hit with an
-- emoji on the payoff, dense compressed audio, and no end card (the clips end on the payoff so they loop).
--
-- 1. factory.style: new. Without it the factory keeps the classic look (StyleRules.classic()).
-- 2. factory.accent_color #FFD400 -> #FF2BD6 (magenta, the accent of the references; keywords and the
--    progress bar). factory.end_card_s 1.5 -> 0 (the gate reads the same setting, so the expected file
--    length follows). factory.loudness_lufs -14 -> -12 (with the compressor; true peak stays -1.5 dB).
--    Each is changed only where it still holds the seeded value, so a deliberate later choice is kept.
insert into settings (key, value, description) values
  ('factory.style',
   '{"caption_words": 2, "caption_chars": 14, "caption_font": "Anton", "caption_size": 150, "caption_caps": true, "hook_mode": "title_bar", "punchin_max": 4, "punchin_zoom": 1.25, "punchin_len_s": 1.2, "punchin_min_gap_s": 2.5, "punchin_db": 5, "zoomcut_every_s": 2.5, "zoomcut_scale": 1.08, "sfx_gain_db": -10, "cut_sfx": true, "payoff_fx": true, "compress": true}'::jsonb,
   'Editing style (docs/27). caption_words/caption_chars: most words/characters per caption (0 words: whole lines, classic). caption_font/caption_size/caption_caps: caption font, size on 1080x1920, upper case. hook_mode: box (factory.hook_seconds) or title_bar (whole clip). punchin_*: up to punchin_max cuts to a full-frame close-up, punchin_len_s long, zoom punchin_zoom, where the audio is punchin_db dB over the clip median, punchin_min_gap_s apart (0: off). zoomcut_every_s/zoomcut_scale: gameplay alternates 100%/scale (0: classic slow tail zoom). sfx_gain_db: level of every sound effect. cut_sfx: whoosh+impact on punch-ins. payoff_fx: emoji+bass hit at the writer''s payoff. compress: compressor before loudness normalisation. Missing: classic look.')
on conflict (key) do nothing;

update settings
   set value = '"#FF2BD6"'::jsonb
 where key = 'factory.accent_color'
   and value = '"#FFD400"'::jsonb;

update settings
   set value = '0'::jsonb
 where key = 'factory.end_card_s'
   and value = '1.5'::jsonb;

update settings
   set value = '-12'::jsonb
 where key = 'factory.loudness_lufs'
   and value = '-14'::jsonb;

-- Design styles, Phase B (development-plan/M18, decisions D113-D115). Idempotent.
--
-- 1. clips.moment_type and clips.style_texts: the clip writer's kind of moment and the on-screen texts of
--    the styles (title_line, headline, question, pov_line).
-- 2. factory.styles gains cam_top_question (red question title over the gameplay, reference R05),
--    headline_card (news card: brand header, headline, the stream as a 4:3 crop, reference R09) and
--    pov_fullframe (tall gameplay crop with a POV line, reference R08). Each requires the writer's text it
--    shows. cam_top_title now shows the writer's title_line (the hook when there is none).
--    Added only where they are still missing, so a deliberate later edit is kept.
-- 3. factory.style_director gains the affinity of each kind of moment for the styles (weight multipliers).

alter table clips add column if not exists moment_type text;
alter table clips add column if not exists style_texts jsonb;

update settings
   set value = jsonb_set(value, '{cam_top_title,rules,title_source}', '"title_line"'::jsonb)
 where key = 'factory.styles'
   and value ? 'cam_top_title'
   and not (value -> 'cam_top_title' -> 'rules' ? 'title_source');

update settings
   set value = '{
      "cam_top_question": {"enabled": true, "weight": 1, "lanes": ["spike", "hourly"], "accounts": "*", "requires": ["question"],
        "rules": {"caption_words": 0, "captions": false, "hook_mode": "title_bar", "title_whole_clip": true, "punchin_max": 0, "zoomcut_every_s": 0, "cut_sfx": false, "payoff_fx": false, "compress": true, "sfx_gain_db": -10, "facecam_top": true, "title_source": "question", "title_look": "question"}},
      "headline_card": {"enabled": true, "weight": 1, "lanes": ["spike", "hourly"], "accounts": "*", "requires": ["headline"],
        "rules": {"caption_words": 0, "captions": false, "hook_mode": "title_bar", "title_whole_clip": true, "punchin_max": 0, "zoomcut_every_s": 0, "cut_sfx": false, "payoff_fx": false, "compress": true, "sfx_gain_db": -10, "frame": "headline_card", "title_source": "headline", "watermark": false}},
      "pov_fullframe": {"enabled": true, "weight": 1, "lanes": ["spike", "hourly"], "accounts": "*", "requires": ["pov_line"],
        "rules": {"caption_words": 0, "captions": false, "hook_mode": "title_bar", "title_whole_clip": true, "punchin_max": 0, "zoomcut_every_s": 0, "cut_sfx": false, "payoff_fx": false, "compress": true, "sfx_gain_db": -10, "frame": "pov", "title_source": "pov_line"}}
    }'::jsonb || value
 where key = 'factory.styles'
   and not (value ? 'headline_card' or value ? 'pov_fullframe' or value ? 'cam_top_question');

update settings
   set value = value || '{"affinity": {
      "reaction": {"cam_top_viral": 3},
      "discovery": {"headline_card": 3, "cam_top_question": 3},
      "action": {"pov_fullframe": 3, "cam_top_viral": 2},
      "story": {"cam_top_title": 3},
      "npc_funny": {"cam_top_title": 2, "pov_fullframe": 2},
      "fail": {"cam_top_viral": 2, "pov_fullframe": 2}
    }}'::jsonb
 where key = 'factory.style_director'
   and not (value ? 'affinity');

update settings
   set description = description || ' affinity: per moment_type of the clip writer, a weight multiplier per style (missing: 1).'
 where key = 'factory.style_director'
   and description not like '%affinity%';

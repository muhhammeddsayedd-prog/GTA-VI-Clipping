-- The hype montage (development-plan/M18 Phase E, reference R10). Idempotent.
--
-- factory.styles gains hype_montage: gameplay above the facecam in a 3:4 letterbox with a soft fade over the
-- seam, cuts to B-roll from the footage around the moment every other 3 s (the busiest stretches outside
-- the clip), two-tier kinetic captions on the seam, a whoosh on each cut and the payoff emoji, no title.
-- Hourly only; it needs a facecam and enough footage around the clip (requires facecam, broll).
-- factory.style_director's affinity favours it for discoveries and reactions. Added only where missing.

update settings
   set value = value || '{"hype_montage": {"enabled": true, "weight": 1, "lanes": ["hourly"], "accounts": "*", "requires": ["facecam", "broll"],
     "rules": {"frame": "montage", "montage_every_s": 3.0, "captions": true, "caption_tiers": true, "caption_words": 2, "caption_chars": 16, "caption_font": "Anton", "caption_size": 120, "caption_caps": true, "show_title": false, "hook_mode": "title_bar", "punchin_max": 0, "zoomcut_every_s": 0, "cut_sfx": true, "payoff_fx": true, "compress": true, "sfx_gain_db": -12}}}'::jsonb
 where key = 'factory.styles'
   and not (value ? 'hype_montage');

update settings
   set value = jsonb_set(value, '{affinity,discovery,hype_montage}', '3'::jsonb)
 where key = 'factory.style_director'
   and value -> 'affinity' ? 'discovery'
   and not (value -> 'affinity' -> 'discovery' ? 'hype_montage');

update settings
   set value = jsonb_set(value, '{affinity,reaction,hype_montage}', '2'::jsonb)
 where key = 'factory.style_director'
   and value -> 'affinity' ? 'reaction'
   and not (value -> 'affinity' -> 'reaction' ? 'hype_montage');

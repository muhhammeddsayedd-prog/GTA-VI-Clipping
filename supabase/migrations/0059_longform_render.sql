-- Long-form episodes L3 (development-plan/M20, decision D122, 2026-11-25). Idempotent: keys are added only
-- where missing; longform.render gets `preset` and `bufsize` only where they are not set yet.
--
-- longform.auto_build: off = a planned episode waits (plan-only review, the L2 stop); on = its moments are
-- downloaded and rendered right after planning. longform.cut: how a clip is found in its re-downloaded
-- footage and how the cut is widened and snapped to pauses. longform.style: the 16:9 overlay.
insert into settings (key, value, description) values
  ('longform.auto_build', 'false'::jsonb,
   'Long-form episodes (D122): on = download and render the moments right after planning; off = plan only.'),
  ('longform.cut',
   '{"pause_s": 0.4, "snap_before_s": 3.0, "snap_after_s": 3.0, "lead_share": 0.6, "match_tolerance_s": 0.6,
     "min_matches": 3, "max_shift_s": 12.0}'::jsonb,
   'Long-form episodes: the clip is found in its re-downloaded footage by words (min_matches agreeing within match_tolerance_s, at most max_shift_s from the expected place); the cut is widened toward the moment length (lead_share of the extra before the clip) and its ends snapped to the nearest speech pause (>= pause_s) within snap_before_s / snap_after_s.'),
  ('longform.style',
   '{"caption_font": "Inter", "caption_size": 54, "caption_margin_v": 70, "caption_max_chars": 42,
     "caption_max_lines": 2, "caption_gap_s": 0.35, "badge_font": "Anton", "badge_size": 84, "badge_s": 2.0,
     "credit_font": "Inter", "credit_size": 34, "credit_s": 3.0, "mark_text": "6 Stars Clips", "mark_size": 26,
     "mark_opacity": 0.45, "margin": 48, "title_font": "Anton", "title_size": 96,
     "cold_open_text": "#1 is insane. Stay till the end.", "end_card_text": "New GTA 6 TOP moments every day",
     "thumb_text_size": 150}'::jsonb,
   'Long-form episodes: the 16:9 overlay. Captions bottom-centre (max chars per line, lines, a new caption after caption_gap_s of silence); the #N badge top-left for badge_s; the creator credit bottom-left for credit_s; the brand mark top-right for the whole segment; title_* / cold_open_text / end_card_text: the cold open line and the end card; thumb_text_size: the thumbnail words (1280x720). Sizes in pixels of the canvas.')
on conflict (key) do nothing;

update settings
   set value = value || '{"preset": "veryfast", "bufsize": "16M"}'::jsonb
 where key = 'longform.render'
   and jsonb_typeof(value) = 'object'
   and not (value ? 'preset');

-- Two-phase build: every moment is downloaded and aligned first (source_path + words kept), then the
-- positions are renumbered without gaps (dropped moments leave none) and only then the segments are rendered,
-- because the #N badge is burned into the segment.
alter table episode_items add column if not exists source_path text;
alter table episode_items add column if not exists words jsonb;

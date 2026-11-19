-- Story cuts and one-word captions (development-plan/M18 Phase C, decision D113). Idempotent.
--
-- 1. clips.version ('short' | 'story') and clips.parent_clip_id: a story cut is its own clip row, rendered
--    right after its short clip from the same source and writer reply, and gated and posted like any clip.
--    One story per short clip.
-- 2. factory.story: when a story cut is asked for (hourly, normal-mode renders of VOD and live sources),
--    its length limits, and how much wider the source window is for it.
-- 3. posting.sibling_gap_h: two clips of one family go to the same account at least this many hours apart.
--    posting.max_duration_s: the longest clip each platform takes (TikTok API 10 min and Instagram Reels
--    15 min per docs.upload-post.com/api/video-requirements; YouTube Shorts 3 min per YouTube Help).
-- 4. factory.styles gains cam_top_wordcap (reference R06: one or two heavy words on the facecam's bottom
--    edge, the spoken one in yellow, no title); story moments favour it.

alter table clips add column if not exists version text not null default 'short';
alter table clips add column if not exists parent_clip_id uuid references clips(id);
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'clips_version_check') then
    alter table clips add constraint clips_version_check check (version in ('short', 'story'));
  end if;
end $$;
create unique index if not exists idx_clips_one_story on clips (parent_clip_id) where version = 'story';

insert into settings (key, value, description) values
  ('factory.story',
   '{"enabled": true, "min_s": 60, "max_s": 150, "pad_before_s": 90, "pad_after_s": 30, "sources": ["vod", "live"]}'::jsonb,
   'Story cuts (development-plan/M18 Phase C). enabled; min_s/max_s: length of the story cut; pad_before_s/pad_after_s: extra source seconds before/after the moment for hourly, normal-mode renders of the listed sources (vod, live), so the writer can find a story; express renders never get one.'),
  ('posting.sibling_gap_h', '12'::jsonb,
   'Hours between two clips of one family (a short clip and its story cut) on the same account (M18 Phase C).'),
  ('posting.max_duration_s', '{"tiktok": 600, "instagram": 900, "youtube": 180}'::jsonb,
   'Longest clip in seconds each platform is given (M18 Phase C): TikTok API 10 min, Instagram Reels 15 min, YouTube Shorts 3 min.')
on conflict (key) do nothing;

update settings
   set value = value || '{"cam_top_wordcap": {"enabled": true, "weight": 1, "lanes": ["spike", "hourly"], "accounts": "*",
     "rules": {"caption_words": 2, "captions": true, "hook_mode": "title_bar", "title_whole_clip": true, "punchin_max": 0, "zoomcut_every_s": 0, "cut_sfx": false, "payoff_fx": false, "compress": true, "sfx_gain_db": -10, "facecam_top": true, "caption_chars": 16, "caption_font": "Montserrat Black", "caption_size": 92, "caption_caps": true, "caption_y": 580, "caption_active_color": "#FFD400", "show_title": false}}}'::jsonb
 where key = 'factory.styles'
   and not (value ? 'cam_top_wordcap');

update settings
   set value = jsonb_set(value, '{affinity,story,cam_top_wordcap}', '3'::jsonb)
 where key = 'factory.style_director'
   and value -> 'affinity' ? 'story'
   and not (value -> 'affinity' -> 'story' ? 'cam_top_wordcap');

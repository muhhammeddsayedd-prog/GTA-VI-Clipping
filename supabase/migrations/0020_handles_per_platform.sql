-- D111: each platform names its own account.
-- The accounts have three different handles (TikTok @6starsclipping, Instagram @6starsclippy, YouTube
-- @6starsClips-m3x), so one fixed handle in the first comment or in the burned-in watermark is wrong on
-- two of the three platforms.

-- First comment: `{handle}` is replaced by the posting account's handle (posting/captions.py).
update settings
set value = to_jsonb(replace(value #>> '{}', '@6starsclipping', '{handle}'))
where key = 'posting.first_comment'
  and jsonb_typeof(value) = 'string'
  and value #>> '{}' like '%@6starsclipping%';

-- Watermark: one video file goes to all three platforms, so it carries the brand name, not a handle.
update settings
set value = '"6 Stars Clips"'::jsonb
where key = 'factory.watermark'
  and value = '"@6starsclipping"'::jsonb;

-- The YouTube handle with the channel's own capitalisation.
update platform_accounts
set handle = '@6starsClips-m3x'
where id = 'youtube_main'
  and handle = '@6starsclips-m3x';

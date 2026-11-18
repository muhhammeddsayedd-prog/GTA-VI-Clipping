-- Console media: clip thumbnails and creator profile pictures for the admin console (docs/26). Additive
-- and idempotent: new nullable columns and new settings; existing rows and values are never changed.
--
-- Numbered 0022, not 0021: 0021 is held by the viral-style work (0021_viral_style.sql), still unmerged.
--
-- clips.thumb_path       storage key of one JPEG frame of the rendered clip, in the private `clips` bucket
--                        (`clips/thumbs/<env>/<yyyy>/<mm>/<dd>/<clip_id>.jpg`), made right after the render
--                        (gate-rejected clips get one too; clips the writer rejected are never rendered)
-- creators.avatar_url    the platform's public profile picture URL (Twitch Get Users profile_image_url,
--                        YouTube channels.list snippet.thumbnails)
-- creators.avatar_checked_at  when the picture was last looked up, also when none was found
-- videos.thumbnail_url, viewer_clips.thumbnail_url, live_sessions.thumbnail_url
--                        the platform's public thumbnail of the source (Twitch Get Videos / Get Clips /
--                        Get Streams thumbnail_url with the size filled in, YouTube snippet.thumbnails)

alter table clips add column if not exists thumb_path text;
alter table creators add column if not exists avatar_url text;
alter table creators add column if not exists avatar_checked_at timestamptz;
alter table videos add column if not exists thumbnail_url text;
alter table viewer_clips add column if not exists thumbnail_url text;
alter table live_sessions add column if not exists thumbnail_url text;

insert into settings (key, value, description) values
  ('console.thumb_frame_s', '1.5'::jsonb,
   'The clip thumbnail is the frame this many seconds into the rendered clip (the middle of the clip when it is shorter).'),
  ('console.thumb_width_px', '540'::jsonb,
   'Width of the clip thumbnail JPEG in pixels; the height keeps the 9:16 shape.'),
  ('console.thumb_jpeg_qscale', '5'::jsonb,
   'ffmpeg JPEG quality of the clip thumbnail (-q:v, 2 best to 31 worst; 5 is about JPEG quality 80).'),
  ('console.avatar_refresh_days', '7'::jsonb,
   'A creator''s profile picture is looked up again when the last lookup is older than this many days.'),
  ('console.avatars_per_pass', '100'::jsonb,
   'Creators whose profile picture is looked up per discovery pass, at most (Twitch: 100 per request; YouTube: 50 per request, 1 quota unit each).')
on conflict (key) do nothing;

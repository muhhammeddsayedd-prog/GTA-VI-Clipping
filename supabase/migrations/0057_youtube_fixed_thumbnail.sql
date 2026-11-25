-- 0057: one fixed thumbnail for every YouTube Short (owner, 2026-11-25).
--
-- `cover.youtube_fixed_thumbnail` holds the storage key of one image in the public `covers-public` bucket
-- (e.g. "covers-public/brand/youtube-thumbnail-<hash>.jpg"), or null. When it is set, every published
-- YouTube post gets that image as its thumbnail through Upload-Post's set-thumbnail endpoint
-- (job `cover.youtube_fixed_thumb`), whether the cover engine is on or off, and instead of any composed
-- cover. Null: YouTube works as before. Set it with `sixstars covers youtube-thumbnail set <image>`.

insert into scoring_config (key, value, description) values
  ('cover.youtube_fixed_thumbnail', 'null'::jsonb,
   'Storage key (covers-public) of the one thumbnail every YouTube Short gets after publishing. Null: off. Works with the cover engine off and wins over composed covers.')
on conflict (key) do nothing;

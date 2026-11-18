# Console media: clip thumbnails and creator pictures (2026-11-18)

Backend for the admin console's pictures. Additive only; the console itself (`web/`) is changed separately.

## What was built

- Migration `supabase/migrations/0022_console_media.sql` (idempotent): `clips.thumb_path`,
  `creators.avatar_url`, `creators.avatar_checked_at`, `videos.thumbnail_url`, `viewer_clips.thumbnail_url`,
  `live_sessions.thumbnail_url`, and five `console.*` settings. Numbered 0022 because `0021_viral_style.sql`
  is held by the unmerged viral-style work; if that lands later, `supabase db push` needs `--include-all` (or
  renumber one of the two).
- Clip frame at render time: `factory/thumbs.py`, called from `pipeline.render_clip` right after `_finish`
  (before the gate, so gate-rejected clips get one; writer-rejected clips are never rendered). ffmpeg
  `-ss` before `-i`, scale to `console.thumb_width_px`, `-q:v console.thumb_jpeg_qscale`, uploaded as
  `image/jpeg` to `clips/thumbs/<env>/<yyyy>/<mm>/<dd>/<clip_id>.jpg` (`storage.thumb_key`). Any failure:
  log + `clip.thumb_failed` event (signed-URL tokens redacted), render continues. Without Storage configured
  nothing is tried.
- Source thumbnails: `Candidate.thumbnail_url` from Twitch Get Streams (640x360), Get Videos (320x180) and
  YouTube `videos.list` `snippet.thumbnails` (high, medium, default). `discovery.store.queue_video` stores it
  on new `videos`; `store.save_thumbnails` updates `videos` and `live_sessions` of every source seen each
  pass (only rows whose URL changes). Viewer clips store Get Clips `thumbnail_url` on insert.
- Creator pictures: `discovery/media.py` `fill_avatars`, run at the end of every discovery pass (step
  `console_media`, summary field `avatars`; never fails the pass). Twitch Get Users (100 ids per request),
  YouTube `channels.list part=snippet` (50 per request, 1 unit, checked against
  `discovery.youtube_quota_daily_units` through `api_usage`). `avatar_checked_at` is set for every answered
  channel, also without a picture; a platform without keys, failing, or out of quota is left for later.
- New client calls: `TwitchClient.get_videos_by_id`, `get_clips_by_id`, `twitch.fill_thumbnail`,
  `youtube.pick_thumbnail`, `ClipStorage.signed_urls` (batch), with fakes in `clients/fakes.py` and
  `FakeClipStorage` (`missing`, `sign_batches`).
- Admin API (docs/26, "Console media"): `avatar_url` on every `CreatorRef` and on creators rows;
  `thumb_url` + `thumb_kind` on clip items/detail and `QueueItem` (queue + overview `up_next`);
  `clip_thumb_url` on every post row; `thumbnail_url` on videos and live sessions. Queries select private
  columns (`_thumb_path`, `_source_thumb`, ...) and `routes_admin.answer` signs all frames of an answer in
  one Storage request, then `admin_queries.resolve_thumbs` swaps them for the public fields. A signing error
  falls back to the source thumbnail or null.

## Settings added

| Key | Default |
|---|---|
| `console.thumb_frame_s` | 1.5 |
| `console.thumb_width_px` | 540 |
| `console.thumb_jpeg_qscale` | 5 |
| `console.avatar_refresh_days` | 7 |
| `console.avatars_per_pass` | 100 |

## Backfill

After the migration is applied, on the server (needs ffmpeg, the Storage, Twitch and YouTube keys):

    sixstars console-media-backfill --limit 200

It prints JSON per step: `frames` (clips with a stored or local file and no `thumb_path`; the local file if
it still exists, else a signed URL of the mp4 read by ffmpeg over https), `sources` (videos and viewer clips
with no thumbnail, via Twitch by id and YouTube `videos.list`), `avatars` (every due creator, `--limit` per
round). Idempotent: run it again to continue after a limit or an error.

## Verified in the official docs (2026-11-18)

- Twitch Get Videos (dev.twitch.tv/docs/api/reference/videos#get-videos): `thumbnail_url` placeholders
  `%{width}` / `%{height}`, "Due to current limitations, ${width} must be 320 and ${height} must be 180";
  `id` up to 100, exclusive with `user_id` / `game_id`.
- Twitch Get Streams: `thumbnail_url` is a frame from the last 5 minutes, placeholders `{width}x{height}`.
- Twitch Get Clips: `thumbnail_url` "A URL to a thumbnail image of the clip" (no placeholders); `id` up to 100.
- Twitch Get Users: `profile_image_url`; `id` up to 100.
- YouTube thumbnails resource: sizes per key for videos and channels; `url` is "The image's URL". No URL
  format is documented, so the `i.ytimg.com/vi/<id>/...` pattern is not used: URLs come from API answers only.
- Supabase Storage batch signing: the JS reference documents `createSignedUrls(paths, expiresIn)`; the
  REST shape (`POST /object/sign/{bucket}`, body `{expiresIn, paths}`, answer `[{path, signedURL, error}]`,
  1 to 1,000 paths) was read from the storage server source (`getSignedURLs.ts`, `limits.ts`) and storage-js
  `StorageFileApi.createSignedUrls`. Not yet called against the live project.

## Open

- Integration tests (`tests/integration/test_admin_routes.py::test_console_media_fields`,
  `tests/integration/test_console_media.py`) were written but not run here: no throwaway Postgres was
  available. CI runs them.
- Twitch live previews change as the stream goes on and stop after it ends; clip rows never use them as
  their `"source"` thumbnail (only YouTube live sessions are used there).

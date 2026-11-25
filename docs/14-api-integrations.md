# 14 — API Integrations

**Rule for Claude Code:** before writing each client, read the linked current docs. Facts marked ✅ were checked during planning (16 Nov 2026) or while building the clients in M02 (17 Nov 2026, marked "M02"). Facts marked ⚠️ must be confirmed when coding.

## 1. Upload-Post (posting + analytics)
Docs: https://docs.upload-post.com (machine-readable index: https://docs.upload-post.com/llm.txt; any page + `.md` gives raw markdown)

| Item | Detail |
|---|---|
| Upload | ✅ `POST https://api.upload-post.com/api/upload`, multipart. Header `Authorization: Apikey <key>`. Required: `user`, `platform[]`, `video` (file or URL), `title` (required for YouTube). |
| Idempotency | ✅ `Idempotency-Key` header (also `X-Idempotency-Key`); send your own `request_id`. If the HTTP client times out, **do not resend** without the same idempotency key. |
| Async | ✅ `async_upload=true` returns a `request_id`; uploads > 59 s become async automatically. ✅ M02: poll `GET /api/uploadposts/status?request_id=<id>`. Top-level `status`: `pending`, `queued`, `processing`, `in_progress`, `completed`, `failed`, `not_found` (HTTP 404). `results` is a list with one entry per platform (`platform`, `success`, `message`, `platform_post_id`, `post_url`, `error_code`, ...). Poll every 10 s, not faster than 5 s. A synchronous upload answers HTTP 200 with `results` as an object keyed by platform; a failed platform has `success: false` there, so always read the per-platform flag. |
| TikTok fields | ✅ `privacy_level` (`PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_FRIENDS`, `FOLLOWER_OF_CREATOR`, `SELF_ONLY`), `disable_duet`, `disable_comment`, `disable_stitch`, `post_mode`, `tiktok_title`, `tiktok_first_comment` |
| YouTube fields | ✅ `youtube_title`, `youtube_description`, `tags`, `categoryId` (use `"20"` Gaming), `privacyStatus` (`public`/`unlisted`/`private`), `selfDeclaredMadeForKids` (false) |
| Instagram fields | ✅ `media_type` (`REELS`), `share_to_feed`, `cover_url` |
| Cover fields | ✅ 2026-11-23 (cover engine C0, docs read + live probe, details in `docs/29b-cover-probe.md`). **Instagram:** `cover_url` (string) or `cover_image` (JPEG file ≤ 8 MB), `cover_url` wins; `thumb_offset` (string, ms). Not probed live (staging can't post IG privately). **TikTok:** `cover_timestamp` (int, ms, default 1000; capability `cover_timestamp`); `tiktok_cover_image_url` / `tiktok_cover_image` (JPG/PNG/WebP ≤ 20 MB, wins over the timestamp; capability `cover_image`). A missing capability does not fail the post: it publishes and `warnings` names the ignored field. Capabilities: `GET /api/uploadposts/users` → `social_accounts.tiktok.capabilities`; ours include `cover_image` and `cover_timestamp`. Both fields were accepted live on `SELF_ONLY` posts with no warning. **YouTube:** `thumbnail` (file ≤ 50 MB) / `thumbnail_url`; a Short still processing answers `thumbnail_set: false, thumbnail_pending: true` (seen in the status `results` entry, still pending after 8 min). `POST /api/uploadposts/youtube/thumbnail` JSON `{user, video_id, thumbnail_url}` sets it later: 200 with `thumbnails.shortsAspectRatio` (1080×1920), 202 `pending: true` while processing, 403 usually an unverified channel, 429 YouTube quota. **It answered 200 on our channel, which is not in the Partner Program** (private video). Whether YouTube displays it on Shorts surfaces is an open check. |
| First comment | ✅ `first_comment` or `<platform>_first_comment` |
| Limits | ✅ TikTok 6 posts/min and 15 posts/day per TikTok account. ✅ Instagram capped at 50 posts/24 h by Upload-Post. ✅ M02: **YouTube is capped at 30 posts/24 h per account by Upload-Post** (hourly posting alone uses 24). ✅ M02: API rate limit on the Basic plan is 60 requests/min and 300 per 10 min (+2/min per profile); over it: HTTP 429 with `X-RateLimit-Reset`. ⚠️ Check your plan's monthly upload allowance (need ~1,500–2,000/month): over it, upload answers HTTP 429 with `usage`. |
| Video limits | ✅ 2026-11-19 (https://docs.upload-post.com/api/video-requirements): TikTok 3 s to 10 min via the API, max 4 GB (Upload-Post rejects over 3 GB); Instagram Reels 3 s to 15 min, max 300 MB, aspect 0.01:1 to 10:1 (9:16 recommended); YouTube max 256 GB, no length stated. YouTube Shorts are square or vertical videos up to 3 minutes (YouTube Help 15424877, since 2024-10-15). Used for `posting.max_duration_s` (M18 Phase C). |
| Profiles | Each profile (`user`) groups connected accounts. One profile exists: `default` (decision D101). ✅ M02: `GET /api/uploadposts/users` returns `{"profiles": [{"username": ..., "social_accounts": ...}]}` and is read-only. |
| Analytics | ✅ `GET /api/analytics/{profile}?platforms=tiktok,instagram,youtube` (followers, reach, views). ✅ M02: per post `GET /api/uploadposts/post-analytics/{request_id}` takes the **request id of the upload** (optional `?platform=`); the answer has `platforms.<name>.platform_post_id`, `post_url`, `post_metrics`. By platform id: `GET /api/uploadposts/post-analytics?platform_post_id=&platform=&user=` (100 requests per 5 minutes, cached 5 minutes). |
| Delete | ✅ M02: `POST /api/uploadposts/posts/unpublish` with JSON `{platform, user, post_id}` deletes a live post on YouTube (and Facebook, X, LinkedIn, ...). **Not available for Instagram or TikTok**: those still need `docs/12` §3. |
| Errors | ✅ M02: non-200 bodies are `{"success": false, "message": ...}`. 400 invalid request; 401 bad key; 403 plan does not allow it; 404 user not found; 429 monthly or daily cap; 5xx server. Per-platform failures carry `error` and often `error_code` (`account_reauth_required`, `account_restricted`, `content_rejected`, `tiktok_privacy_unavailable`, `youtube_channel_daily_limit`, ...). TikTok may deliver to the inbox instead of publishing (`fallback_to_inbox: true`) and may give the public post id hours later (`post_url: null`). |
| Webhooks | ⚠️ Account/profile webhooks exist (HMAC `X-Upload-Post-Signature`); optional, polling is enough. |
| Long-form upload | ✅ 2026-11-25 (M20 L0, https://docs.upload-post.com/api/upload-video): `video` may be a **public URL** (fetched, up to 20 GB; a direct file is up to 4 GB). Uploads over 59 s go async on their own; on a client timeout do not resend, keep the same `request_id`. YouTube fields at upload: `title` (required), `description`/`youtube_description`, `tags` (array), `categoryId` (default "22", we send "20"), `privacyStatus` (default `public`), `thumbnail` (file) or `thumbnail_url` (JPG/PNG/GIF/BMP ≤ 50 MB, the file wins), `containsSyntheticMedia`, `youtube_playlist_id`, `youtube_publish_at`. No YouTube length limit stated (max 256 GB). Long-form episodes send a signed Storage URL, not the file. ⚠️ To measure in L0: real URL-mode upload time and the answer shape. |

**Verified by the first real uploads (M15, 2026-11-17: one private YouTube video and one `SELF_ONLY` TikTok post from staging; details in `development-plan/progress-notes/M15-staging-post.md`).** ✅ = seen in a real answer.

| Item | What the real API did |
|---|---|
| Upload request | ✅ Multipart fields exactly as documented: `async_upload=true`, `user`, `platform[]`, `request_id`, `title`, and per platform `privacyStatus=private` (+ `youtube_title`, `youtube_description`, `categoryId`, `selfDeclaredMadeForKids`) or `privacy_level=SELF_ONLY`; file field `video`; header `Idempotency-Key`. A 24 MB file took 75 to 80 s to send; the answer came back `pending`. |
| Status while running | ✅ `{"request_id", "job_id", "status": "processing", "scheduler_status", "completed", "failed", "retryable", "skipped", "total", "results": [{"platform", "status": "processing", "attempts", "success": false}], "external_id", "last_update": "<HTTP date>", "message"}`. `success: false` here only means "not finished". |
| Status when finished | ✅ A different shape: `{"status": "completed", "completed", "total", "results": [...], "external_id", "last_update": {"$date": ...}, "request_id"}`, no top-level `message`. Each result has `platform`, `success`, `platform_post_id`, `post_url`, `error_message`, `error_code`, `error_signature`, `failure_stage`, `warnings` (list of sentences), `media_size_bytes`, `post_title`, `post_caption`, `upload_timestamp`, `prevalidation_metadata`, `job_id`, `request_id`, and the account's email (`user_email`: never log the raw answer). |
| YouTube private | ✅ Finished about 80 s after the request. `platform_post_id` is the video id; **`post_url` is the sentence `"Post uploaded as Private. No public URL available."`, not a link** (the publisher stores `https://www.youtube.com/watch?v=<id>`). The public Data API (`videos.list` with the API key) returns no item for it, the watch page says "Private video", oEmbed answers 403. `first_comment` is not posted on a private video (a `warnings` entry says so). |
| TikTok `SELF_ONLY` | ✅ Finished about 4 minutes after the request (status `processing` until then). **`platform_post_id: null` and `post_url: null`**, with `original_publish_id` (`v_pub_url~v2.<digits>`, not a post id) and the warnings "TikTok published the video but has not released its public post ID yet ..." and "First comment skipped: TikTok has not returned the post id yet." No `fallback_to_inbox`. The docs say a post that is not public never gets a post id, so a staging TikTok post stays without id and link. **No answer of Upload-Post states the privacy TikTok applied** (status, history, analytics and the media list do not carry it); the connection has the `video_privacy` capability and `GET /api/uploadposts/tiktok/settings?profile=` lists `SELF_ONLY` for the account. The TikTok media list (`GET /api/uploadposts/media?platform=tiktok&user=`) stayed empty. |
| First comment | ✅ Skipped on TikTok whenever the post id is not known at publish time, and never posted on a private YouTube video. In production expect the TikTok first comment to be missing on some posts. |
| Post analytics by request id | ✅ `{"success", "post": {"request_id", "profile_username", "post_title", "post_caption", "media_type", "upload_timestamp"}, "platforms": {"<platform>": {"success", "platform_post_id", "post_url", "error_message", "media_size_bytes", "post_metrics" or "post_metrics_error", "post_metrics_source", ...}}}`. YouTube `post_metrics` keys: `views`, `likes`, `comments`, `favorites`, `shares`, `watch_time_minutes`, `average_view_duration_seconds`, `average_view_percentage`, `engaged_views`, `subscribers_gained` (a private video reads fine, all zero). TikTok without a post id: no `post_metrics`, `post_metrics_error: "TikTok post ID could not be resolved yet. Moderation may still be in progress. Please try again later."`, plus `original_publish_id`, `profile_snapshot_at_post_date`, `profile_snapshot_latest`, `profile_snapshot_latest_date`. `?platform=` gives the same shape. |
| Post analytics by platform id | ✅ The metrics are under `platforms.<platform>.post_metrics` here too (not at the top level), with `available_metrics`, `metric_labels`, `primary_impressions_field`; `post` also has `platform_post_id`, `platform`, `post_url`, `source` (`api_uploaded`). |
| Delete (YouTube) | ✅ `POST /api/uploadposts/posts/unpublish` with `{platform: "youtube", user, post_id: <video id>}` **works on a private video**: HTTP 200 `{"success": true, "message": "YouTube video deleted."}`; the watch page then says "Video unavailable". Right after the delete, post analytics still answered with the old `post_metrics` (the answer is cached for 5 minutes), so a deleted video can read as "ok" for a few minutes; 7 minutes later both analytics forms answered `post_metrics_error: "YouTube video not found (ID: <id>)."`, which is the text the guardian reads as "gone". |
| Other read-only endpoints seen | ✅ `GET /api/uploadposts/history?request_id=` (`history[]` with the same fields as a finished status result, plus `tiktok_session_message`), `GET /api/uploadposts/media?platform=&user=` (`media[]` with `id`, `caption`, `permalink`, `timestamp`; for YouTube it lists private videos, and a deleted one as "Deleted video"). |

## 2. Twitch
Docs: https://dev.twitch.tv/docs/api/reference , https://dev.twitch.tv/docs/chat/irc

| Item | Detail |
|---|---|
| App token | ✅ M02: `POST https://id.twitch.tv/oauth2/token` with `client_id`, `client_secret`, `grant_type=client_credentials` in a form body (`application/x-www-form-urlencoded`); answer `access_token`, `expires_in`; no refresh token; cache until expiry |
| Headers | `Client-Id: <id>`, `Authorization: Bearer <app token>` |
| Game id | `GET /helix/games?name=Grand%20Theft%20Auto%20VI` (⚠️ confirm the exact category name once GTA 6 is listed; staging uses `Grand Theft Auto V`) |
| Live streams | `GET /helix/streams?game_id=&first=100&after=` |
| VODs | `GET /helix/videos?game_id=&type=archive&period=day&sort=views&first=50`; per user `GET /helix/videos?user_id=&type=archive&first=5` (`stream_id` links a VOD to a stream). ✅ M02: `first` max 100; a `game_id` query returns at most 500 videos; `duration` is text such as `1h0m5s` |
| Users | `GET /helix/users?id=` → `description`, `profile_image_url` |
| Followers | `GET /helix/channels/followers?broadcaster_id=`. ⚠️ M02: the reference lists only a **user** access token (scope `moderator:read:followers` for the list; without it only `total`). Whether an app token gets `total` could not be tested (no Twitch keys): the client returns `None` on 401/403 |
| Clips | ✅ D110 (reference read and real calls with the app token, 2026-11-17): `GET /helix/clips?game_id=<id>&started_at=<RFC 3339>&ended_at=<RFC 3339>&first=100`, app or user token. Exactly one of `broadcaster_id`, `game_id`, `id`. Times are cut to the minute; without `ended_at` the window is one week from `started_at`; `ended_at` alone is ignored. `first` 1 to 100 (default 20), `after` = `pagination.cursor`; sorted by view count, descending; paging stops near 1,000 clips. `is_featured` true/false filters, omitted returns all. Fields: `id`, `url`, `broadcaster_id`, `broadcaster_name` (the streamer), `creator_id`, `creator_name` (the viewer who made the clip), `video_id` ("" when the broadcast's video is not available), `game_id`, `language` (ISO 639-1 or `other`), `title`, `view_count`, `created_at`, `thumbnail_url`, `duration` (seconds, 0.1 precision), `vod_offset` (seconds into the video; null without a video, and for the first minutes after the clip is made), `is_featured`. There is no login in the row: `GET /helix/users?id=` gives it. yt-dlp downloads the clip URL without a login (extractor `twitch:clips`; landscape renditions 360 to 1080 plus auto-cropped `portrait-*` ones); a deleted clip gives "This clip is no longer available" |
| Rate limit | ~800 points/min per app token; discovery uses < 20 calls per run |
| Chat (live) | IRC over `wss://irc-ws.chat.twitch.tv:443`, anonymous `NICK justinfan<digits>`, capabilities `twitch.tv/tags twitch.tv/commands` |
| Chat (replay) | TwitchDownloaderCLI (https://github.com/lay295/TwitchDownloader, MIT). ⚠️ confirm current CLI flags: `chatdownload --id <vod> -o chat.json` |

## 3. YouTube
Docs: https://developers.google.com/youtube/v3

| Item | Detail |
|---|---|
| Public data | API key: `videos.list` (status, contentDetails, statistics, liveStreamingDetails), `channels.list` (snippet, statistics) |
| Quota | ✅ M02 (quota cost page): 10,000 units/day per project for everything except `search.list` and `videos.insert`. `videos.list` and `channels.list` = 1 unit; `videos.delete` = 50 units; `videos.insert` = 1 unit per call **in its own bucket, 100 calls/day by default**. ⚠️ The real limits of this project are on the Cloud Console quota page (not checked). |
| Unaudited uploads | ✅ Videos uploaded via `videos.insert` from unverified API projects created after 28 July 2020 are restricted to private. That's why all public posting goes through Upload-Post and our own project only does **private** pre-check uploads. |
| OAuth | Owner's Google account; scopes `youtube.upload` (pre-check) and `youtube` (delete, read private status). Set the OAuth consent screen publishing status to **In production** (accepting the "unverified app" screen for the owner's own use); in "Testing" status refresh tokens expire after 7 days. ⚠️ Confirm this behaviour when setting up. `sixstars youtube auth` runs the one-time local OAuth flow and prints the refresh token. ✅ M02: desktop-app flow = loopback redirect `http://127.0.0.1:<port>`, PKCE S256, authorization endpoint `https://accounts.google.com/o/oauth2/v2/auth`, token endpoint `https://oauth2.googleapis.com/token`; a refresh token is always returned for desktop apps, so the OAuth client must be of type "Desktop app". |
| RSS | `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` (no quota) |
| Live chat | `chat-downloader` (https://github.com/xenova/chat-downloader) for live and replay; no API quota |
| Downloads | yt-dlp; YouTube often blocks data-centre IPs → residential proxy (`--proxy`) and/or cookies (`--cookies`) |
| Long videos | ✅ 2026-11-25 (YouTube Help 71673, 9884579, 72431): unverified channels upload up to **15 min**; longer needs phone verification. Custom thumbnails need a verified account, and there is a daily cap on thumbnail uploads. Chapters: the first timestamp is 00:00, at least 3 timestamps in ascending order, each chapter at least 10 s. Episodes stay at 14:30 or under. |

## 4. Apify (YouTube discovery)
Docs: https://docs.apify.com/api/v2 ; actor page in Apify Store.
- Actor: `streamers/youtube-scraper`. ✅ M02 input (schema + real runs): `searchQueries` (list), `maxResults` (finished videos per term), `maxResultsShorts`, `maxResultStreams` (streams per term), `isLive` (bool), `dateFilter` (`hour`, `today`, `week`, `month`, `year`), `sortingOrder` (`relevance`, `rating`, `date`, `views`). **A live search needs `isLive: true` with `maxResultStreams`**; with `maxResults` alone it returns nothing. Output fields: `id`, `title`, `type` (`video`/`stream`), `streamType` (`live`), `viewCount` (current viewers for a live stream), `date`, `likes`, `commentsCount`, `duration` (`HH:MM:SS`, null when live), `channelId`, `channelName`, `numberOfSubscribers`. A search that finds nothing writes one row `{"error": "NO_VIDEOS"}`; this also happened once at random, so an empty result is not an error.
- ✅ M02 price: pay per event, $0.003 per video written (17 Nov 2026). Every run is started with `maxTotalChargeUsd`.
- Run async: `POST /v2/acts/<user>~<actor>/runs` (HTTP 201) → poll `GET /v2/actor-runs/<id>` until `status` is `SUCCEEDED`, `FAILED`, `TIMED-OUT` or `ABORTED` → `GET /v2/datasets/<datasetId>/items?clean=true`. ✅ M02: the token goes in `Authorization: Bearer`, not in the URL.
- Budget: keep max results ~50 per term per run.

## 5. Anthropic
Docs: https://docs.claude.com
- SDK `anthropic`; models from settings (`claude-sonnet-5-5`, `claude-haiku-5-5`).
- ✅ M02 prices per million tokens (pricing page, 17 Nov 2026): Sonnet 5.5 input $2, output $10, 5-minute cache write $2.50, cache read $0.10. Haiku 5.5 input $0.10, output $0.50, cache write $0.125, cache read $0.01 for prompts up to 100,000 tokens; above that $0.50 / $2.50 / $0.625 / $0.05.
- ✅ M02: both models think by default and thinking counts toward `max_tokens`; depth is set with `output_config.effort`; `temperature`/`top_p`/`top_k` and assistant prefill are rejected. A policy decline is HTTP 200 with `stop_reason: "refusal"`.
- Use structured outputs: give the JSON schema in the prompt and validate with Pydantic; on invalid JSON retry once with the validation error appended.
- Vision: send JPEG frames (≤ 1568 px long side) as base64 image blocks.
- Use prompt caching for the long static system prompts (P2, P3).
- Log tokens and cost to `api_usage` on every call.

## 6. Transcription
- **Groq** (not configured, decision D104): ✅ M02 docs: `POST https://api.groq.com/openai/v1/audio/transcriptions`, model `whisper-large-v3-turbo` ($0.04 per audio hour, at least 10 s billed per request), `response_format=verbose_json` with `timestamp_granularities[]=word` gives `words[{word,start,end}]`. Not tested live (no key).
- **OpenAI** (the hosted provider in use): ✅ M02: `POST https://api.openai.com/v1/audio/transcriptions` with `model=whisper-1`, `response_format=verbose_json`, `timestamp_granularities[]=word`. **Word timestamps exist only on `whisper-1`** (not on the `gpt-*-transcribe` models); `whisper-1` shuts down on 26 Feb 2027. $0.006 per minute, 25 MB file limit. Tested live: `language` comes back as a name (`english`).
- **Local**: faster-whisper `medium`, `compute_type=int8`.

## 6b. OpenAI Images (long-form thumbnail, M20)
✅ Checked 2026-11-25 (https://developers.openai.com/api/reference/resources/images/methods/edit, the image
generation guide and the pricing page): `POST https://api.openai.com/v1/images/edits`, multipart `model`,
`prompt`, up to 16 `image[]` files, `size` (1024x1024, 1536x1024, 1024x1536, auto; GPT Image 2.x also any
`WxH` with edges divisible by 16, aspect 1:3–3:1, max 3840x2160), `quality` (low/medium/high/auto),
`output_format` (png/jpeg/webp), `n`. The reply has `data[0].b64_json` (no URLs) and `usage` tokens. Models:
`gpt-image-2.6-sunburst` (default, recommended), `-flare`, `gpt-image-2.5-*`, `gpt-image-2`, `gpt-image-1.5`,
`gpt-image-1-mini`; **`gpt-image-1` was shut down on 2026-10-23**. Price (GPT Image 2.x, per 1M tokens): image
input $8, text input $5, image output $30 (about $0.15–0.25 for one high-quality 1536x864 image). Complex
prompts can take up to 2 minutes. A refusal is an error with code `moderation_blocked`. Organisation
verification may be needed before GPT Image models work on a key. Client: `clients/openai_images.py`.

## 7. Telegram (via n8n only)
The worker never calls Telegram directly; it calls the n8n notify webhook (`docs/13` WF07).

## 8. Supabase
- Postgres via session pooler connection string; Storage via `supabase` Python client with the service role key.
- Bucket `clips` (private).
- ✅ 2026-11-25 (https://supabase.com/docs/guides/storage/uploads/file-limits, /standard-uploads, /resumable-uploads): the global file limit is at most 50 MB on Free and up to 500 GB on Pro (the org is on Pro; the limit is set in Dashboard → Storage → Settings). The standard upload is meant for files of 6 MB or less (it works up to 5 GB); use TUS resumable upload for anything larger (upload URL valid up to 24 h, 6 MB chunks). Bucket `episodes` (private, M20) holds the long-form MP4s and thumbnails.

## 9. Tools in the Docker image
| Tool | Install |
|---|---|
| ffmpeg/ffprobe 7.x | static build (johnvansickle) or Debian package if ≥ 6 |
| streamlink | pip |
| yt-dlp | pip, upgraded on container start |
| TwitchDownloaderCLI | Linux x64 release zip from GitHub; needs `libicu`; set executable |
| chat-downloader | pip |
| faster-whisper | pip; models downloaded at build time into the image (`medium`) |
| panns-inference | pip; checkpoint downloaded at build time |
| fonts | Montserrat (OFL) into `/usr/share/fonts`, `fc-cache -f` |
| opencv-python-headless 4.14.0.94 (Render v2 face check, D119) | pip (`pyproject.toml`, `>=4.14.0.94,<5`); no system packages. API checked 2026-11-23 against the installed package's own docstrings (docs.opencv.org refused automated fetches): `cv2.FaceDetectorYN.create(model, config, input_size[, score_threshold[, nms_threshold[, top_k[, backend_id[, target_id]]]]])`, `setInputSize(size)`, `detect(image) -> (retval, faces)` with one row of 15 values per face: x, y, w, h, five landmark points (x, y), score. 4.x was chosen over 5.1.0.95 (released 2026-11-17, too new) because the 2023mar model has a fixed input shape and runs on the 4.x DNN engine; the zoo's 2026may model targets OpenCV 5's ONNX Runtime engine. |
| YuNet face model | `assets/models/face_detection_yunet_2023mar.onnx` from opencv/opencv_zoo `models/face_detection_yunet` (Git LFS; fetched from media.githubusercontent.com), 232,589 bytes, sha256 `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` (matches the LFS pointer). MIT licence, `assets/models/LICENSE-yunet.txt`. Bundled in the image with `assets/`. |

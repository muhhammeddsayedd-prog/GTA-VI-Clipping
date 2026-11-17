# 14 — API Integrations

**Rule for Claude Code:** before writing each client, read the linked current docs. Facts marked ✅ were checked during planning (16 Nov 2026). Facts marked ⚠️ must be confirmed when coding.

## 1. Upload-Post (posting + analytics)
Docs: https://docs.upload-post.com (machine-readable index: https://docs.upload-post.com/llm.txt; any page + `.md` gives raw markdown)

| Item | Detail |
|---|---|
| Upload | ✅ `POST https://api.upload-post.com/api/upload`, multipart. Header `Authorization: Apikey <key>`. Required: `user`, `platform[]`, `video` (file or URL), `title` (required for YouTube). |
| Idempotency | ✅ `Idempotency-Key` header (also `X-Idempotency-Key`); send your own `request_id`. If the HTTP client times out, **do not resend** without the same idempotency key. |
| Async | ✅ `async_upload=true` returns a `request_id`; uploads > 59 s become async automatically. Poll the upload status endpoint (⚠️ confirm path and method: `/api/uploadposts/status` or `/api/upload-status`). |
| TikTok fields | ✅ `privacy_level` (`PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_FRIENDS`, `FOLLOWER_OF_CREATOR`, `SELF_ONLY`), `disable_duet`, `disable_comment`, `disable_stitch`, `post_mode`, `tiktok_title`, `tiktok_first_comment` |
| YouTube fields | ✅ `youtube_title`, `youtube_description`, `tags`, `categoryId` (use `"20"` Gaming), `privacyStatus` (`public`/`unlisted`/`private`), `selfDeclaredMadeForKids` (false) |
| Instagram fields | ✅ `media_type` (`REELS`), `share_to_feed`, `cover_url` |
| First comment | ✅ `first_comment` or `<platform>_first_comment` |
| Limits | ✅ TikTok 6 posts/min and 15 posts/day per TikTok account. ✅ Instagram capped at 50 posts/24 h by Upload-Post. ⚠️ Check your plan's monthly upload allowance (need ~1,500–2,000/month) and extra profiles. |
| Profiles | Each profile (`user`) groups connected accounts. Create `6stars-main`, `6stars-live`, `6stars-staging` in the Upload-Post dashboard. |
| Analytics | ✅ `GET /api/analytics/{profile}?platforms=tiktok,instagram,youtube` (followers, reach, views). ✅ Per post: `GET /api/uploadposts/post-analytics/{id}` (⚠️ confirm whether the id is the request id or platform post id). |
| Delete | ⚠️ No delete endpoint for published posts was visible. Check again; if absent, see `docs/12` §3. |
| Webhooks | ⚠️ Account/profile webhooks exist (HMAC `X-Upload-Post-Signature`); optional, polling is enough. |

## 2. Twitch
Docs: https://dev.twitch.tv/docs/api/reference , https://dev.twitch.tv/docs/chat/irc

| Item | Detail |
|---|---|
| App token | `POST https://id.twitch.tv/oauth2/token?client_id=&client_secret=&grant_type=client_credentials`; cache until expiry |
| Headers | `Client-Id: <id>`, `Authorization: Bearer <app token>` |
| Game id | `GET /helix/games?name=Grand%20Theft%20Auto%20VI` (⚠️ confirm the exact category name once GTA 6 is listed; staging uses `Grand Theft Auto V`) |
| Live streams | `GET /helix/streams?game_id=&first=100&after=` |
| VODs | `GET /helix/videos?game_id=&type=archive&period=day&sort=views&first=50`; per user `GET /helix/videos?user_id=&type=archive&first=5` (`stream_id` links a VOD to a stream) |
| Users | `GET /helix/users?id=` → `description`, `profile_image_url` |
| Followers | `GET /helix/channels/followers?broadcaster_id=` (total only needs app token ⚠️ confirm) |
| Rate limit | ~800 points/min per app token; discovery uses < 20 calls per run |
| Chat (live) | IRC over `wss://irc-ws.chat.twitch.tv:443`, anonymous `NICK justinfan<digits>`, capabilities `twitch.tv/tags twitch.tv/commands` |
| Chat (replay) | TwitchDownloaderCLI (https://github.com/lay295/TwitchDownloader, MIT). ⚠️ confirm current CLI flags: `chatdownload --id <vod> -o chat.json` |

## 3. YouTube
Docs: https://developers.google.com/youtube/v3

| Item | Detail |
|---|---|
| Public data | API key: `videos.list` (status, contentDetails, statistics, liveStreamingDetails), `channels.list` (snippet, statistics) |
| Quota | ⚠️ Default 10,000 units/day per project. `videos.list` = 1 unit. Upload cost is reported as 1 unit in a separate 100/day bucket by some sources and 1,600 units by others — check the Cloud Console quota page. |
| Unaudited uploads | ✅ Videos uploaded via `videos.insert` from unverified API projects created after 28 July 2020 are restricted to private. That's why all public posting goes through Upload-Post and our own project only does **private** pre-check uploads. |
| OAuth | Owner's Google account; scopes `youtube.upload` (pre-check) and `youtube` (delete, read private status). Set the OAuth consent screen publishing status to **In production** (accepting the "unverified app" screen for the owner's own use); in "Testing" status refresh tokens expire after 7 days. ⚠️ Confirm this behaviour when setting up. `sixstars youtube auth` runs the one-time local OAuth flow and prints the refresh token. |
| RSS | `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` (no quota) |
| Live chat | `chat-downloader` (https://github.com/xenova/chat-downloader) for live and replay; no API quota |
| Downloads | yt-dlp; YouTube often blocks data-centre IPs → residential proxy (`--proxy`) and/or cookies (`--cookies`) |

## 4. Apify (YouTube discovery)
Docs: https://docs.apify.com/api/v2 ; actor page in Apify Store.
- Actor: `streamers/youtube-scraper` (⚠️ confirm input schema with the Apify actor details: search terms, upload date filter, live filter, max results).
- Run async: `POST /v2/acts/<actor>/runs?token=` → poll `GET /v2/actor-runs/<id>` → `GET /v2/datasets/<datasetId>/items?clean=true`.
- Budget: keep max results ~50 per term per run.

## 5. Anthropic
Docs: https://docs.claude.com
- SDK `anthropic`; models from settings (`claude-sonnet-5-5`, `claude-haiku-5-5`).
- Use structured outputs: give the JSON schema in the prompt and validate with Pydantic; on invalid JSON retry once with the validation error appended.
- Vision: send JPEG frames (≤ 1568 px long side) as base64 image blocks.
- Use prompt caching for the long static system prompts (P2, P3).
- Log tokens and cost to `api_usage` on every call.

## 6. Transcription
- **Groq** (express): OpenAI-compatible audio transcription endpoint; model `whisper-large-v3-turbo` (⚠️ confirm current model name and that `timestamp_granularities[]=word` is supported).
- **OpenAI** (fallback): `POST /v1/audio/transcriptions` with `response_format=verbose_json`, `timestamp_granularities[]=word` (⚠️ confirm model name).
- **Local**: faster-whisper `medium`, `compute_type=int8`.

## 7. Telegram (via n8n only)
The worker never calls Telegram directly; it calls the n8n notify webhook (`docs/13` WF07).

## 8. Supabase
- Postgres via session pooler connection string; Storage via `supabase` Python client with the service role key.
- Bucket `clips` (private).

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

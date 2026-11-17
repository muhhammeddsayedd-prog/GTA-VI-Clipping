# M02 — External API Clients + Fakes

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 00:30–02:30 UTC · **Requires:** M01 · **Launch-critical:** yes

## Goal
One well-behaved client per external service, each with timeouts, retries, logging, usage/cost recording and a fake for tests. Later milestones never call external APIs directly.

## Rules for every client
- Read the current official docs first (URLs below). Facts marked ✅ were verified on 16 Nov 2026; ⚠️ must be confirmed now. Record any difference in PROGRESS.md.
- httpx with timeouts (connect 10 s, read 60 s; uploads 600 s). tenacity retries: 3 attempts, exponential backoff 2→30 s, only on network errors, 429 and 5xx. Honour `Retry-After`.
- Log every call (service, endpoint, status, duration). Never log secrets or full request bodies with media.
- Record usage in `api_usage` (provider, operation, model, tokens/units, cost_usd estimate).
- `clients/fakes.py`: a fake for each client with deterministic canned responses, used by unit/integration tests.

## Clients to build

### 1. `clients/twitch.py` (docs: https://dev.twitch.tv/docs/api/reference)
- App token via client credentials (`POST https://id.twitch.tv/oauth2/token`), cached until 5 min before expiry; refresh on 401.
- Headers `Client-Id`, `Authorization: Bearer`.
- Methods: `get_game_id(name)`, `get_streams(game_id, max_items=300)` (paginate `after`), `get_videos_by_game(game_id, period="day", sort="views", type="archive", first=50)`, `get_videos_by_user(user_id, type="archive", first=5)`, `get_users(ids)` (description, profile), `get_followers_total(broadcaster_id)` (⚠️ confirm token requirements; return None if not allowed).
- Return dataclasses (`TwitchStream`, `TwitchVideo`, `TwitchUser`).

### 2. `clients/youtube.py` (docs: https://developers.google.com/youtube/v3)
- API key methods: `videos_list(ids, parts)`, `channels_list(ids, parts)`.
- RSS: `channel_feed(channel_id)` → recent video ids, titles, published times (parse Atom XML).
- OAuth (owner channel) methods for pre-check and deletion: `upload_private(path, title, description)` (resumable upload, `privacyStatus=private`), `get_status(video_id)` (status, contentDetails incl. regionRestriction), `delete_video(video_id)`, `whoami()` (channel id/title).
- ✅ Videos uploaded through unaudited API projects are forced private — that's fine: we only upload private pre-check copies.
- CLI `sixstars youtube auth`: run the installed-app OAuth flow locally (scopes `youtube.upload`, `youtube`), print the refresh token for `.env`. `sixstars youtube whoami`.
- Track quota units in `api_usage` (`units`): list = 1; upload cost per current Cloud Console (⚠️).

### 3. `clients/apify.py` (docs: https://docs.apify.com/api/v2)
- `start_run(actor_id, input) -> run_id`, `get_run(run_id)`, `get_items(dataset_id, limit)`.
- `youtube_search(terms, live=bool, uploaded_within_hours=int, max_results=50)`: builds the input for `APIFY_YOUTUBE_ACTOR` (⚠️ fetch the actor's input schema first; map fields: search queries, sort/upload date filter, live filter, max results) and normalises items into `YouTubeItem(video_id, channel_id, channel_name, title, is_live, viewers, views, published_at, duration_s, likes, comments)`.

### 4. `clients/anthropic_client.py` (docs: https://docs.claude.com)
- `complete_json(prompt_id, system, user_content, schema: type[BaseModel], model, max_tokens, images=None, cache_system=True) -> BaseModel`: calls Messages API, extracts text, parses JSON (strip code fences), validates; on failure retries once appending the validation error; records tokens and cost (price table in settings is not needed — keep a small dict per model in code, ⚠️ fill with current prices).
- Budget guard: `budget_ok(kind="vod"|"express")`: compares today's anthropic spend with `ai.daily_budget_usd`; `express` always allowed.

### 5. `clients/transcribe.py`
- Interface `transcribe(audio_path, mode="local"|"hosted", language=None) -> Transcript(language, words[{w,s,e}])`.
- Local: faster-whisper model from `vod.whisper_model`, `compute_type="int8"`, `word_timestamps=True`, `vad_filter=True`; load model once per process.
- Hosted: Groq OpenAI-compatible audio endpoint (⚠️ confirm model `whisper-large-v3-turbo` and word timestamps), fallback OpenAI `/v1/audio/transcriptions` with `response_format=verbose_json`, `timestamp_granularities[]=word`. Audio: 16 kHz mono (extracted with ffmpeg before calling). Record seconds and cost.

### 6. `clients/upload_post.py` (docs: https://docs.upload-post.com, raw index https://docs.upload-post.com/llm.txt)
- ✅ `upload_video(user, platform, video_path, fields: dict, idempotency_key, request_id)`: `POST https://api.upload-post.com/api/upload`, header `Authorization: Apikey <key>`, `Idempotency-Key`, multipart fields `user`, `platform[]`, `video`, `title`, plus per-platform fields (`privacy_level`, `youtube_title`, `youtube_description`, `privacyStatus`, `categoryId`, `media_type`, `first_comment`, `request_id`, `async_upload=true`).
- ⚠️ `get_upload_status(request_id)` — confirm path/method. Poll helper `wait_for_upload(request_id, timeout_s=600)` returning per-platform result (post id, URL, error).
- ✅ `get_profile_analytics(user, platforms)` → `GET /api/analytics/{user}?platforms=…`.
- ✅ `get_post_analytics(id)` → `GET /api/uploadposts/post-analytics/{id}` (⚠️ confirm which id).
- ⚠️ `delete_post(...)`: implement only if the docs now show a delete endpoint; otherwise raise `NotSupported` (guardian handles it).
- Error mapping: TikTok daily limit / 429 → `RateLimited(account, retry_after)`; auth problems → `AccountAuthError`; validation → `PostRejected`.

## Build steps
1. Implement the six clients and their dataclasses.
2. Implement `fakes.py`.
3. Unit tests with `respx` for HTTP clients (token caching, pagination, retries on 429/5xx, error mapping, JSON repair retry for Anthropic).
4. A `tests/live/` folder (skipped unless `RUN_LIVE_TESTS=1`) with one real smoke call per client against staging keys: Twitch game id for "Grand Theft Auto V", YouTube channels.list, Apify small search, Anthropic tiny JSON prompt, Groq transcription of a 5-second clip, Upload-Post analytics for profile `6stars-staging`.

## Acceptance tests
- All unit tests pass; mypy strict passes.
- With staging keys: `RUN_LIVE_TESTS=1 uv run pytest tests/live` passes (or failures are explained in PROGRESS.md with what the docs actually say).
- `sixstars youtube auth` produces a refresh token for the staging test channel; `whoami` prints it.

## Definition of done
Clients + fakes + tests merged; verified API facts and differences recorded in PROGRESS.md.

## Handoff
M03 uses Twitch IRC (not this Helix client) and chat-downloader directly inside `chat/` adapters. M05 builds prompts on top of `anthropic_client`.

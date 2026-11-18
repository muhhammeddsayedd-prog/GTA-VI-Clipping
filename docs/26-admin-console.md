# 26 — Admin console

The admin console is a private web app for the single operator. It shows every part of the pipeline and offers the controls the control API already has: emergency stop/start and the settings editor. It never becomes an approval step (D02): the system does not wait for it.

Decision **D109** (supersedes D23): build the console after launch as milestone M17. It is a Next.js app on Vercel in `web/`, one Vercel project per environment, reading through new read-only control-API routes.

## Architecture

```
Browser ──► Next.js on Vercel (web/, one project per environment)
              │ Supabase Auth session (httpOnly cookies), checked on the server
              │ Route handlers = thin backend-for-frontend; the browser never sees an API token
              ▼
            Control API (FastAPI, Hetzner, Caddy)
              │ /v1/admin/*  (read only, new)   /v1/status   /v1/reports/daily   /v1/control/*
              ▼
            Postgres + Storage (signed URLs minted by the API)
```

- RLS stays at "no policies". The browser never talks to Postgres or Storage directly.
- **Auth.** Supabase Auth in the environment's own project, email and password. Sign-ups are turned off and the one user is created by hand. Every request also checks the session email against `ADMIN_EMAIL`.
- **Tokens.** The console calls the API with `DASHBOARD_API_TOKEN`, which is separate from n8n's `CONTROL_API_TOKEN`.

| Token | Accepted on |
|---|---|
| `CONTROL_API_TOKEN` (n8n) | everything |
| `DASHBOARD_API_TOKEN` (console) | everything **except** the trigger routes (`POST /v1/discovery/run`, `/v1/posting/hourly`, `/v1/guardian/check`, `/v1/guardian/takedown`, `/v1/stats/collect`) |

- The request log line carries `actor` = `n8n` or `dashboard`.
- Live data is polled: status every 10 s, lists every 30 s, paused while the tab is hidden.

## Read API contract (`GET /v1/admin/*`)

All routes are `GET`, read only and authenticated with a bearer token. Times are ISO-8601 UTC strings. Money is a float in USD.

### Lists

A list answers:

```json
{"items": [...], "next_cursor": "opaque" | null, "counts": {"<status>": 12, "all": 40}}
```

- Query parameters are `limit` (default 50, max 200), `cursor`, and filters per route.
- `counts` holds the totals for the route's main status filter, ignoring that filter but applying the others. The UI uses it for the segmented status chips.

### Detail objects

Every detail object carries:

- `events`: the last 50 `events` rows for that entity.
- `trace`: links to the related entities, `{"creator": Ref|null, "live_session": Ref|null, "video": Ref|null, "spike": Ref|null, "clip": Ref|null, "posts": [Ref]}`.

A `Ref` is `{"id": str, "label": str, "status": str|null}`.

### Common shapes

| Shape | Fields |
|---|---|
| `CreatorRef` | `{id, handle, display_name, platform, tier, avatar_url: str\|null}` |
| `Event` | `{id, created_at, level, type, service, entity_type, entity_id, message, data}` |
| `Incident` | `{key, title, opened_at, data}`; the open ones are taken from `guardian/incidents.py` |
| `QueueItem` | `{clip_id, series_no, hook, title, creator: CreatorRef, lane, score, rank_score, duration_s, created_at, age_s, expires_at, thumb_url, thumb_kind}` |
| `Thumb` | `thumb_url: str\|null`, `thumb_kind: "frame"\|"source"\|null` (see Console media below) |
| `AccountSummary` | `{id, platform, handle, lanes, enabled, paused_until, pause_reason, cap_24h, spike_reserve, used_24h, spike_reserve_remaining, last_post_at, privacy, hourly_hours_utc, min_gap_s}` |

### Routes

| Route | Filters | Answer |
|---|---|---|
| `overview` | — | `{now, env, posting_mode, stop_reason, launch_at, dry_run, posts_24h: {published, failed, by_hour: [24 ints, oldest first]}, queue: {hourly, spike, low_watermark}, express: {p50_s, p90_s, series: [{t, s}]}, spend: {total_usd, budget_usd, by_provider: [{provider, usd, budget_usd}]}, accounts: [AccountSummary], up_next: [QueueItem ≤ 5], recent_events: [Event ≤ 10, level ≥ info, excluding noisy types], health: {services_ok, services_total, triggers_ok, triggers_total, open_incidents: [Incident], jobs: {queued, running, failed_24h}, live: {active, max_slots}}, growth: {accounts: [{account_id, platform, followers, delta_24h}], ypp: {subscribers, subscribers_target, shorts_views_90d, views_target}}}` |
| `pipeline` | `window=24h\|7d\|30d` | `{window, nodes: {discovery: {candidates, picked}, live: {active, max_slots, sessions}, vod: {new, processing, done, failed, skipped}, spikes: {detected, confirmed, rejected, merged}, render: {rendered, failed}, gate: {approved, rejected}, queue: {waiting, expired}, posts: {published, failed}}, funnel: [{tier, detected, confirmed, clipped, posted}], rejects: [{reason, n}], express_stages: [{stage, median_s}], express_series: [{t, s}], approved_by_hour: [{t, n}]}` |
| `live-sessions` | `status` | items `{id, creator: CreatorRef, platform, status, trend_score, url, channel_login, reconnects, assigned_at, started_at, ended_at, below_cutoff_since, thumbnail_url, spikes: {S, A, B}, clips}`; also `meta: {max_slots, blocked: {platform: until_iso}}` |
| `live-sessions/{id}` | — | the item (with `spikes` still the S/A/B counts) plus `{bot_names, clipped_ranges, vod_source_id, buffer_dir, spikes_list: [Spike], events, trace}` |
| `videos` | `status`, `platform` | items `{id, creator, platform, source_id, url, title, duration_s, published_at, trend_score, has_chat, status, skip_reason, attempts, created_at, updated_at, thumbnail_url, clips}`; also `meta: {source_health}` |
| `videos/{id}` | — | the item plus `{chat_file: {messages_count, duration_s, baseline_rate, bot_names, storage_path, downloaded_at} \| null, spikes: [Spike], jobs: [Job], events, trace}` |
| `spikes` | `status`, `tier`, `mode` | items `{id, mode, creator, live_session_id, video_id, detected_at, spike_wall, spike_offset_s, score_z, parts, tier, claude_confidence, claude_reason, status, merged_into, clip_id, clip_series_no, spike_to_post_s, window_start_s, window_end_s, window_start_wall, window_end_wall}` |
| `spikes/{id}` | — | the item plus `{chat_sample, msgs_in_bucket, unique_chatters, reaction_count, clip_requests, events, trace}` |
| `clips` | `status`, `lane`, `creator_id`, `q` | items `{id, series_no, spike_id, creator, source_platform, source_type, lane, duration_s, hook, title, topic, score, rank_score, music_ratio, status, reject_reason, gate_profile, created_at, approved_at, posted_at, has_file, thumb_url, thumb_kind}` |
| `clips/{id}` | — | the item plus `{caption, hashtags, commentary, transcript, keywords, safety_flags, score_parts, spoiler, profanity_count, gate_result, precheck_result, render_ms, file_size_bytes, storage_path, start_s, end_s, original_language, posts: [Post], events, trace}` |
| `clips/{id}/media` | — | `{url: str\|null, expires_at: str\|null, reason: "ok"\|"no_file"\|"deleted"}`; the signed URL lasts 1 h |
| `queue` | — | `{ranked: {hourly: [QueueItem], spike: [QueueItem]}, slots: [{account_id, platform, hours: [{hour, state, post_id}]}], caps: [{account_id, used_24h, cap_24h, free, spike_reserve_remaining, hourly_allowed, spike_allowed, gap_ok, last_post_at, paused_until}], decisions: [Event], next_slot_at}`. Slot `state` is one of `posted`, `failed`, `skipped`, `planned`, `not_allowed`, `empty` |
| `posts` | `status`, `account_id`, `lane`, `clip_id` | items `{id, clip_id, clip_series_no, clip_hook, account_id, platform, lane, slot_at, status, dry_run, post_url, platform_post_id, published_at, views, likes, comments, shares, claim_status, error, attempts, created_at, clip_thumb_url}` |
| `posts/{id}` | — | the item plus `{upload_request_id, idempotency_key, stats_updated_at, claim_checked_at, removed_at, remove_reason, events, trace}` |
| `creators` | `tier`, `platform`, `q` | items `{id, platform, channel_id, handle, display_name, language, tier, tier_reason, follower_count, avg_clip_views, claims_count, description_checked_at, discovered_at, avatar_url, avatar_checked_at, clips_posted}` |
| `creators/{id}` | — | the item plus `{layout: creator_layouts row \| null, counts: {live_sessions, videos, spikes, clips, posts, takedowns}, clips: [Clip ≤ 12], events, trace}` |
| `accounts` | — | items `[AccountSummary]` (not paginated) |
| `accounts/{id}` | — | `AccountSummary` plus `{caps: CapRow, stats: [{captured_at, followers, views_total}], views_by_day: [{day, views}], posts: [Post ≤ 20], events}` |
| `discovery` | — | `{runs: [Event discovery.run/failed ≤ 20], source_health: {discovery, live, vod}, apify_last_run, quotas: {youtube_units_today, youtube_units_limit, search_calls_today, search_calls_limit, apify_today_usd, apify_daily_budget_usd, apify_month_usd, apify_monthly_budget_usd}}` |
| `candidates` | `source_type`, `picked` | items: `candidates` rows with `creator` |
| `guardian` | — | `{strikes: {count, limit, window_d, items: [{at, creator, takedown_id}]}, emergency_stop, stop_reason, manual_removals: [Post with clip_hook], claims: {last_run_at, ok, suspect, unverifiable, removed_by_platform}, takedown_counts: {open, actioned, partial, ignored}}` |
| `takedowns` | `status` | items: the `takedowns` row with `creator` |
| `takedowns/{id}` | — | the item plus `{matched_posts: [Post], events}` |
| `health` | — | `{heartbeats: [{name, kind, last_seen, age_s, stale, info}], triggers: [{name, last_seen, grace_s, source, stale}], open_incidents: [Incident], incidents: [Event health.*], jobs: {queued, running, failed_24h}}` |
| `alerts` | `severity`, `delivered` | items: `notify_outbox` rows |
| `jobs` | `status`, `queue`, `type` | items `{id, queue, type, priority, status, attempts, max_attempts, run_after, locked_by, locked_at, last_error, dedupe_key, created_at, updated_at}`; also `meta: {by_queue: {express: {status: n}, normal: {...}}}` |
| `jobs/{id}` | — | the item plus `{payload}` |
| `events` | `type_prefix`, `level`, `service`, `entity_type`, `entity_id` | items `Event` |
| `events/{id}` | — | `Event` plus `{related: [Event]}`, the same entity within ±10 min |
| `usage` | `range=today\|7d\|month` | `{totals: [{provider, usd}], budgets: {anthropic, transcribe, apify_daily, apify_monthly, month_ceiling}, daily: [{day, provider, usd}], operations: [{provider, operation, model, calls, input_tokens, output_tokens, units, usd}], quotas}` |
| `settings` | — | items `[{key, value, description, updated_at, group, written_by_code}]` (not paginated) |
| `settings/history` | — | `setting.changed` / `control.*` events |
| `search` | `q` | `{results: [{kind, id, label, sub, href}]}`; matches uuid prefixes, series numbers (`#142` or `142`), handles, post URLs and setting keys |

### Console media (thumbnails and profile pictures)

Added after M17, additive only (migration `0022_console_media.sql`).

| Field | Where | Value |
|---|---|---|
| `avatar_url` | every `CreatorRef`; `creators` items and `creators/{id}` (with `avatar_checked_at`) | the platform's public profile picture (Twitch Get Users `profile_image_url`, YouTube `channels.list` `snippet.thumbnails` medium/high/default), or null |
| `thumb_url`, `thumb_kind` | `clips` items, `clips/{id}`, `QueueItem` (so `queue.ranked.*[]` and `overview.up_next[]`), `creators/{id}.clips[]` | `"frame"`: a signed URL (1 h) of `clips.thumb_path`, a JPEG frame of the rendered clip; else `"source"`: the platform thumbnail of the clip's source (its viewer clip, the video of its spike, the video it was cut from, or its YouTube live session); else both null |
| `clip_thumb_url` | `posts` items, `posts/{id}`, every embedded `Post` (`clips/{id}.posts`, `accounts/{id}.posts`, `takedowns/{id}.matched_posts`, `guardian.manual_removals`) | the same choice for the post's clip, without the kind |
| `thumbnail_url` | `videos` items and `videos/{id}`; `live-sessions` items and `live-sessions/{id}` | the platform thumbnail (Twitch Get Videos at the only allowed size 320x180; Twitch Get Streams preview at 640x360, a frame from the last 5 minutes of the stream; YouTube `snippet.thumbnails` high/medium/default), or null |

All frames named in one answer are signed in one Storage request (`POST /storage/v1/object/sign/clips` with
`{expiresIn, paths}`). When signing fails the answer still comes, with the source thumbnail or null; it is never an
error. URLs of `"source"` and `avatar_url` are public platform CDN URLs and do not expire, but a platform may stop
serving one (a deleted video or a changed picture); the console shows a placeholder when an image fails to load.

Where the data comes from:

- frame: right after a clip is rendered (`factory/thumbs.py`, before the gate, so gate-rejected clips have one
  too; clips the writer rejected are never rendered). Frame at `console.thumb_frame_s`, `console.thumb_width_px`
  wide, ffmpeg JPEG quality `console.thumb_jpeg_qscale`, key `clips/thumbs/<env>/<yyyy>/<mm>/<dd>/<clip_id>.jpg`.
  A failure is logged and recorded as `clip.thumb_failed`; it never fails the render.
- source thumbnails: stored by discovery on `videos`, `live_sessions` and `viewer_clips` rows each pass.
- profile pictures: each discovery pass looks up at most `console.avatars_per_pass` creators that have none or
  whose last lookup is older than `console.avatar_refresh_days` (YouTube inside `discovery.youtube_quota_daily_units`);
  the pass summary carries the counts as `avatars`.
- rows made before migration 0022: `sixstars console-media-backfill [--limit N]` (idempotent).

Errors use the existing shape of the control API (docs/17), `{"error": "<message>", "code": "<code>", "request_id": "<id>"}`, with 404 (`not_found`) for unknown or malformed ids, 422 (`invalid_filter`, `invalid_cursor`, `validation_error`) for a bad parameter and 504 (`timeout`) when a query passes `admin.statement_timeout_ms`.

## Screens

There is one route per page in `web/app/(console)/`. The mockups in the owner's "Screen designs" folder are the visual reference. The sitemap is the one in the M17 plan: Overview, Pipeline, Content (Live, VOD, Spikes, Clips, Queue, Posts), Sources (Discovery, Creators, Accounts), Guardian, Operations (Health, Alerts, Jobs, Events), Insights (Spend, Reports) and Settings.

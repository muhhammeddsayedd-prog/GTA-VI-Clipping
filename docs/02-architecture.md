# 02 — Architecture

## 1. Deployment view

```
┌──────────────────────── Hetzner server (per environment) ────────────────────────┐
│ docker compose (one image: ghcr.io/<owner>/6stars-clips)                          │
│                                                                                   │
│  caddy ──HTTPS──► api (FastAPI control API, :8080)                                │
│                                                                                   │
│  live-manager      asyncio: chat listeners, stream recorders, live spike engine   │
│  worker-express×2  job queue "express": S-tier renders + spike posting            │
│  worker-normal×4   job queue "normal": VOD, A/B renders, pre-checks, cleanup      │
│  scheduler         heartbeats, failover triggers, cleanup timers                  │
│                                                                                   │
│  /data volume: live buffers, downloads, renders (scratch, auto-cleaned)           │
└───────────────────────────────────────────────────────────────────────────────────┘
          │ Postgres (pooler) + Storage                 ▲ HTTPS + bearer token
          ▼                                             │
┌──────── Supabase project ────────┐        ┌──────── n8n (owner's VPS) ────────┐
│ Postgres: all state + job queue  │        │ schedules → control API            │
│ Storage bucket: clips            │        │ Gmail trigger → takedown endpoint  │
└──────────────────────────────────┘        │ notify webhook → Telegram          │
                                            │ Telegram commands → control API    │
                                            └────────────────────────────────────┘
External APIs: Twitch Helix + IRC · YouTube (RSS, Data API) · Apify · Anthropic ·
Groq/OpenAI (transcription) · Upload-Post (posting + analytics) · residential proxy
```

Two complete copies exist: **staging** and **production**. Each has its own server, Supabase project, Upload-Post profiles, n8n workflows (tagged by environment, pointing at that environment's API) and env file.

## 2. Responsibilities

| Component | Owns | Does not own |
|---|---|---|
| Python services | All business logic: discovery, spikes, rendering, gate, posting decisions, guardian logic, stats, report content | Timers that fire on a schedule (n8n), email reading, Telegram delivery |
| n8n | Schedules, Gmail takedown intake, Telegram delivery, Telegram commands, n8n error alerts | Any decision logic |
| Supabase | Persistent state, job queue, clip files | Logic |
| Upload-Post | Publishing to TikTok/Instagram/YouTube, analytics | Deciding what to post |

If n8n is down, the `scheduler` service notices missing triggers and runs hourly posting and discovery itself (failover), and keeps alerts in an outbox until n8n is back.

## 3. Data flow

### 3.1 Discovery → intake
1. n8n `WF01` calls `POST /v1/discovery/run` every 10 minutes.
2. Discovery pulls Twitch streams/videos for the target game and YouTube live/recent via Apify + RSS.
3. Writes `candidates`, upserts `creators`, computes trend scores.
4. Live: updates `live_sessions` assignments (start/stop recordings). `live-manager` polls assignments every 15 s.
5. VOD: inserts `videos` rows (status `new`) and enqueues `vod.process` jobs.

### 3.2 Live lane
1. `live-manager` starts a recorder (streamlink → ffmpeg segmenter) and a chat listener per assigned session.
2. Chat messages feed the live spike engine (10 s window, slides every 5 s).
3. On a spike: insert `spikes` row, call Claude spike confirmation (Haiku).
4. S-tier → enqueue `express.render` (queue `express`, high priority). A/B → enqueue `clip.render` (queue `normal`).
5. Stream ends → session `ended`, enqueue `replay.scan` for when the VOD appears.

### 3.3 VOD lane
1. `vod.process` job: get chat replay → replay spike engine → `spikes` rows.
2. For each confirmed spike: `yt-dlp --download-sections` → `clip.render` job.
3. No chat → fallback: download, transcribe, Claude picks moments → `clip.render` jobs.

### 3.4 Factory → gate → queue
1. `clip.render`/`express.render`: transcribe window (local or API) → Claude clip writer → render → gate.
2. Pass → `clips.status = 'approved'` with `lane` = `spike` (express) or `hourly`.
3. Risky hourly clips → `clip.precheck` (private YouTube upload) → after 30 min `clip.precheck_result` → approved or rejected.
4. MP4 uploaded to Storage bucket `clips`.

### 3.5 Posting
- Spike lane: `express.render` job, on gate pass, calls `posting.spike.try_post(clip)` directly (no waiting on n8n).
- Hourly lane: n8n `WF02` calls `POST /v1/posting/hourly` → worker waits jitter → for each account: precedence rules → cap manager → Upload-Post.
- Every attempt writes `posts` rows; failures retried with backoff.

### 3.6 Guardian and stats
- `WF03` → `/v1/guardian/check` (claims/removals/health).
- `WF04` Gmail trigger → `/v1/guardian/takedown`.
- `WF05` → `/v1/stats/collect`; `WF06` → `/v1/reports/daily` → Telegram.
- Worker → `WF07` notify webhook → Telegram (alerts).

## 4. Job queue

Single `jobs` table. Workers claim with `FOR UPDATE SKIP LOCKED`, ordered by `priority DESC, run_after, id`.

| Job type | Queue | Priority | Created by |
|---|---|---|---|
| `express.render` | express | 100 | live spike engine (S-tier) |
| `clip.render` | normal | 60 (A), 40 (B/VOD) | live engine, VOD lane |
| `vod.process` | normal | 30 | discovery |
| `replay.find` | normal | 25 | live-manager on stream end (waits for the VOD) |
| `replay.scan` | normal | 25 | `replay.find` once the VOD exists |
| `creator.permission` | normal | 20 | discovery (new creators, 14-day re-check) |
| `clip.precheck` | normal | 50 | gate |
| `clip.precheck_result` | normal | 50 | precheck (run_after +30 min) |
| `post.retry` | express | 90 | posting engine |
| `cleanup.files` | normal | 10 | scheduler |

Retries: exponential backoff (30 s, 2 min, 10 min), `max_attempts` 3, then `failed` + event + alert if important.

## 5. State machines

**videos.status**: `new → processing → done | failed | skipped`

**live_sessions.status**: `assigned → recording → ended → replay_queued → replay_done` (or `failed`)

**spikes.tier**: `S | A | B | rejected`; **spikes.status**: `detected → confirmed | rejected → rendering → clipped | failed`

**clips.status**: `rendering → gating → approved | rejected → (precheck_pending → approved | rejected) → posted → removed`
A clip in `approved` with `lane = spike` older than `posting.spike_max_age_s` is converted to `lane = hourly`.

**posts.status**: `pending → published | failed → removed | remove_failed`

## 6. Failure handling principles

1. Every external call: timeout, 3 retries, then a recorded failure.
2. A failed clip never blocks the queue.
3. A failed account (token expired, cap hit) is paused alone; other accounts keep posting.
4. Recorders reconnect 3 times, then free their slot.
5. Emergency stop halts posting only; discovery and rendering continue so the queue stays full for when posting resumes.

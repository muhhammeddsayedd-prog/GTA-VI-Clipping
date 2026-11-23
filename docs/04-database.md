# 04 — Database

Source of truth: `supabase/migrations/0001_init.sql` (schema) and `0002_seed_settings.sql` (default settings). Environment seeds: `supabase/seed/production.sql`, `supabase/seed/staging.sql`.

## 1. Projects and access

| Environment | Supabase project | Seed after migrations |
|---|---|---|
| Production | existing empty Pro project → rename to `6stars-production` | `supabase/seed/production.sql` |
| Staging | new project `6stars-staging` in the same Pro organisation | `supabase/seed/staging.sql` |

- Apply migrations with the Supabase CLI: `supabase link --project-ref <ref>` then `supabase db push --remote --yes` (CLI 3.x, run as `npx supabase@latest`; without `--remote` it targets a local database). The exact commands used are in `development-plan/PROGRESS.md` (M01).
- The worker connects through the **session pooler** (port 5432) using `DATABASE_URL`. Long-running processes keep a small psycopg pool (`DB_POOL_MAX`).
- RLS is enabled on every table with **no policies**: the anon/public keys can read nothing. Only the server (postgres role / service role) can access data.
- Storage bucket `clips` is private. Files are uploaded with the service role key.

## 2. Tables

| Table | Purpose | Key columns | Written by |
|---|---|---|---|
| `settings` | Runtime configuration | key, value (jsonb) | seed, CLI, guardian (`emergency_stop`) |
| `creators` | Every creator seen | platform + channel_id (unique), tier, claims_count | discovery, guardian |
| `creator_layouts` | Cached facecam/gameplay boxes per creator | facecam_box, expires_at (7 days) | factory |
| `candidates` | Every discovery observation (history for growth maths) | trend_score, viewers, views_per_hour, checked_at | discovery |
| `live_sessions` | A stream being recorded | status, clipped_ranges, bot_names, vod_source_id | discovery (assign), live-manager |
| `videos` | Finished streams/uploads for the VOD lane | status, has_chat, live_session_id | discovery, live-manager (replay), VOD lane |
| `chat_files` | Downloaded chat replays | messages_count, baseline_rate | VOD lane |
| `spikes` | Every detected spike | mode, score_z, parts, tier, window_*, status, clip_id | spike engine |
| `clips` | Every rendered clip | lane, status, score, rank_score, gate_result, storage_path, style_id, layout_kind, style_plan, moment_type, style_texts, version (`short`/`story`), parent_clip_id | factory, gate, ranking, posting |
| `clip_renders` | One render per design style of a clip (migration 0027) | clip_id + style_id (unique), is_reference, status, local_path, storage_path, checks | factory (`clip.variants`) |
| `platform_accounts` | Posting destinations and caps | lanes, cap_24h, spike_reserve, hourly_hours_utc, paused_until | seed, cap manager |
| `posts` | One row per clip per account | status, idempotency_key, platform_post_id, views, render_id (the file posted) | posting, stats, guardian |
| `account_stats` | Follower/view snapshots | followers, views_total | stats collector |
| `takedowns` | Takedown emails and claims | is_takedown, creator_id, actions | guardian |
| `jobs` | Work queue | queue, type, payload, priority, status, run_after, dedupe_key | everyone |
| `events` | Audit log | level, type, entity, data | everyone |
| `notify_outbox` | Telegram alerts waiting to be sent | severity, title, sent_at | notify |
| `api_usage` | Spend tracking | provider, operation, cost_usd; model calls also stop_reason, attempt, outcome (ok/invalid/refusal/http_error), cache_read_tokens, cache_write_tokens, detail (migration 0037) | every client |
| `heartbeats` | Liveness + trigger tracking | name, last_seen | every service, API routes |

## 3. Important rules

- **Idempotency**: `posts.idempotency_key = 'post:<clip_id>:<account_id>'` (unique). A clip can never be posted twice to the same account.
- **Job dedupe**: `jobs.dedupe_key` is unique among queued/running jobs, e.g. `vod:<video_id>`, `render:<spike_id>`, `replay:<live_session_id>`.
- **Claiming jobs**: `select * from claim_job('normal', '<worker-name>')`.
- **Series numbers**: `nextval('clip_series_seq')` when a clip is approved.
- **clipped_ranges** (live_sessions) stores wall-clock ranges; when the VOD appears, convert to VOD offsets with `vod_offset = wall − stream_started_at` (Twitch VOD offset 0 = stream start).
- **Never delete** rows from `posts`, `clips`, `spikes`, `events` (files are deleted, rows stay). Old `candidates` and `events` older than 30 days can be pruned by the scheduler.

## 4. Useful queries

```sql
-- Queue depth by lane
select * from v_queue_depth;

-- Posts in the last 24 h per account
select * from v_posts_last_24h;

-- Top of the hourly queue
select id, hook, score, rank_score, created_at from clips
where status = 'approved' and lane = 'hourly' order by rank_score desc limit 10;

-- Spike funnel today
select tier, status, count(*) from spikes where detected_at > now() - interval '1 day' group by 1,2 order by 1,2;

-- Express latency today
select percentile_cont(0.5) within group (order by spike_to_post_s) as p50,
       percentile_cont(0.9) within group (order by spike_to_post_s) as p90
from spikes where spike_to_post_s is not null and detected_at > now() - interval '1 day';

-- Spend today
select provider, sum(cost_usd) from api_usage where created_at > date_trunc('day', now()) group by 1;
```
